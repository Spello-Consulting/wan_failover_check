"""Main module for the WAN Failover Check app.

Determines whether the network is on the primary (NBN) or backup (5G) WAN connection by
matching the reverse DNS name of the public IP address against configured patterns.
"""

import argparse
import datetime as dt
import os
import platform
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from dotenv import load_dotenv
from sc_foundation import (
    RestartPolicy,
    SCCommon,
    SCConfigManager,
    SCLogger,
    ThreadManager,
)

from config_schemas import ConfigSchema
from failover_history import FailoverHistory
from state_change_script import StateChangeScript, resolve_script_path
from status_api import StatusAPIServer
from wan_detector import WANDetector
from wan_monitor import WANMonitor

APP_NAME = "wan-failover-check"
CONFIG_FILE = "config.yaml"
DEFAULT_CHECK_INTERVAL = 60
DEFAULT_STATUS_FILE = "status.json"
DEFAULT_HISTORY_FILE = "failover_history.csv"
DEFAULT_API_HOST = "0.0.0.0"  # noqa: S104 - the status API is meant to be reachable on the LAN
DEFAULT_API_PORT = 8080
CONFIG_CHECK_INTERVAL = 5.0  # Seconds between checks of the config file for changes


@dataclass(frozen=True)
class CommandLineArgs:
    """Parsed command line arguments.

    Attributes:
        config_file: Path to the configuration file.
        homedir: Project home directory override, if given.
        onetime: Run one check and exit.
        onprimary: Run one check, print Yes/No and exit.
        noheartbeat: Disable heartbeats for this run.
    """

    config_file: str
    homedir: str | None = None
    onetime: bool = False
    onprimary: bool = False
    noheartbeat: bool = False


def build_parser() -> argparse.ArgumentParser:
    """Create the command line parser.

    Returns:
        The configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} - detect whether the network is on the primary or backup WAN connection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python src/main.py --config configs/development.yaml
  python src/main.py --config configs/development.yaml --onetime
  python src/main.py --config configs/production.yaml --onprimary
        """,
    )
    parser.add_argument("--homedir", type=str, metavar="PATH", help="Specify the project home directory")
    parser.add_argument("--config", type=str, metavar="FILE", help=f"Path to configuration file (default: {CONFIG_FILE})")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--onetime", action="store_true", help="Do one WAN check, write the status file, print the result, and exit.")
    mode_group.add_argument("--onprimary", action="store_true", help='Do one WAN check and print "Yes" when on the primary connection, otherwise "No".')
    parser.add_argument("--noheartbeat", action="store_true", help="Disable Better Stack heartbeats for this run.")
    return parser


def parse_command_line_args(argv: list[str] | None = None) -> CommandLineArgs:
    """Parse and validate command line arguments.

    Exits with status 1 if --homedir or --config point at something that does not exist.

    Args:
        argv: Arguments to parse. Defaults to sys.argv[1:].

    Returns:
        The parsed arguments, with config_file resolved to an absolute path when --config is given.
    """
    args = build_parser().parse_args(argv)

    if args.homedir:
        homedir = Path(args.homedir)
        if not homedir.exists():
            print(f"ERROR: Specified homedir does not exist: {args.homedir}", file=sys.stderr)
            sys.exit(1)
        if not homedir.is_dir():
            print(f"ERROR: Specified homedir is not a directory: {args.homedir}", file=sys.stderr)
            sys.exit(1)
        base_dir = homedir.resolve()
        os.environ["SC_FOUNDATION_PROJECT_ROOT"] = str(base_dir)
    else:
        base_dir = Path(SCCommon.get_project_root())

    if args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = base_dir / config_path
        config_path = config_path.resolve()
        if not config_path.exists():
            print(f"ERROR: Configuration file does not exist: {config_path}", file=sys.stderr)
            sys.exit(1)
        if not config_path.is_file():
            print(f"ERROR: Configuration path is not a file: {config_path}", file=sys.stderr)
            sys.exit(1)
        config_file = str(config_path)
    else:
        config_file = CONFIG_FILE

    return CommandLineArgs(
        config_file=config_file,
        homedir=str(base_dir) if args.homedir else None,
        onetime=args.onetime,
        onprimary=args.onprimary,
        noheartbeat=args.noheartbeat,
    )


def report_fatal_heartbeat(logger: SCLogger) -> None:
    """Report a fatal error to the heartbeat monitor.

    Args:
        logger: The logger instance used to send the heartbeat.
    """
    logger.ping_heartbeat(is_fail=True)


