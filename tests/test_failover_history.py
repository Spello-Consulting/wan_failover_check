"""Tests for src/failover_history.py."""

import datetime as dt

import pytest

from failover_history import FailoverEvent, FailoverHistory

START = dt.datetime(2026, 4, 9, 10, 0, 0).astimezone()
END = dt.datetime(2026, 4, 9, 12, 30, 0).astimezone()


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_backup_opens_row_and_creates_file(tmp_path):
    path = tmp_path / "history.csv"
    history = FailoverHistory(path)

    assert history.update(on_primary=False, observed_at=START) == FailoverEvent.OPENED

    assert _lines(path) == ["Start Time,End Time,Duration Hours", "2026-04-09 10:00:00,,"]


def test_backup_does_not_duplicate_open_row(tmp_path):
    history = FailoverHistory(tmp_path / "history.csv")
    history.update(on_primary=False, observed_at=START)

    assert history.update(on_primary=False, observed_at=END) is None

    assert len(history.load_rows()) == 1


def test_primary_closes_open_row(tmp_path):
    path = tmp_path / "history.csv"
    history = FailoverHistory(path)
    history.update(on_primary=False, observed_at=START)

    assert history.update(on_primary=True, observed_at=END) == FailoverEvent.CLOSED

    assert _lines(path)[1] == "2026-04-09 10:00:00,2026-04-09 12:30:00,2.50"


def test_primary_without_open_row_does_nothing(tmp_path):
    path = tmp_path / "history.csv"
    history = FailoverHistory(path)

    assert history.update(on_primary=True, observed_at=END) is None
    assert not path.exists()


def test_new_failover_after_closed_one(tmp_path):
    history = FailoverHistory(tmp_path / "history.csv")
    history.update(on_primary=False, observed_at=START)
    history.update(on_primary=True, observed_at=END)

    assert history.update(on_primary=False, observed_at=END + dt.timedelta(hours=1)) == FailoverEvent.OPENED

    rows = history.load_rows()
    assert len(rows) == 2
    assert rows[1]["Start Time"] == "2026-04-09 13:30:00"


def test_reads_history_written_by_v1(tmp_path):
    """A file written by the 1.x app (header plus closed and open rows) is updated in place."""
    path = tmp_path / "history.csv"
    path.write_text(
        "Start Time,End Time,Duration Hours\n2026-04-01 08:00:00,2026-04-01 09:00:00,1.00\n2026-04-09 10:00:00,,\n",
        encoding="utf-8",
    )
    history = FailoverHistory(path)

    assert history.update(on_primary=True, observed_at=END) == FailoverEvent.CLOSED

    assert _lines(path) == [
        "Start Time,End Time,Duration Hours",
        "2026-04-01 08:00:00,2026-04-01 09:00:00,1.00",
        "2026-04-09 10:00:00,2026-04-09 12:30:00,2.50",
    ]


def test_header_only_file(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text("Start Time,End Time,Duration Hours\n", encoding="utf-8")

    assert FailoverHistory(path).update(on_primary=False, observed_at=START) == FailoverEvent.OPENED


def test_invalid_start_time_leaves_duration_blank(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text("Start Time,End Time,Duration Hours\nnot a date,,\n", encoding="utf-8")

    FailoverHistory(path).update(on_primary=True, observed_at=END)

    assert _lines(path)[1] == "not a date,2026-04-09 12:30:00,"


def test_naive_observed_at(tmp_path):
    history = FailoverHistory(tmp_path / "history.csv")
    history.update(on_primary=False, observed_at=dt.datetime(2026, 4, 9, 10, 0, 0))  # noqa: DTZ001

    history.update(on_primary=True, observed_at=dt.datetime(2026, 4, 9, 10, 45, 0))  # noqa: DTZ001

    assert history.load_rows()[0]["Duration Hours"] == "0.75"


def test_malformed_file_raises_runtime_error(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text("Wrong,Headers\n1,2\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Failed to read"):
        FailoverHistory(path).update(on_primary=False, observed_at=START)
