"""Tests for src/wan_detector.py."""

import socket
import time

import pytest

import wan_detector
from wan_detector import (
    DNS_FAILED_LABEL,
    NO_INTERNET_LABEL,
    ReverseDNSError,
    WANDetector,
    WANState,
    matches_primary,
    normalise_hostname,
    reverse_dns_lookup,
)

AUSSIE_PTR = "180-150-43-236.b4962b.syd.static.aussiebb.net"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Host.Example.NET.", "host.example.net"),
        ("  host.example.net  ", "host.example.net"),
        ("host.example.net", "host.example.net"),
    ],
)
def test_normalise_hostname(raw, expected):
    assert normalise_hostname(raw) == expected


@pytest.mark.parametrize(
    ("hostname", "patterns", "expected"),
    [
        (AUSSIE_PTR, ["*.aussiebb.net"], True),
        (AUSSIE_PTR + ".", ["*.aussiebb.net"], True),
        (AUSSIE_PTR.upper(), ["*.aussiebb.net"], True),
        (AUSSIE_PTR, ["*.AussieBB.net"], True),
        ("1-2-3-4.dyn.syd.aussiebb.net", ["*.aussiebb.net"], True),
        ("aussiebb.net", ["*.aussiebb.net"], False),
        ("host.notaussiebb.net", ["*.aussiebb.net"], False),
        ("cpe-1-2-3-4.mobile.telstra.net", ["*.aussiebb.net"], False),
        ("cpe-1-2-3-4.mobile.telstra.net", ["*.aussiebb.net", "*.telstra.net"], True),
        (None, ["*.aussiebb.net"], False),
        ("", ["*.aussiebb.net"], False),
    ],
)
def test_matches_primary(hostname, patterns, expected):
    assert matches_primary(hostname, patterns) is expected


def test_reverse_dns_lookup_returns_normalised_name(monkeypatch):
    monkeypatch.setattr(wan_detector.socket, "gethostbyaddr", lambda _ip: (AUSSIE_PTR.upper() + ".", [], ["1.2.3.4"]))

    assert reverse_dns_lookup("1.2.3.4", timeout=1) == AUSSIE_PTR


@pytest.mark.parametrize("h_errno", [1, 4])
def test_reverse_dns_lookup_no_ptr_returns_none(monkeypatch, h_errno):
    def _raise(_ip):
        raise socket.herror(h_errno, "Unknown host")

    monkeypatch.setattr(wan_detector.socket, "gethostbyaddr", _raise)

    assert reverse_dns_lookup("1.2.3.4", timeout=1) is None


@pytest.mark.parametrize(
    "error",
    [socket.herror(2, "Host name lookup failure"), socket.gaierror(8, "nodename nor servname provided"), OSError("boom")],
)
def test_reverse_dns_lookup_resolver_errors_raise(monkeypatch, error):
    def _raise(_ip):
        raise error

    monkeypatch.setattr(wan_detector.socket, "gethostbyaddr", _raise)

    with pytest.raises(ReverseDNSError):
        reverse_dns_lookup("1.2.3.4", timeout=1)


def test_reverse_dns_lookup_times_out(monkeypatch):
    def _slow(_ip):
        time.sleep(0.5)
        return (AUSSIE_PTR, [], [])

    monkeypatch.setattr(wan_detector.socket, "gethostbyaddr", _slow)

    with pytest.raises(ReverseDNSError, match="timed out"):
        reverse_dns_lookup("1.2.3.4", timeout=0.05)


def _detector(ip="1.2.3.4", hostname=AUSSIE_PTR, ip_error=None, rdns_error=None):
    def ip_lookup():
        if ip_error:
            raise ip_error
        return ip

    def rdns_lookup(_ip, _timeout):
        if rdns_error:
            raise rdns_error
        return hostname

    return WANDetector(["*.aussiebb.net"], ip_lookup=ip_lookup, rdns_lookup=rdns_lookup)


def test_check_primary():
    status = _detector().check()

    assert status.state == WANState.PRIMARY
    assert status.on_primary is True
    assert status.is_error is False
    assert status.label == "NBN (primary)"
    assert status.external_ip == "1.2.3.4"
    assert status.reverse_dns == AUSSIE_PTR


def test_check_backup_when_ptr_does_not_match():
    status = _detector(hostname="cpe-1-2-3-4.mobile.example.net").check()

    assert status.state == WANState.BACKUP
    assert status.on_primary is False
    assert status.label == "5G Backup"
    assert status.reverse_dns == "cpe-1-2-3-4.mobile.example.net"


def test_check_backup_when_no_ptr_record():
    status = _detector(hostname=None).check()

    assert status.state == WANState.BACKUP
    assert status.reverse_dns is None
    assert status.external_ip == "1.2.3.4"


def test_check_no_internet():
    detector = _detector(ip_error=RuntimeError("All IP-echo services failed."))

    status = detector.check()

    assert status.state == WANState.NO_INTERNET
    assert status.label == NO_INTERNET_LABEL
    assert status.is_error is True
    assert status.on_primary is False
    assert status.external_ip is None
    assert detector.last_error == "All IP-echo services failed."


def test_check_dns_failed():
    detector = _detector(rdns_error=ReverseDNSError("timed out"))

    status = detector.check()

    assert status.state == WANState.DNS_FAILED
    assert status.label == DNS_FAILED_LABEL
    assert status.is_error is True
    assert status.external_ip == "1.2.3.4"
    assert detector.last_error == "timed out"


def test_check_clears_last_error():
    calls = iter([RuntimeError("down"), None])

    def ip_lookup():
        error = next(calls)
        if error:
            raise error
        return "1.2.3.4"

    detector = WANDetector(["*.aussiebb.net"], ip_lookup=ip_lookup, rdns_lookup=lambda _ip, _t: AUSSIE_PTR)
    detector.check()
    detector.check()

    assert detector.last_error is None


def test_custom_labels():
    detector = WANDetector(["*.aussiebb.net"], primary_label="Fibre", backup_label="LTE", ip_lookup=lambda: "1.2.3.4", rdns_lookup=lambda _ip, _t: None)

    assert detector.check().label == "LTE"


def test_requires_patterns():
    with pytest.raises(ValueError, match="pattern"):
        WANDetector([])
