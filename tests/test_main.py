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
    (tmp_path / "config.yaml").write_text(CONFIG.format(api="True"), encoding="utf-8")
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
    (project / "config.yaml").write_text(CONFIG.format(api="True").replace("Port: 8099", f"Port: {port}"), encoding="utf-8")
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
    (project / "config.yaml").write_text(CONFIG.format(api="False"), encoding="utf-8")
    config, _ = main.initialize_config_and_logging(parse_command_line_args(["--homedir", str(project), "--config", "config.yaml"]))

    assert build_status_api(config, project / "status.json") is None


def test_invalid_config_exits(project):
    (project / "config.yaml").write_text(CONFIG.format(api="True").replace('- "*.aussiebb.net"', ""), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main.main(["--homedir", str(project), "--config", "config.yaml", "--onetime"])

    assert exc_info.value.code == 1
