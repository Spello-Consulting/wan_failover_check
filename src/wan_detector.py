"""Determine which WAN connection is active from the reverse DNS name of the public IP.

The public IP is found through IP-echo services. Its PTR (reverse DNS) record is then looked
up and matched against glob patterns such as ``*.aussiebb.net``. A match means the primary
WAN connection is active; anything else (including no PTR record at all) means the backup.
"""

import fnmatch
import socket
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from enum import StrEnum

from sc_foundation import SCCommon

NO_INTERNET_LABEL = "No internet"
DNS_FAILED_LABEL = "DNS lookup failed"

# h_errno values (netdb.h) that socket.herror carries when the address simply has no PTR record.
_HOST_NOT_FOUND = 1
_NO_DATA = 4


class WANState(StrEnum):
    """The outcome of a WAN check."""

    PRIMARY = "primary"
    BACKUP = "backup"
    NO_INTERNET = "no_internet"
    DNS_FAILED = "dns_failed"


@dataclass(frozen=True)
class WANStatus:
    """The result of a single WAN check.

    Attributes:
        state: Which state was detected.
        label: Human-readable state label, as written to status.json.
        external_ip: The public IP address, or None if it could not be determined.
        reverse_dns: The PTR name of the public IP (lower-case, no trailing dot), or None.
    """

    state: WANState
    label: str
    external_ip: str | None = None
    reverse_dns: str | None = None

    @property
    def on_primary(self) -> bool:
        """True when the primary WAN connection is active."""
        return self.state == WANState.PRIMARY

    @property
    def is_error(self) -> bool:
        """True when the WAN state could not be determined."""
        return self.state in {WANState.NO_INTERNET, WANState.DNS_FAILED}


class ReverseDNSError(Exception):
    """Raised when the reverse DNS lookup fails for a reason other than a missing PTR record."""


def normalise_hostname(hostname: str) -> str:
    """Normalise a DNS name for comparison.

    Args:
        hostname: The DNS name, possibly with a trailing dot and mixed case.

    Returns:
        The name in lower case with any trailing dot removed.
    """
    return hostname.strip().rstrip(".").lower()


def matches_primary(hostname: str | None, patterns: Sequence[str]) -> bool:
    """Check whether a reverse DNS name matches any of the primary WAN patterns.

    Args:
        hostname: The PTR name to check, or None if there was no PTR record.
        patterns: Glob patterns, e.g. ``["*.aussiebb.net"]``. Matching is case-insensitive.

    Returns:
        True if hostname matches at least one pattern.
    """
    if not hostname:
        return False
    name = normalise_hostname(hostname)
    return any(fnmatch.fnmatchcase(name, normalise_hostname(pattern)) for pattern in patterns)


def reverse_dns_lookup(ip_address: str, timeout: float) -> str | None:
    """Look up the PTR name for an IP address.

    Args:
        ip_address: The IPv4 or IPv6 address to look up.
        timeout: Maximum number of seconds to wait for the resolver.

    Returns:
        The normalised PTR name, or None if the address has no PTR record.

    Raises:
        ReverseDNSError: If the resolver failed or timed out.
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rdns")
    try:
        future = executor.submit(socket.gethostbyaddr, ip_address)
        hostname, _aliases, _addresses = future.result(timeout=timeout)
    except FutureTimeoutError as e:
        msg = f"Reverse DNS lookup for {ip_address} timed out after {timeout}s"
        raise ReverseDNSError(msg) from e
    except socket.herror as e:
        if e.errno in {_HOST_NOT_FOUND, _NO_DATA}:
            return None
        msg = f"Reverse DNS lookup for {ip_address} failed: {e}"
        raise ReverseDNSError(msg) from e
    except OSError as e:  # socket.gaierror and friends: resolver unavailable, bad address, etc.
        msg = f"Reverse DNS lookup for {ip_address} failed: {e}"
        raise ReverseDNSError(msg) from e
    finally:
        # Don't block on a hung resolver thread; it finishes in the background.
        executor.shutdown(wait=False)
    return normalise_hostname(hostname)


class WANDetector:
    """Classifies the current WAN connection as primary or backup."""

    def __init__(
        self,
        primary_patterns: Sequence[str],
        primary_label: str = "NBN (primary)",
        backup_label: str = "5G Backup",
        dns_timeout: float = 5.0,
        ip_lookup: Callable[[], str] | None = None,
        rdns_lookup: Callable[[str, float], str | None] | None = None,
    ) -> None:
        """Create a detector.

        Args:
            primary_patterns: Glob patterns that identify the primary WAN by its PTR name.
            primary_label: Label used when on the primary WAN.
            backup_label: Label used when on the backup WAN.
            dns_timeout: Seconds allowed for the reverse DNS lookup.
            ip_lookup: Returns the public IP, raising RuntimeError on failure. Defaults to
                SCCommon.get_external_ip.
            rdns_lookup: Reverse DNS lookup function. Defaults to reverse_dns_lookup.

        Raises:
            ValueError: If no primary patterns are given.
        """
        if not primary_patterns:
            msg = "At least one primary WAN match pattern is required."
            raise ValueError(msg)
        self.primary_patterns = list(primary_patterns)
        self.primary_label = primary_label
        self.backup_label = backup_label
        self.dns_timeout = dns_timeout
        self._ip_lookup: Callable[[], str] = ip_lookup or SCCommon.get_external_ip
        self._rdns_lookup = rdns_lookup or reverse_dns_lookup
        self.last_error: str | None = None

    def check(self) -> WANStatus:
        """Run one WAN check.

        Returns:
            The detected WAN status. When the state could not be determined, last_error holds
            the reason.
        """
        self.last_error = None
        try:
            ip_address = str(self._ip_lookup()).strip()
        except RuntimeError as e:
            self.last_error = str(e)
            return WANStatus(WANState.NO_INTERNET, NO_INTERNET_LABEL)

        try:
            hostname = self._rdns_lookup(ip_address, self.dns_timeout)
        except ReverseDNSError as e:
            self.last_error = str(e)
            return WANStatus(WANState.DNS_FAILED, DNS_FAILED_LABEL, external_ip=ip_address)

        if matches_primary(hostname, self.primary_patterns):
            return WANStatus(WANState.PRIMARY, self.primary_label, ip_address, hostname)
        return WANStatus(WANState.BACKUP, self.backup_label, ip_address, hostname)
