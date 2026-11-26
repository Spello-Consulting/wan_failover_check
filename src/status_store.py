"""Read and write the status.json file that holds the latest WAN state."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

from sc_foundation import DateHelper, JSONEncoder

from wan_detector import WANStatus

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def build_status_payload(status: WANStatus, now: dt.datetime | None = None) -> dict[str, Any]:
    """Build the dictionary written to status.json.

    Args:
        status: The WAN status to record.
        now: The time of the check. Defaults to the current time.

    Returns:
        The status payload. The timestamp is a UTC string, so no JSONEncoder type hints are added.
    """
    observed_at = now if now is not None else DateHelper.now_utc()
    return {
        "timestamp": observed_at.astimezone(dt.UTC).strftime(UTC_TIMESTAMP_FORMAT),
        "on_primary": status.on_primary,
        "status": status.label,
        "external_ip": status.external_ip,
        "reverse_dns": status.reverse_dns,
    }


def write_status(path: Path, status: WANStatus, now: dt.datetime | None = None) -> None:
    """Atomically write the WAN status to the status file.

    Args:
        path: The status file path.
        status: The WAN status to record.
        now: The time of the check. Defaults to the current time.

    Raises:
        RuntimeError: If the file could not be written.
    """  # noqa: DOC502
    JSONEncoder.save_to_file(build_status_payload(status, now), path)


def read_status(path: Path) -> dict[str, Any]:
    """Read the status file as plain JSON.

    The standard json module is used rather than JSONEncoder.read_from_file, which would turn the
    ISO timestamp string back into a datetime that the status API could not serialise.

    Args:
        path: The status file path.

    Returns:
        The status payload.

    Raises:
        RuntimeError: If the file is missing, unreadable or not valid JSON.
        TypeError: If the file does not hold a JSON object.
    """
    if not path.exists():
        msg = f"Status file {path} does not exist yet."
        raise RuntimeError(msg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        msg = f"Failed to read status file {path}: {e}"
        raise RuntimeError(msg) from e
    if not isinstance(data, dict):
        msg = f"Status file {path} does not contain a JSON object."
        raise TypeError(msg)
    return data