def get_heartbeat_config(config: SCConfigManager, enabled: bool) -> dict[str, Any] | None:
    """Return the HeartbeatMonitor section if heartbeats should be sent.

    Args:
        config: The configuration manager.
        enabled: False when heartbeats were disabled on the command line.

    Returns:
        The heartbeat config, or None when heartbeats are disabled.
    """
    heartbeat_config = config.get("HeartbeatMonitor")
    if not enabled or not isinstance(heartbeat_config, dict) or not heartbeat_config.get("Enable", False):
        return None
    return heartbeat_config


def initialize_config_and_logging(cmd_args: CommandLineArgs, quiet: bool = False) -> tuple[SCConfigManager, SCLogger]:
    """Initialize the configuration manager and logger.

    If either fails, the program exits with an error message.

    Args:
        cmd_args: Parsed command line arguments.
        quiet: Restrict console output to errors (on stderr), so stdout carries only the result.

    Returns:
        The configuration manager and logger.
    """
    try:
        config = SCConfigManager(config_file=cmd_args.config_file, validation_schema=ConfigSchema().validation)
    except RuntimeError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        logger_settings = config.get_logger_settings()
        if quiet:
            logger_settings["console_verbosity"] = "error"
        # --onetime / --onprimary never send heartbeats, so only monitor mode registers them.
        heartbeat_wanted = not (cmd_args.noheartbeat or cmd_args.onetime or cmd_args.onprimary)
        logger = SCLogger(logger_settings, heartbeat_config=get_heartbeat_config(config, heartbeat_wanted))
    except (RuntimeError, TypeError) as e:
        print(f"Logger initialisation error: {e}", file=sys.stderr)
        sys.exit(1)

    logger.log_message("", "summary")
    logger.log_message(f"{APP_NAME} starting.", "summary")
    logger.log_message(f"Configuration file: {cmd_args.config_file}", "debug")
    if cmd_args.homedir:
        logger.log_message(f"Home directory: {cmd_args.homedir}", "debug")

    email_settings = config.get_email_settings()
    if email_settings is not None:
        logger.register_email_settings(email_settings)

    if logger.get_fatal_error():
        logger.log_message("Application has recovered after a prior fatal failure.", "summary")
        logger.clear_fatal_error()
        logger.send_email(f"{APP_NAME} recovery", f"{APP_NAME} run was successful after a prior failure.")

    return config, logger


def resolve_data_file(file_name: str) -> Path:
    """Resolve a data file name from the config to an absolute path.

    Args:
        file_name: A bare file name (placed in the project root), a relative path, or an absolute path.

    Returns:
        The resolved path.
    """
    path = SCCommon.select_file_location(file_name, create_folder=True)
    return Path(path) if path is not None else Path(file_name).resolve()


def build_state_change_script(config: SCConfigManager) -> StateChangeScript | None:
    """Create the state change script runner if General.StateChangeScript is set.

    Args:
        config: The configuration manager.

    Returns:
        The script runner, or None if no script is configured.

    Raises:
        ValueError: If the configured script does not exist or is not executable.
    """  # noqa: DOC502
    path_text = config.get("General", "StateChangeScript", default=None)
    if not path_text or not str(path_text).strip():
        return None
    project_root = Path(SCCommon.get_project_root())
    return StateChangeScript(resolve_script_path(str(path_text).strip(), project_root), working_dir=project_root)


def build_monitor(config: SCConfigManager, logger: SCLogger, send_heartbeat: bool) -> WANMonitor:
    """Create the WAN monitor from the configuration.

    Args:
        config: The configuration manager.
        logger: The application logger.
        send_heartbeat: Whether the monitor should ping the heartbeat monitor.

    Returns:
        The configured monitor.

    Raises:
        ValueError: If the configured state change script is missing or not executable.
    """  # noqa: DOC502
    detector = WANDetector(
        primary_patterns=list(config.get("WAN", "PrimaryMatch", default=[])),
        primary_label=str(config.get("WAN", "PrimaryLabel", default=None) or "NBN (primary)"),
        backup_label=str(config.get("WAN", "BackupLabel", default=None) or "5G Backup"),
        dns_timeout=float(config.get("WAN", "DNSTimeout", default=None) or 5),
    )
    status_file = resolve_data_file(str(config.get("WAN", "StatusFile", default=None) or DEFAULT_STATUS_FILE))
    history_file = resolve_data_file(str(config.get("WAN", "FailoverHistoryFile", default=None) or DEFAULT_HISTORY_FILE))
    return WANMonitor(
        detector=detector,
        status_file=status_file,
        history=FailoverHistory(history_file),
        logger=logger,
        check_interval=float(config.get("General", "CheckInterval", default=None) or DEFAULT_CHECK_INTERVAL),
        send_heartbeat=send_heartbeat,
        state_change_script=build_state_change_script(config),
    )


