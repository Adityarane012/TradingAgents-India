"""Crash-safety tests for the unattended daily refresh.

Each test stands in for a way the scheduled run can die: power lost mid-write,
a second run started on top of the first, a process killed while holding the
lock, a run that would overrun its evening window.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import subprocess
import sys
import textwrap
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from tradingagents.dataflows import safe_io
from tradingagents.dataflows.refresh_triggers import load_last_reports
from tradingagents.dataflows.safe_io import (
    LOCK_HELD_ENV,
    AlreadyRunning,
    append_csv_row,
    atomic_write_text,
    backup,
    is_complete_row,
    read_complete_rows,
    rotate_log,
    single_instance,
)

FIELDS = ["ticker", "date", "status", "signal", "run_at"]
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _script(name):
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(ticker, run_at="2026-09-21T19:10:00"):
    return {"ticker": ticker, "date": "2026-09-21", "status": "ok", "signal": "Hold",
            "run_at": run_at}


class TestAtomicWrite:
    def test_replaces_content_and_leaves_no_temp(self, tmp_path):
        out = tmp_path / "summary.md"
        atomic_write_text(out, "old")
        atomic_write_text(out, "new ₹")
        assert out.read_text(encoding="utf-8") == "new ₹"
        assert [p.name for p in tmp_path.iterdir()] == ["summary.md"]

    def test_a_crash_before_the_swap_keeps_the_old_file(self, tmp_path, monkeypatch):
        out = tmp_path / "review_latest.html"
        atomic_write_text(out, "yesterday's dashboard")

        def die(*a, **k):
            raise OSError("power lost")

        monkeypatch.setattr(safe_io.os, "replace", die)
        with pytest.raises(OSError):
            atomic_write_text(out, "half")
        assert out.read_text(encoding="utf-8") == "yesterday's dashboard"
        assert [p.name for p in tmp_path.iterdir()] == ["review_latest.html"]


class TestDurableAppend:
    def test_header_once_then_rows(self, tmp_path):
        p = tmp_path / "runs.csv"
        append_csv_row(p, FIELDS, _row("A.NS"))
        append_csv_row(p, FIELDS, _row("B.NS"))
        assert [r["ticker"] for r in csv.DictReader(p.open(encoding="utf-8"))] == ["A.NS", "B.NS"]

    def test_a_row_torn_by_power_loss_does_not_swallow_the_next(self, tmp_path):
        p = tmp_path / "runs.csv"
        append_csv_row(p, FIELDS, _row("A.NS"))
        with p.open("a", encoding="utf-8", newline="") as h:
            h.write("B.NS,2026-09-21,ok,Ho")  # the machine died here
        append_csv_row(p, FIELDS, _row("C.NS"))
        assert [r["ticker"] for r in read_complete_rows(p)] == ["A.NS", "C.NS"]

    def test_torn_row_is_ignored_by_the_refresh(self, tmp_path):
        p = tmp_path / "runs.csv"
        append_csv_row(p, FIELDS, _row("A.NS"))
        with p.open("a", encoding="utf-8", newline="") as h:
            h.write("B.NS,2026-09-21,ok,Hold,2026-09-21T1")  # cut inside run_at
        assert set(load_last_reports(p)) == {"A.NS"}

    @pytest.mark.parametrize("run_at, ok", [
        ("2026-09-21T19:10:00", True),
        ("2026-09-21T19:1", False),
        ("2026-09-21", False),
        (None, False),
    ])
    def test_is_complete_row(self, run_at, ok):
        assert is_complete_row(_row("A.NS", run_at)) is ok


class TestLock:
    def test_second_run_is_refused_then_allowed_after(self, tmp_path):
        lock = tmp_path / "refresh.lock"
        with single_instance(lock), pytest.raises(AlreadyRunning), single_instance(lock):
            pass
        with single_instance(lock):
            pass  # released on exit

    def test_child_of_the_holder_is_not_locked_out(self, tmp_path, monkeypatch):
        lock = tmp_path / "refresh.lock"
        with single_instance(lock):
            monkeypatch.setenv(LOCK_HELD_ENV, "1")
            with single_instance(lock):
                pass

    def test_a_killed_holder_never_leaves_a_stale_lock(self, tmp_path):
        lock = tmp_path / "refresh.lock"
        ready = tmp_path / "ready"
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import time
            from pathlib import Path
            from tradingagents.dataflows.safe_io import single_instance
            with single_instance(Path({str(lock)!r})):
                Path({str(ready)!r}).touch()
                time.sleep(60)
        """)], env={k: v for k, v in os.environ.items() if k != LOCK_HELD_ENV})
        try:
            for _ in range(200):
                if ready.exists():
                    break
                time.sleep(0.05)
            assert ready.exists()
            with pytest.raises(AlreadyRunning), single_instance(lock):
                pass
        finally:
            holder.kill()  # as a crash or Task Scheduler's time limit would
            holder.wait()
        with single_instance(lock):
            pass

    def test_refresh_exits_cleanly_when_another_run_holds_the_lock(self, tmp_path, monkeypatch):
        refresh = _script("daily_refresh")
        monkeypatch.setattr(refresh, "LOCK", tmp_path / "refresh.lock")
        with single_instance(refresh.LOCK):
            assert refresh.main(["--dry-run"]) == 3


