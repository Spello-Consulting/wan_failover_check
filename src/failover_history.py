"""Record periods on the backup WAN connection in a CSV file.

Each row is one failover period. A row is opened (Start Time only) when the backup WAN is first
seen and closed (End Time and Duration Hours filled in) when the primary WAN returns. Times are
local, formatted ``yyyy-mm-dd hh:mm:ss``.
"""

import datetime as dt
from enum import StrEnum
from pathlib import Path
from typing import Any

from sc_foundation import CSVReader, DateHelper

START_TIME = "Start Time"
END_TIME = "End Time"
DURATION_HOURS = "Duration Hours"
HEADERS = [START_TIME, END_TIME, DURATION_HOURS]
LOCAL_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class FailoverEvent(StrEnum):
    """A change made to the failover history."""

    OPENED = "opened"
    CLOSED = "closed"


class FailoverHistory:
    """Maintains the failover history CSV file."""

    def __init__(self, csv_path: Path) -> None:
        """Create the history manager.

        Args:
            csv_path: Path to the CSV file. It is created on the first failover.
        """
        self.csv_path = csv_path

    def _reader(self) -> CSVReader:
        # All columns are strings: an open row has empty End Time / Duration Hours, which a typed
        # datetime/float column would reject. A fresh reader each time, because read_csv() mutates
        # the reader's header config.
        return CSVReader(self.csv_path, header_config=[{"name": name, "type": "str"} for name in HEADERS])

    def load_rows(self) -> list[dict[str, Any]]:
        """Read all rows from the history file.

        Returns:
            The rows, or an empty list if the file does not exist yet.

        Raises:
            RuntimeError: If the file cannot be read or is malformed.
        """  # noqa: DOC502
        return self._read(self._reader())

    def update(self, on_primary: bool, observed_at: dt.datetime | None = None) -> FailoverEvent | None:
        """Open or close a failover period based on the current WAN state.

        Args:
            on_primary: True if the primary WAN is active.
            observed_at: When the state was observed. Defaults to now (local time).

        Returns:
            The change made, or None if nothing changed.

        Raises:
            RuntimeError: If the history file cannot be read or written.
        """  # noqa: DOC502
        timestamp = (observed_at or DateHelper.now()).replace(microsecond=0)
        reader = self._reader()
        rows = self._read(reader)
        open_index = next((i for i, row in enumerate(rows) if not str(row.get(END_TIME) or "").strip()), None)

        if not on_primary:
            if open_index is not None:
                return None
            rows.append({START_TIME: DateHelper.format(timestamp, LOCAL_TIMESTAMP_FORMAT), END_TIME: "", DURATION_HOURS: ""})
            self._write(reader, rows)
            return FailoverEvent.OPENED

        if open_index is None:
            return None

        row = rows[open_index]
        row[END_TIME] = DateHelper.format(timestamp, LOCAL_TIMESTAMP_FORMAT)
        row[DURATION_HOURS] = self._duration_hours(str(row.get(START_TIME) or ""), timestamp)
        self._write(reader, rows)
        return FailoverEvent.CLOSED

    @staticmethod
    def _duration_hours(start_text: str, end: dt.datetime) -> str:
        """Calculate the failover duration in hours.

        Args:
            start_text: The Start Time column value.
            end: The end of the failover period.

        Returns:
            The duration formatted to two decimal places, or an empty string if start_text is invalid.
        """
        try:
            # hide_tz: DateHelper would otherwise attach today's fixed UTC offset, which is wrong
            # for a start time on the other side of a daylight-saving change.
            start = DateHelper.extract_datetime(start_text, LOCAL_TIMESTAMP_FORMAT, hide_tz=True)
        except ValueError:
            return ""
        # Start Time is naive local time; astimezone() attaches the local offset in force at that
        # time, so a period spanning a daylight-saving change is measured correctly.
        if end.tzinfo is not None:
            start = start.astimezone()
        hours = (end - start).total_seconds() / 3600
        return f"{hours:.2f}"

    def _read(self, reader: CSVReader) -> list[dict[str, Any]]:
        try:
            rows = reader.read_csv()
        except (ImportError, ValueError, TypeError, OSError) as e:
            msg = f"Failed to read failover history {self.csv_path}: {e}"
            raise RuntimeError(msg) from e
        return list(rows) if rows else []

    def _write(self, reader: CSVReader, rows: list[dict[str, Any]]) -> None:
        try:
            reader.write_csv(rows)
        except (ValueError, OSError) as e:
            msg = f"Failed to write failover history {self.csv_path}: {e}"
            raise RuntimeError(msg) from e