def build_status_api(config: SCConfigManager, status_file: Path) -> StatusAPIServer | None:
    """Create the status API server if it is enabled in the configuration.

    Args:
        config: The configuration manager.
        status_file: The status file the API serves.

    Returns:
        The server, or None if the API is disabled.
    """
    if not config.get("StatusAPI", "Enable", default=False):
        return None
    host = str(config.get("StatusAPI", "Host", default=None) or DEFAULT_API_HOST)
    port = int(config.get("StatusAPI", "Port", default=None) or DEFAULT_API_PORT)
    return StatusAPIServer(status_file, host, port)


def run_single_check(monitor: WANMonitor, onprimary: bool) -> int:
    """Run one WAN check for --onetime or --onprimary and print the result.

    Args:
        monitor: The WAN monitor.
        onprimary: Print only "Yes" / "No" instead of the full status line.

    Returns:
        The process exit code: 0 on success, 1 if the WAN state could not be determined.
    """
    status, _ = monitor.check_once(send_heartbeat=False)
    if status.is_error:
        print(f"[error] {status.label}: {monitor.detector.last_error}", file=sys.stderr)
        return 1
    if onprimary:
        print("Yes" if status.on_primary else "No")
    else:
        print(f"[ok] {monitor.describe(status)}")
    return 0


def apply_logger_config(config: SCConfigManager, logger: SCLogger, heartbeat_config: dict[str, Any] | None) -> None:
    """Apply the Files, Email and HeartbeatMonitor sections to a running logger.

    Args:
        config: The configuration manager, already holding the new settings.
        logger: The logger to reconfigure in place.
        heartbeat_config: The heartbeat settings to use, or None to stop sending heartbeats.

    Raises:
        TypeError: If the email or heartbeat settings are not valid.
        ValueError: If required email settings are missing.
    """  # noqa: DOC502
    logger.initialise_settings(config.get_logger_settings())

    # SCLogger's register_* methods ignore empty or disabled settings, so clear those directly.
    email_settings = config.get_email_settings()
    if email_settings is None:
        logger.email_settings = None
    else:
        logger.register_email_settings(email_settings)

    if heartbeat_config is None:
        logger.heartbeat_config = {}
    else:
        logger.register_heartbeat_monitor(heartbeat_config)