class TestBackupAndRotation:
    def test_backup_keeps_the_newest(self, tmp_path):
        src = tmp_path / "runs.csv"
        src.write_text("x", encoding="utf-8")
        for day in range(1, 6):
            backup(src, tmp_path / "backups", f"2026-09-0{day}", keep=3)
        kept = sorted(p.name for p in (tmp_path / "backups").iterdir())
        assert kept == [f"runs.2026-09-0{d}.csv" for d in (3, 4, 5)]

    def test_backup_of_nothing_is_none(self, tmp_path):
        assert backup(tmp_path / "missing.csv", tmp_path / "b", "s") is None

    def test_rotation_shifts_and_caps(self, tmp_path):
        log = tmp_path / "daily_refresh.log"
        for n in range(5):
            log.write_text(f"run {n}" * 10, encoding="utf-8")
            rotate_log(log, max_bytes=10, keep=2)
        assert not log.exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "daily_refresh.log.1", "daily_refresh.log.2"]
        assert log.with_name("daily_refresh.log.1").read_text(encoding="utf-8").startswith("run 4")

    def test_small_log_is_left_alone(self, tmp_path):
        log = tmp_path / "daily_refresh.log"
        log.write_text("short", encoding="utf-8")
        rotate_log(log, max_bytes=1000)
        assert log.read_text(encoding="utf-8") == "short"


class TestWindow:
    def test_no_ticker_is_started_that_would_overrun(self):
        batch = _script("analyze_india_universe")
        deadline = datetime(2026, 9, 21, 19, 57)
        assert batch._time_for_another(datetime(2026, 9, 21, 19, 50), deadline, 240)
        assert not batch._time_for_another(datetime(2026, 9, 21, 19, 54), deadline, 240)
        assert batch._time_for_another(datetime(2026, 9, 21, 23, 0), None, 240)

    def test_until_is_today_at_that_time(self):
        refresh = _script("daily_refresh")
        now = datetime(2026, 9, 21, 19, 3, 12)
        assert refresh.window_end("20:00", now) == datetime(2026, 9, 21, 20, 0)
        assert refresh.window_end(None, now) is None


class TestSessionFinality:
    """A run must never treat a live intraday quote as the day's close.

    On 2026-09-24 the scheduler ran the missed evening job at 09:26, eleven
    minutes after the open. yfinance already had a bar dated that day, so the
    old check ("is the newest bar today's?") passed and the batch began
    analysing a session that had barely started.
    """

    @pytest.mark.parametrize("bar, now, final", [
        # today's bar, before the close -> a live quote, not a close
        (date(2026, 9, 24), datetime(2026, 9, 24, 9, 26), False),
        (date(2026, 9, 24), datetime(2026, 9, 24, 15, 29), False),
        # just after the close, before the settle window is up
        (date(2026, 9, 24), datetime(2026, 9, 24, 15, 40), False),
        # settled, and the usual evening slot
        (date(2026, 9, 24), datetime(2026, 9, 24, 15, 45), True),
        (date(2026, 9, 24), datetime(2026, 9, 24, 19, 0), True),
        # a bar from an earlier session is finished whatever the time
        (date(2026, 9, 23), datetime(2026, 9, 24, 9, 26), True),
    ])
    def test_only_a_closed_session_counts(self, bar, now, final):
        refresh = _script("daily_refresh")
        assert refresh.session_is_final(bar, date(2026, 9, 24), now) is final
