"""Crash-safe file handling for the unattended refresh.

The daily refresh runs from Task Scheduler on a laptop, so it has to survive
the machine sleeping, losing power or being killed at any instant. The rules:

- Whole-file outputs (summaries, dashboards) are written to a temporary file
  and swapped in with os.replace, so a reader sees the old file or the new
  one, never half of one.
- The runs CSV is append-only history and is never rewritten. Each row is
  flushed to disk before the next ticker starts, so a crash loses at most the
  ticker in progress. A row torn by power loss is sealed off with a newline
  before the next append (otherwise the next row would be glued onto it and
  both lost), and readers skip incomplete rows.
- Only one refresh runs at a time. The lock is an OS file lock rather than a
  PID file, so the OS drops it when the process dies and a crash can never
  leave a stale lock that blocks every later run.
"""

from __future__ import annotations

import csv
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from uuid import uuid4

# Set in the environment of a child process whose parent already holds the
# lock, so the batch runner started by the refresh does not lock itself out.
LOCK_HELD_ENV = "TRADINGAGENTS_REFRESH_LOCK_HELD"


class AlreadyRunning(RuntimeError):
    """Another refresh or batch run holds the lock."""


def _replace(src: Path, dst: Path, attempts: int = 5) -> None:
    # On Windows a virus scanner or indexer can hold the target open for a
    # moment; os.replace then fails with PermissionError. Retry briefly.
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call, not per process: two threads writing the same path
    # would otherwise share a temp name, and the first one's os.replace would
    # leave the second with nothing to rename (FileNotFoundError, seen on the
    # price cache on 2026-09-24).
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with temp.open("w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def append_csv_row(path: Path, fieldnames: list[str], row: dict) -> None:
    """Append one row durably. Writes the header for a new file, and seals a
    torn last line (no trailing newline) so this row starts on its own line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    if not is_new:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            torn = handle.read(1) not in (b"\n", b"\r")
    with path.open("a", encoding="utf-8", newline="") as handle:
        if not is_new and torn:
            handle.write("\r\n")
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def is_complete_row(row: dict, last_field: str = "run_at", min_length: int = 19) -> bool:
    """A row cut short by a crash is missing fields or ends in a partial
    timestamp. The runs CSV ends with run_at, which makes that checkable.

    ``min_length`` is what a whole value looks like: 19 for a timestamp
    (yyyy-mm-ddThh:mm:ss), 10 for a date-only column such as the scan log's
    scan_date. Stating it per caller beats guessing from the content, because
    "2026-09-21" is a complete date and a truncated timestamp."""
    if None in row or any(v is None for v in row.values()):
        return False
    try:
        datetime.fromisoformat(row[last_field])
    except (KeyError, TypeError, ValueError):
        return False
    return len(row[last_field]) >= min_length


def read_complete_rows(path: Path, last_field: str = "run_at",
                       min_length: int = 19) -> Iterator[dict]:
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if is_complete_row(row, last_field, min_length):
                yield row


def backup(path: Path, backup_dir: Path, stamp: str, keep: int = 14) -> Path | None:
    """Copy ``path`` to ``backup_dir/<stem>.<stamp><suffix>`` and keep the
    newest ``keep`` copies. Returns the backup path, or None if nothing to copy."""
    path = Path(path)
    if not path.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    temp = dest.with_name(f".{dest.name}.tmp")
    try:
        shutil.copy2(path, temp)
        _replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)
    copies = sorted(backup_dir.glob(f"{path.stem}.*{path.suffix}"))
    for old in copies[:-keep]:
        old.unlink(missing_ok=True)
    return dest


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive OS lock on ``lock_path`` for the duration, or raise
    AlreadyRunning. A no-op when the parent process already holds it."""
    if os.environ.get(LOCK_HELD_ENV) == "1":
        yield
        return
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(f"another run holds {lock_path}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                with suppress(OSError):  # closing the handle releases it anyway
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def rotate_log(path: Path, max_bytes: int = 5_000_000, keep: int = 3) -> None:
    """Shift log -> log.1 -> log.2 ... once it passes ``max_bytes``."""
    path = Path(path)
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for n in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{n}")
        if older.exists():
            _replace(older, path.with_name(f"{path.name}.{n + 1}"))
    _replace(path, path.with_name(f"{path.name}.1"))