class MonitorRunner:
    """Runs the WAN monitor and status API, and applies config file changes while they run.

    The main thread checks the config file every config_check_interval seconds. When the file
    changes, it is loaded and validated, a new monitor and status API are built from it and the
    logger is reconfigured, and only then are the running threads stopped and replaced. If the new
    file is invalid or the new monitor can't be built, the error is logged once and the app carries
    on with the settings it already has.
    """

    def __init__(
        self,
        config: SCConfigManager,
        logger: SCLogger,
        stop_event: Event,
        heartbeat_enabled: bool,
        config_check_interval: float = CONFIG_CHECK_INTERVAL,
    ) -> None:
        """Create the runner.

        Args:
            config: The configuration manager.
            logger: The application logger.
            stop_event: Set by the signal handlers to shut down.
            heartbeat_enabled: False when heartbeats were disabled on the command line.
            config_check_interval: Seconds between checks of the config file for changes.
        """
        self.config = config
        self.logger = logger
        self.stop_event = stop_event
        self.heartbeat_enabled = heartbeat_enabled
        self.config_check_interval = config_check_interval
        self.monitor: WANMonitor | None = None
        self.api: StatusAPIServer | None = None
        self._thread_manager: ThreadManager | None = None
        self._config_modified = config.get_config_file_last_modified() or dt.datetime.now(tz=dt.UTC)

    def start(self, monitor: WANMonitor, api: StatusAPIServer | None) -> None:
        """Start the monitor (and status API, if enabled) in managed threads.

        Args:
            monitor: The WAN monitor.
            api: The status API server, or None if disabled.
        """
        # Each set of threads gets its own stop event, so a reload can stop them without
        # stopping the app.
        threads_stop = Event()
        logger = self.logger
        thread_manager = ThreadManager(logger, global_stop=threads_stop, before_exit=lambda: report_fatal_heartbeat(logger))
        thread_manager.add(name="wan monitor", target=monitor.run, args=(threads_stop,), restart=RestartPolicy(mode="never"))
        if api is not None:
            thread_manager.add(
                name="status api",
                target=api.serve,
                args=(threads_stop,),
                restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=5.0),
            )
            logger.log_message(f"Status API listening on {api.url}", "summary")
        else:
            logger.log_message("Status API disabled; set StatusAPI.Enable to enable it.", "summary")

        if not monitor.send_heartbeat:
            logger.log_message("Heartbeats disabled.", "summary")
        if monitor.state_change_script is not None:
            logger.log_message(f"State change script: {monitor.state_change_script.path}", "summary")
        logger.log_message(
            f"Monitoring started. Primary WAN match: {', '.join(monitor.detector.primary_patterns)}  Interval: {monitor.check_interval:g}s",
            "summary",
        )

        thread_manager.start_all()
        self.monitor = monitor
        self.api = api
        self._thread_manager = thread_manager

    def stop(self) -> None:
        """Stop the running threads and wait for them to finish."""
        if self._thread_manager is None:
            return
        self._thread_manager.stop_all()
        self._thread_manager.join_all(timeout_per_thread=10.0)
        self._thread_manager = None

    def reload_if_changed(self) -> bool:
        """Reload the config file if it has changed, and restart the threads with the new settings.

        Returns:
            True if new settings were loaded and applied, False if the file is unchanged or the new
            settings could not be used.
        """
        try:
            modified = self.config.check_for_config_changes(self._config_modified)
        except RuntimeError as e:
            # Wait for the next save rather than logging the same error on every check.
            self._config_modified = self.config.get_config_file_last_modified() or self._config_modified
            self.logger.log_message(f"Config file changed but could not be loaded; keeping the current settings. {e}", "error")
            return False
        if modified is None:
            return False
        self._config_modified = modified
        self.logger.log_message(f"Config file {self.config.config_path} changed; applying the new settings.", "summary")

        heartbeat_config = get_heartbeat_config(self.config, self.heartbeat_enabled)
        try:
            monitor = build_monitor(self.config, self.logger, send_heartbeat=heartbeat_config is not None)
            api = build_status_api(self.config, monitor.status_file)
            apply_logger_config(self.config, self.logger, heartbeat_config)
        except (ValueError, TypeError, RuntimeError) as e:
            self.logger.log_message(f"New config settings could not be applied; keeping the current settings. {e}", "error")
            return False

        self.stop()
        self.start(monitor, api)
        return True

    def run(self, monitor: WANMonitor) -> int:
        """Run the monitor and status API until stop_event is set, applying config file changes.

        Args:
            monitor: The WAN monitor built from the initial config.

        Returns:
            The process exit code.
        """
        self.start(monitor, build_status_api(self.config, monitor.status_file))
        try:
            while not self.stop_event.wait(timeout=self.config_check_interval):
                self.reload_if_changed()
        finally:
            self.stop()
            self.logger.log_message(f"{APP_NAME} stopped.", "summary")
        return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point.

    Args:
        argv: Command line arguments. Defaults to sys.argv[1:].

    Returns:
        The process exit code.
    """
    load_dotenv()  # Load .env file if present (no-op if absent)
    cmd_args = parse_command_line_args(argv)
    quiet = cmd_args.onprimary
    if not quiet:
        print(f"Starting {APP_NAME} on {platform.system()}")

    config, logger = initialize_config_and_logging(cmd_args, quiet=quiet)

    try:
        monitor = build_monitor(config, logger, send_heartbeat=bool(logger.heartbeat_config))
    except (ValueError, TypeError, RuntimeError) as e:
        logger.log_fatal_error(f"Startup error: {e}", exit_app=False)
        return 1

    if cmd_args.onetime or cmd_args.onprimary:
        return run_single_check(monitor, onprimary=cmd_args.onprimary)

    # Monitor mode: SIGINT (Ctrl-C) and SIGTERM (systemctl stop) shut down gracefully.
    stop_event = Event()

    def handle_signal(_sig: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    runner = MonitorRunner(config, logger, stop_event, heartbeat_enabled=not cmd_args.noheartbeat, config_check_interval=CONFIG_CHECK_INTERVAL)
    return runner.run(monitor)


if __name__ == "__main__":
    raise SystemExit(main())
