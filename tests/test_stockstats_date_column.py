"""Tests for tolerating a non-`Date` index column in stockstats_utils (#890).

Guards against a download frame whose date column is `index` or `Datetime`
instead of `Date`, which would otherwise silently drop every indicator.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from tradingagents.dataflows import stockstats_utils as su


def _ohlcv(date_col: str) -> pd.DataFrame:
    """OHLCV frame whose date column is named `date_col`."""
    dates = pd.bdate_range("2026-04-01", periods=10)
    return pd.DataFrame({
        date_col: dates,
        "Open": [100.0 + i for i in range(10)],
        "High": [101.0 + i for i in range(10)],
        "Low": [99.0 + i for i in range(10)],
        "Close": [100.5 + i for i in range(10)],
        "Volume": [1_000_000 + i for i in range(10)],
    })


@pytest.mark.unit
class TestEnsureDateColumn:
    def test_renames_index_column(self):
        out = su._ensure_date_column(_ohlcv("index"))
        assert "Date" in out.columns and "index" not in out.columns

    def test_renames_datetime_and_date_variants(self):
        assert "Date" in su._ensure_date_column(_ohlcv("Datetime")).columns
        assert "Date" in su._ensure_date_column(_ohlcv("date")).columns

    def test_leaves_existing_date_untouched(self):
        df = _ohlcv("Date")
        assert su._ensure_date_column(df) is df  # no-op short-circuit

    def test_no_datelike_column_is_left_alone(self):
        df = pd.DataFrame({"Close": [1, 2, 3]})
        out = su._ensure_date_column(df)
        assert "Date" not in out.columns  # nothing to rename; caller handles


@pytest.mark.unit
class TestCleanDataframeAcrossVersions:
    def test_clean_handles_index_column(self):
        """A frame with `index` instead of `Date` must still clean to a
        usable, date-parsed frame (was KeyError: 'Date')."""
        cleaned = su._clean_dataframe(_ohlcv("index"))
        assert "Date" in cleaned.columns
        assert pd.api.types.is_datetime64_any_dtype(cleaned["Date"])
        assert len(cleaned) == 10

    def test_clean_handles_legacy_date_column(self):
        cleaned = su._clean_dataframe(_ohlcv("Date"))
        assert len(cleaned) == 10

    def test_indicators_compute_after_index_rename(self):
        """stockstats must compute indicators on a frame whose date column
        arrived as `index`, instead of erroring per indicator."""
        from stockstats import wrap
        cleaned = su._clean_dataframe(_ohlcv("index"))
        df = wrap(cleaned)
        df["close_5_sma"]  # triggers calculation
        assert "close_5_sma" in df.columns
        assert df["close_5_sma"].notna().any()


class TestDamagedDateRowsAreDropped:
    """A truncated date must cost one bar, not the whole ticker.

    Found in a live batch run: a cache CSV row for TCS.NS had lost the leading
    "202" of its date, leaving "9-17". pandas parses that without complaint as
    year 1, and the failure only surfaces later when the column is cast to
    datetime64[ns] — an OutOfBoundsDatetime that aborted the entire ticker.
    The damage came from an interrupted write, so any crash can reproduce it.
    """

    @pytest.mark.parametrize("raw", ["9-17", "0001-09-17", "1-1"])
    def test_implausible_years_become_nat(self, raw):
        assert pd.isna(su._local_midnight(raw))

    @pytest.mark.parametrize("raw", ["2026-09-17", "1999-01-04"])
    def test_real_dates_still_parse(self, raw):
        assert su._local_midnight(raw) == pd.Timestamp(raw)

    def test_unparseable_values_still_become_nat(self):
        assert pd.isna(su._local_midnight("not-a-date"))
        assert pd.isna(su._local_midnight(None))

    def test_a_damaged_row_is_dropped_and_the_rest_survive(self):
        frame = pd.DataFrame(
            {
                "Date": ["2026-09-15", "9-17", "2026-09-18"],
                "Open": [1.0, 2.0, 3.0],
                "High": [1.0, 2.0, 3.0],
                "Low": [1.0, 2.0, 3.0],
                "Close": [1.0, 2.0, 3.0],
                "Volume": [10, 20, 30],
            }
        )
        cleaned = su._clean_dataframe(frame)
        assert len(cleaned) == 2
        assert list(cleaned["Date"].dt.strftime("%Y-%m-%d")) == ["2026-09-15", "2026-09-18"]

    def test_the_whole_column_casts_without_overflow(self):
        """The actual failure mode: the cast, not the parse."""
        series = su._normalize_dates(["2026-09-15", "9-17", "2026-09-18"])
        assert str(series.dtype).startswith("datetime64")
        assert series.isna().sum() == 1


class TestDamagedCacheSelfHeals:
    """A cache file with unparseable rows must be refetched, not served short.

    pandas reads these with on_bad_lines="skip", so corrupt values never reach
    an analysis — but the drop is silent, and a damaged file would serve fewer
    bars forever. A crash on 2026-09-20 produced exactly this: one cache had
    two rows merged onto one line (a date sitting in a price column) and
    another had a fragment starting mid-number.
    """

    def _write(self, tmp_path, lines: list[str]):
        f = tmp_path / "X.NS-YFin-data-2021-01-01-2026-01-01.csv"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(f)

    _GOOD = [
        "Date,Close,High,Low,Open,Volume",
        "2026-01-02,10,11,9,10,100",
        "2026-01-05,11,12,10,11,110",
    ]

    def test_an_intact_cache_is_not_flagged(self, tmp_path):
        path = self._write(tmp_path, self._GOOD)
        cached = pd.read_csv(path, on_bad_lines="skip")
        assert su._cache_is_damaged(path, cached) is False

    def test_a_merged_row_is_detected(self, tmp_path):
        """The real ADANIPORTS shape: two rows on one line, 10 columns."""
        path = self._write(tmp_path, [
            *self._GOOD,
            "2026-01-06,12,13,11,2026-01-07,12,13,11,12,120",
        ])
        cached = pd.read_csv(path, on_bad_lines="skip")
        assert su._cache_is_damaged(path, cached) is True

    def test_trailing_blank_lines_are_not_mistaken_for_damage(self, tmp_path):
        path = self._write(tmp_path, [*self._GOOD, "", "  "])
        cached = pd.read_csv(path, on_bad_lines="skip")
        assert su._cache_is_damaged(path, cached) is False

    def test_a_missing_file_is_treated_as_damaged(self, tmp_path):
        assert su._cache_is_damaged(str(tmp_path / "nope.csv"), pd.DataFrame()) is True


class TestCacheWriteIsAtomic:
    """A killed or concurrent write must never leave a half-written cache.

    The self-heal above cleans up after damage; this stops the damage. On
    2026-09-24 a run was killed mid-flight and M&M.NS's next analysis failed
    with "unconverted data remains: ," — a date read out of a truncated row.
    """

    def test_a_crash_during_the_write_leaves_the_previous_cache_intact(self, tmp_path,
                                                                       monkeypatch):
        cache = tmp_path / "X.NS-YFin-data.csv"
        good = "Date,Close\n2026-01-02,10\n"
        cache.write_text(good, encoding="utf-8")

        def die(*a, **k):
            raise OSError("process killed mid-write")

        monkeypatch.setattr(su.atomic_write_text.__globals__["os"], "replace", die)
        with pytest.raises(OSError):
            su.atomic_write_text(cache, "Date,Close\n2026-01-02,10\n2026-01-05,11\n")

        assert cache.read_text(encoding="utf-8") == good
        assert [p.name for p in tmp_path.iterdir()] == [cache.name]  # no temp left

    def test_a_completed_write_replaces_the_file_whole(self, tmp_path):
        cache = tmp_path / "X.NS-YFin-data.csv"
        cache.write_text("Date,Close\n2026-01-02,10\n", encoding="utf-8")
        su.atomic_write_text(cache, "Date,Close\n2026-01-06,12\n")
        assert cache.read_text(encoding="utf-8") == "Date,Close\n2026-01-06,12\n"

    def test_the_loader_writes_its_cache_atomically(self):
        """Guard against a future edit going back to a plain to_csv."""
        source = Path(su.__file__).read_text(encoding="utf-8")
        assert "atomic_write_text(Path(data_file)" in source
        assert "downloaded.to_csv(data_file" not in source
