"""Tests for src/status_store.py."""

import datetime as dt
import json

import pytest

from status_store import build_status_payload, read_status, write_status
from wan_detector import NO_INTERNET_LABEL, WANState, WANStatus

PRIMARY = WANStatus(WANState.PRIMARY, "NBN (primary)", "1.2.3.4", "host.aussiebb.net")
NOW = dt.datetime(2026, 11, 26, 12, 30, 5, tzinfo=dt.timezone(dt.timedelta(hours=11)))


def test_build_status_payload_primary():
    assert build_status_payload(PRIMARY, NOW) == {
        "timestamp": "2026-11-26T01:30:05Z",
        "on_primary": True,
        "status": "NBN (primary)",
        "external_ip": "1.2.3.4",
        "reverse_dns": "host.aussiebb.net",
    }


def test_build_status_payload_no_internet():
    payload = build_status_payload(WANStatus(WANState.NO_INTERNET, NO_INTERNET_LABEL), NOW)

    assert payload["on_primary"] is False
    assert payload["status"] == "No internet"
    assert payload["external_ip"] is None
    assert payload["reverse_dns"] is None


def test_write_and_read_status(tmp_path):
    path = tmp_path / "status.json"

    write_status(path, PRIMARY, NOW)

    assert read_status(path) == build_status_payload(PRIMARY, NOW)
    # Plain JSON, with no JSONEncoder type-hint keys, so other tools can read it.
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"timestamp", "on_primary", "status", "external_ip", "reverse_dns"}


def test_write_status_overwrites(tmp_path):
    path = tmp_path / "status.json"
    write_status(path, PRIMARY, NOW)

    write_status(path, WANStatus(WANState.NO_INTERNET, NO_INTERNET_LABEL), NOW)

    assert read_status(path)["status"] == "No internet"


def test_read_status_missing_file(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        read_status(tmp_path / "status.json")


def test_read_status_invalid_json(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeError):
        read_status(path)


def test_read_status_not_an_object(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(TypeError):
        read_status(path)
