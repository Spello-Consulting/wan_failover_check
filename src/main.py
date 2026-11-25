"""Main module for the WAN Failover Check app.

Determines whether the network is on the primary (NBN) or backup (5G) WAN connection by
matching the reverse DNS name of the public IP address against configured patterns.
"""

import argparse
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


def build_monitor(config: SCConfigManager, logger: SCLogger, send_heartbeat: bool) -> WANMonitor:
    """Create the WAN monitor from the configuration.

    Args:
        config: The configuration manager.
        logger: The application logger.
        send_heartbeat: Whether the monitor should ping the heartbeat monitor.

    Returns:
        The configured monitor.
    """
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


def run_monitor(monitor: WANMonitor, api: StatusAPIServer | None, logger: SCLogger, stop_event: Event) -> int:
    """Run the monitor loop (and status API, if enabled) until stop_event is set.

    Args:
        monitor: The WAN monitor.
        api: The status API server, or None if disabled.
        logger: The application logger.
        stop_event: Set by the signal handlers to shut down.

    Returns:
        The process exit code.
    """
    thread_manager = ThreadManager(logger, global_stop=stop_event, before_exit=lambda: report_fatal_heartbeat(logger))
    thread_manager.add(name="wan monitor", target=monitor.run, args=(stop_event,), restart=RestartPolicy(mode="never"))
    if api is not None:
        thread_manager.add(
            name="status api",
            target=api.serve,
            args=(stop_event,),
            restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=5.0),
        )
        logger.log_message(f"Status API listening on {api.url}", "summary")
    else:
        logger.log_message("Status API disabled; set StatusAPI.Enable to enable it.", "summary")

    if not monitor.send_heartbeat:
        logger.log_message("Heartbeats disabled.", "summary")
    logger.log_message(
        f"Monitoring started. Primary WAN match: {', '.join(monitor.detector.primary_patterns)}  Interval: {monitor.check_interval:g}s",
        "summary",
    )

    thread_manager.start_all()
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    finally:
        thread_manager.stop_all()
        thread_manager.join_all(timeout_per_thread=10.0)
        logger.log_message(f"{APP_NAME} stopped.", "summary")
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

    return run_monitor(monitor, build_status_api(config, monitor.status_file), logger, stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
