"""Tests for src/main.py."""

import json
import os
import signal
import socket
import textwrap
import threading
import time
import urllib.request

import pytest

import main
import wan_detector
from main import CONFIG_FILE, build_status_api, parse_command_line_args

PRIMARY_PTR = "1-2-3-4.b4962b.syd.aussiebb.net"

CONFIG = textwrap.dedent(
    """
    General:
      AppName: wan-failover-check
      CheckInterval: 30
      StateChangeScript: {script}
    WAN:
      PrimaryMatch:
        - "*.aussiebb.net"
      StatusFile: status.json
      FailoverHistoryFile: failover_history.csv
      DNSTimeout: 2
    StatusAPI:
      Enable: {api}
      Host: 127.0.0.1
      Port: 8099
    Files:
      LogfileName: logs/logfile.log
      LogfileVerbosity: debug
      ConsoleVerbosity: debug
    Email:
      EnableEmail: False
    HeartbeatMonitor:
      Enable: True
      WebsiteURL: https://example.invalid/heartbeat
      HeartbeatTimeout: 5
      Frequency: 30
    """
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temporary project root with a config file; cwd is the project root.

    Returns:
        The project root path.
    """
    monkeypatch.delenv("SC_FOUNDATION_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(CONFIG.format(api="True", script=""), encoding="utf-8")
    return tmp_path


@pytest.fixture
def lookups(monkeypatch):
    """Patch the network lookups.

    Returns:
        A dict of lookup results that the test can change.
    """
    state = {"ip": "1.2.3.4", "hostname": PRIMARY_PTR, "ip_error": None}

    def fake_ip():
        if state["ip_error"]:
            raise state["ip_error"]
        return state["ip"]

    monkeypatch.setattr(wan_detector.SCCommon, "get_external_ip", fake_ip)
    monkeypatch.setattr(wan_detector.socket, "gethostbyaddr", lambda _ip: (state["hostname"], [], [state["ip"]]))
    return state


def test_parse_command_line_args_defaults(monkeypatch, tmp_path):
    """With no arguments, the default config file and no homedir are returned."""
    monkeypatch.setattr("main.SCCommon.get_project_root", lambda: tmp_path)

    result = parse_command_line_args([])

    assert result.config_file == CONFIG_FILE
    assert result.homedir is None
    assert not result.onetime
    assert not result.onprimary
    assert not result.noheartbeat


def test_parse_command_line_args_with_valid_config(project):
    result = parse_command_line_args(["--homedir", str(project), "--config", "config.yaml", "--onetime", "--noheartbeat"])

    assert result.config_file == str((project / "config.yaml").resolve())
    assert result.homedir == str(project.resolve())
    assert result.onetime
    assert result.noheartbeat


def test_parse_command_line_args_missing_config_exits(project):
    with pytest.raises(SystemExit) as exc_info:
        parse_command_line_args(["--homedir", str(project), "--config", "missing.yaml"])

    assert exc_info.value.code == 1


def test_parse_command_line_args_missing_homedir_exits(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        parse_command_line_args(["--homedir", str(tmp_path / "does-not-exist")])

    assert exc_info.value.code == 1


def test_onetime_and_onprimary_are_mutually_exclusive(project):
    with pytest.raises(SystemExit):
        parse_command_line_args(["--homedir", str(project), "--onetime", "--onprimary"])


@pytest.mark.usefixtures("lookups")
def test_onprimary_prints_only_yes(project, capsys):
    exit_code = main.main(["--homedir", str(project), "--config", "config.yaml", "--onprimary"])

    assert exit_code == 0
    assert capsys.readouterr().out == "Yes\n"
    assert json.loads((project / "status.json").read_text(encoding="utf-8"))["on_primary"] is True


def test_onprimary_prints_no_on_backup(project, lookups, capsys):
    lookups["hostname"] = "cpe.mobile.example.net"

    exit_code = main.main(["--homedir", str(project), "--config", "config.yaml", "--onprimary"])

    assert exit_code == 0
    assert capsys.readouterr().out == "No\n"
    assert (project / "failover_history.csv").exists()


def test_onprimary_no_internet_exits_1_with_empty_stdout(project, lookups, capsys):
    lookups["ip_error"] = RuntimeError("All IP-echo services failed.")

    exit_code = main.main(["--homedir", str(project), "--config", "config.yaml", "--onprimary"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert not captured.out
    assert "No internet" in captured.err


@pytest.mark.usefixtures("lookups")
def test_onetime_prints_status(project, capsys):
    exit_code = main.main(["--homedir", str(project), "--config", "config.yaml", "--onetime"])

    assert exit_code == 0
    assert f"[ok] NBN (primary)  ext-ip=1.2.3.4  rdns={PRIMARY_PTR}" in capsys.readouterr().out


@pytest.mark.usefixtures("lookups")
def test_onetime_does_not_send_heartbeat(project, mocker):
    ping = mocker.patch("sc_foundation.sc_logging.requests.get")

    main.main(["--homedir", str(project), "--config", "config.yaml", "--onetime"])

    ping.assert_not_called()


@pytest.fixture
def restore_signal_handlers():
    """main() installs SIGINT/SIGTERM handlers; put pytest's back afterwards."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_monitor_mode_runs_and_stops_on_sigterm(project, lookups, mocker, restore_signal_handlers):  # noqa: ARG001
    """Monitor mode checks the WAN, serves the API, pings the heartbeat and exits 0 on SIGTERM."""
    with socket.socket() as port_holder:
        port_holder.bind(("127.0.0.1", 0))
        port = port_holder.getsockname()[1]
    (project / "config.yaml").write_text(CONFIG.format(api="True", script="").replace("Port: 8099", f"Port: {port}"), encoding="utf-8")
    heartbeat = mocker.patch("sc_foundation.sc_logging.requests.get")
    heartbeat.return_value.status_code = 200
    seen = {}

    def stop_when_running():
        # urllib, not requests: requests.get is mocked above for the heartbeat.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1) as response:
                    seen["api"] = json.loads(response.read())
                break
            except OSError:
                time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=stop_when_running, daemon=True).start()

    exit_code = main.main(["--homedir", str(project), "--config", "config.yaml"])

    assert exit_code == 0
    assert seen["api"]["status"] == "NBN (primary)"
    heartbeat.assert_called_with("https://example.invalid/heartbeat", timeout=5)


