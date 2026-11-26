"""Run WAN checks, record the results and send heartbeats."""

import threading
from pathlib import Path

from sc_foundation import SCLogger

from failover_history import FailoverEvent, FailoverHistory
from state_change_script import StateChangeScript
from status_store import write_status
from wan_detector import WANDetector, WANStatus

MAX_BACKOFF_MULTIPLIER = 3


class WANMonitor:
    """Performs WAN checks and records their outcome."""

    def __init__(
        self,
        detector: WANDetector,
        status_file: Path,
        history: FailoverHistory,
        logger: SCLogger,
        check_interval: float = 60,
        send_heartbeat: bool = True,
        state_change_script: StateChangeScript | None = None,
    ) -> None:
        """Create the monitor.

        Args:
            detector: Classifies the WAN connection.
            status_file: Where to write the latest status.
            history: The failover history CSV manager.
            logger: Application logger, also used for Better Stack heartbeats.
            check_interval: Seconds between checks in monitor mode.
            send_heartbeat: Whether to ping the heartbeat monitor after each check.
            state_change_script: Script to run when the WAN moves between primary and backup.
        """
        self.detector = detector
        self.status_file = status_file
        self.history = history
        self.logger = logger
        self.check_interval = check_interval
        self.send_heartbeat = send_heartbeat
        self.state_change_script = state_change_script
        self.last_status: WANStatus | None = None

    def check_once(self, send_heartbeat: bool | None = None) -> tuple[WANStatus, bool]:
        """Run one WAN check, update the status file and failover history, and send a heartbeat.

        When the state cannot be determined (no internet or DNS failure), only the status file is
        updated, so the status API never serves stale data and no false failover is recorded.

        The state change script runs when the failover history opens a period (primary to backup)
        or closes one (backup to primary). The history file holds the last known state, so a change
        is acted on once, even across restarts and --onetime / --onprimary runs.

        Args:
            send_heartbeat: Overrides the monitor's heartbeat setting for this check.

        Returns:
            The WAN status, and False if a heartbeat was attempted and failed (True otherwise).
        """
        status = self.detector.check()
        if status.is_error:
            self.logger.log_message(f"WAN check failed: {self.detector.last_error}", "error")

        try:
            write_status(self.status_file, status)
        except RuntimeError as e:
            self.logger.log_message(f"Failed to write status file: {e}", "error")

        if status.is_error:
            return status, True

        try:
            event = self.history.update(status.on_primary)
        except RuntimeError as e:
            self.logger.log_message(str(e), "error")
        else:
            if event == FailoverEvent.OPENED:
                self.logger.log_message("Failover period started in history file.", "detailed")
            elif event == FailoverEvent.CLOSED:
                self.logger.log_message("Failover period closed in history file.", "detailed")
            if event is not None:
                self.run_state_change_script(status)

        heartbeat_ok = True
        if self.send_heartbeat if send_heartbeat is None else send_heartbeat:
            heartbeat_ok = bool(self.logger.ping_heartbeat(is_fail=not status.on_primary))
        return status, heartbeat_ok

    def run_state_change_script(self, status: WANStatus) -> None:
        """Run the state change script, if configured, and log the outcome.

        Failures are logged and never stop the monitor.

        Args:
            status: The new WAN status.
        """
        script = self.state_change_script
        if script is None:
            return
        self.logger.log_message(f"Running state change script: {' '.join(script.command(status.on_primary))}", "summary")
        try:
            result = script.run(status)
        except RuntimeError as e:
            self.logger.log_message(str(e), "error")
            return
        output = result.stdout.strip()
        errors = result.stderr.strip()
        if result.returncode != 0:
            detail = f": {errors}" if errors else ""
            self.logger.log_message(f"State change script exited with code {result.returncode}{detail}", "error")
        else:
            self.logger.log_message("State change script finished.", "detailed")
            if errors:
                self.logger.log_message(f"State change script stderr: {errors}", "warning")
        if output:
            self.logger.log_message(f"State change script output: {output}", "detailed")

    @staticmethod
    def describe(status: WANStatus) -> str:
        """Format a status for log output.

        Args:
            status: The WAN status.

        Returns:
            A one-line description.
        """
        return f"{status.label}  ext-ip={status.external_ip}  rdns={status.reverse_dns}"

    def run(self, stop_event: threading.Event) -> None:
        """Check the WAN repeatedly until stop_event is set.

        Consecutive failures (undetermined state or failed heartbeat) back the interval off to
        2x and then 3x, where it holds until a check succeeds.

        Args:
            stop_event: Set to stop the loop.
        """
        consecutive_errors = 0
        while not stop_event.is_set():
            status, heartbeat_ok = self.check_once()

            if self.last_status is None or status.state != self.last_status.state:
                self.logger.log_message(f"WAN state is now {self.describe(status)}", "summary")
            elif not status.is_error:
                self.logger.log_message(self.describe(status), "detailed")
            self.last_status = status

            if status.is_error or not heartbeat_ok:
                consecutive_errors += 1
            else:
                consecutive_errors = 0

            multiplier = min(MAX_BACKOFF_MULTIPLIER, max(1, consecutive_errors))
            stop_event.wait(timeout=self.check_interval * multiplier)
