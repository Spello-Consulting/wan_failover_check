"""Tests for src/wan_monitor.py."""

import json
import threading
from unittest.mock import MagicMock

import pytest

from failover_history import FailoverHistory
from wan_detector import WANDetector
from wan_monitor import WANMonitor

PRIMARY_PTR = "1-2-3-4.b4962b.syd.aussiebb.net"
BACKUP_PTR = "cpe.mobile.example.net"


class FakeLookups:
    """Scriptable IP and reverse DNS lookups."""

    def __init__(self):
        self.ip_error = None
        self.hostname = PRIMARY_PTR

    def ip(self):
        if self.ip_error:
            raise self.ip_error
        return "1.2.3.4"

    def rdns(self, _ip, _timeout):
        return self.hostname


@pytest.fixture
def lookups():
    return FakeLookups()


@pytest.fixture
def logger():
    mock = MagicMock()
    mock.ping_heartbeat.return_value = True
    return mock


@pytest.fixture
def monitor(tmp_path, lookups, logger):
    detector = WANDetector(["*.aussiebb.net"], ip_lookup=lookups.ip, rdns_lookup=lookups.rdns)
    return WANMonitor(
        detector=detector,
        status_file=tmp_path / "status.json",
        history=FailoverHistory(tmp_path / "history.csv"),
        logger=logger,
        check_interval=0.01,
        send_heartbeat=True,
    )


def _status(monitor):
    return json.loads(monitor.status_file.read_text(encoding="utf-8"))


def test_primary_check(monitor, logger):
    status, heartbeat_ok = monitor.check_once()

    assert status.on_primary
    assert heartbeat_ok
    assert _status(monitor)["status"] == "NBN (primary)"
    assert _status(monitor)["reverse_dns"] == PRIMARY_PTR
    assert monitor.history.load_rows() == []
    logger.ping_heartbeat.assert_called_once_with(is_fail=False)


def test_backup_check_opens_failover_and_sends_fail_heartbeat(monitor, lookups, logger):
    lookups.hostname = BACKUP_PTR

    status, _ = monitor.check_once()

    assert not status.on_primary
    assert _status(monitor)["status"] == "5G Backup"
    assert len(monitor.history.load_rows()) == 1
    logger.ping_heartbeat.assert_called_once_with(is_fail=True)


def test_return_to_primary_closes_failover(monitor, lookups):
    lookups.hostname = BACKUP_PTR
    monitor.check_once()
    lookups.hostname = PRIMARY_PTR

    monitor.check_once()

    assert monitor.history.load_rows()[0]["End Time"]


def test_no_internet_updates_status_only(monitor, lookups, logger):
    lookups.ip_error = RuntimeError("All IP-echo services failed.")

    status, heartbeat_ok = monitor.check_once()

    assert status.is_error
    assert heartbeat_ok
    assert _status(monitor) | {"timestamp": None} == {
        "timestamp": None,
        "on_primary": False,
        "status": "No internet",
        "external_ip": None,
        "reverse_dns": None,
    }
    assert not monitor.history.csv_path.exists()
    logger.ping_heartbeat.assert_not_called()


def test_heartbeat_override_disables_ping(monitor, logger):
    monitor.check_once(send_heartbeat=False)

    logger.ping_heartbeat.assert_not_called()


def test_heartbeat_failure_reported(monitor, logger):
    logger.ping_heartbeat.return_value = False

    _, heartbeat_ok = monitor.check_once()

    assert heartbeat_ok is False


def test_history_error_is_logged_not_raised(monitor, lookups, logger):
    lookups.hostname = BACKUP_PTR
    monitor.history.csv_path.write_text("Wrong,Headers\n", encoding="utf-8")

    status, _ = monitor.check_once()

    assert not status.on_primary
    assert any("Failed to read failover history" in call.args[0] for call in logger.log_message.call_args_list)


def test_run_logs_transitions_and_stops(monitor, lookups, logger):
    stop_event = threading.Event()
    checks = []
    original = monitor.check_once

    def counting_check():
        checks.append(1)
        if len(checks) == 2:
            lookups.hostname = BACKUP_PTR
        if len(checks) >= 3:
            stop_event.set()
        return original()

    monitor.check_once = counting_check

    monitor.run(stop_event)

    transitions = [call.args[0] for call in logger.log_message.call_args_list if call.args[0].startswith("WAN state is now")]
    assert len(transitions) == 2
    assert "NBN (primary)" in transitions[0]
    assert "5G Backup" in transitions[1]


def test_run_backs_off_on_errors(monitor, lookups):
    lookups.ip_error = RuntimeError("down")
    stop_event = MagicMock()
    stop_event.is_set.side_effect = [False, False, False, False, True]

    monitor.run(stop_event)

    timeouts = [call.kwargs["timeout"] for call in stop_event.wait.call_args_list]
    assert timeouts == pytest.approx([0.01, 0.02, 0.03, 0.03])