def test_build_status_api_disabled(project):
    (project / "config.yaml").write_text(CONFIG.format(api="False", script=""), encoding="utf-8")
    config, _ = main.initialize_config_and_logging(parse_command_line_args(["--homedir", str(project), "--config", "config.yaml"]))

    assert build_status_api(config, project / "status.json") is None


def test_invalid_config_exits(project):
    (project / "config.yaml").write_text(CONFIG.format(api="True", script="").replace('- "*.aussiebb.net"', ""), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main.main(["--homedir", str(project), "--config", "config.yaml", "--onetime"])

    assert exc_info.value.code == 1


def _with_script(project, script_text):
    (project / "config.yaml").write_text(CONFIG.format(api="True", script=script_text), encoding="utf-8")


def test_onprimary_runs_state_change_script_without_touching_stdout(project, lookups, capsys):
    (project / "scripts").mkdir()
    hook = project / "scripts" / "hook.sh"
    hook.write_text('#!/bin/sh\necho "$*" >> calls.txt\necho noisy\n', encoding="utf-8")
    hook.chmod(0o755)
    _with_script(project, "scripts/hook.sh")
    lookups["hostname"] = "cpe.mobile.example.net"

    assert main.main(["--homedir", str(project), "--config", "config.yaml", "--onprimary"]) == 0
    assert capsys.readouterr().out == "No\n"
    lookups["hostname"] = PRIMARY_PTR
    assert main.main(["--homedir", str(project), "--config", "config.yaml", "--onprimary"]) == 0
    assert capsys.readouterr().out == "Yes\n"

    assert (project / "calls.txt").read_text(encoding="utf-8").splitlines() == ["--on_primary false", "--on_primary true"]


@pytest.mark.usefixtures("lookups")
def test_missing_state_change_script_exits_1(project, capsys):
    _with_script(project, "scripts/missing.sh")

    exit_code = main.main(["--homedir", str(project), "--config", "config.yaml", "--onetime"])

    assert exit_code == 1
    assert "StateChangeScript" in capsys.readouterr().err


# ---- Config file reloading in monitor mode ----


def _write_config(project, *, api="False", script="", replacements=()):
    """Rewrite config.yaml and push its modified time forward so the change is always seen.

    Args:
        project: The project root.
        api: Value for StatusAPI.Enable.
        script: Value for General.StateChangeScript.
        replacements: (old, new) text replacements to apply to the config.
    """
    text = CONFIG.format(api=api, script=script)
    for old, new in replacements:
        text = text.replace(old, new)
    path = project / "config.yaml"
    previous = path.stat().st_mtime
    path.write_text(text, encoding="utf-8")
    os.utime(path, (previous + 10, previous + 10))


@pytest.fixture
def runner(project, lookups, mocker):  # noqa: ARG001
    """A MonitorRunner started from the project config, with the status API off and heartbeats mocked.

    Yields:
        The running MonitorRunner.
    """
    heartbeat = mocker.patch("sc_foundation.sc_logging.requests.get")
    heartbeat.return_value.status_code = 200
    (project / "config.yaml").write_text(CONFIG.format(api="False", script=""), encoding="utf-8")
    cmd_args = parse_command_line_args(["--homedir", str(project), "--config", "config.yaml"])
    config, logger = main.initialize_config_and_logging(cmd_args)
    monitor_runner = main.MonitorRunner(config, logger, threading.Event(), heartbeat_enabled=True)
    monitor_runner.start(main.build_monitor(config, logger, send_heartbeat=True), None)
    yield monitor_runner
    monitor_runner.stop()


def test_reload_does_nothing_when_config_unchanged(runner):
    monitor = runner.monitor

    assert runner.reload_if_changed() is False
    assert runner.monitor is monitor


def test_reload_applies_new_settings(project, runner):
    _write_config(project, replacements=[("CheckInterval: 30", "CheckInterval: 45"), ('"*.aussiebb.net"', '"*.example.net"')])
    old_monitor = runner.monitor

    assert runner.reload_if_changed() is True
    assert runner.monitor is not old_monitor
    assert runner.monitor.check_interval == 45
    assert runner.monitor.detector.primary_patterns == ["*.example.net"]


def test_reload_updates_logger_and_heartbeat_settings(project, runner):
    _write_config(project, replacements=[("ConsoleVerbosity: debug", "ConsoleVerbosity: error"), ("Enable: True\n  WebsiteURL", "Enable: False\n  WebsiteURL")])

    assert runner.reload_if_changed() is True
    assert runner.logger.console_verbosity == "error"
    assert runner.logger.heartbeat_config == {}
    assert runner.monitor.send_heartbeat is False


def test_reload_with_invalid_config_keeps_current_settings_and_logs_once(project, runner, mocker):
    log = mocker.spy(runner.logger, "log_message")
    monitor = runner.monitor
    _write_config(project, replacements=[('- "*.aussiebb.net"', ""), ("CheckInterval: 30", "CheckInterval: 45")])

    assert runner.reload_if_changed() is False
    assert runner.reload_if_changed() is False
    assert runner.monitor is monitor
    assert runner.monitor.check_interval == 30
    errors = [c for c in log.call_args_list if c.args[1:] == ("error",)]
    assert len(errors) == 1
    assert "keeping the current settings" in errors[0].args[0]

    # Fixing the file is picked up on the next check.
    _write_config(project, replacements=[("CheckInterval: 30", "CheckInterval: 45")])
    assert runner.reload_if_changed() is True
    assert runner.monitor.check_interval == 45


def test_reload_with_missing_state_change_script_keeps_current_monitor(project, runner, mocker):
    log = mocker.spy(runner.logger, "log_message")
    monitor = runner.monitor
    _write_config(project, script="scripts/missing.sh", replacements=[("CheckInterval: 30", "CheckInterval: 45")])

    assert runner.reload_if_changed() is False
    assert runner.monitor is monitor
    assert runner.monitor.check_interval == 30
    assert any("StateChangeScript" in c.args[0] for c in log.call_args_list if c.args[1:] == ("error",))


def _free_port():
    with socket.socket() as port_holder:
        port_holder.bind(("127.0.0.1", 0))
        return port_holder.getsockname()[1]


def _get_status(port, timeout=10):
    # urllib, not requests: requests.get is mocked for the heartbeat.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1) as response:
                return json.loads(response.read())
        except OSError:
            time.sleep(0.05)
    return None


@pytest.mark.usefixtures("lookups")
def test_monitor_mode_moves_status_api_when_config_changes(project, mocker, monkeypatch, restore_signal_handlers):  # noqa: ARG001
    """Editing the config while monitor mode runs restarts the status API on the new port."""
    monkeypatch.setattr(main, "CONFIG_CHECK_INTERVAL", 0.05)
    first_port, second_port = _free_port(), _free_port()
    (project / "config.yaml").write_text(CONFIG.format(api="True", script="").replace("Port: 8099", f"Port: {first_port}"), encoding="utf-8")
    heartbeat = mocker.patch("sc_foundation.sc_logging.requests.get")
    heartbeat.return_value.status_code = 200
    seen = {}

    def change_port_then_stop():
        seen["first"] = _get_status(first_port)
        _write_config(project, api="True", replacements=[("Port: 8099", f"Port: {second_port}")])
        seen["second"] = _get_status(second_port)
        seen["first_after_reload"] = _get_status(first_port, timeout=0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=change_port_then_stop, daemon=True).start()

    exit_code = main.main(["--homedir", str(project), "--config", "config.yaml"])

    assert exit_code == 0
    assert seen["first"]["status"] == "NBN (primary)"
    assert seen["second"]["status"] == "NBN (primary)"
    assert seen["first_after_reload"] is None
