"""Tests for src/state_change_script.py."""

import textwrap

import pytest

from state_change_script import StateChangeScript, resolve_script_path
from wan_detector import WANState, WANStatus

PRIMARY = WANStatus(WANState.PRIMARY, "NBN (primary)", "1.2.3.4", "host.aussiebb.net")
BACKUP = WANStatus(WANState.BACKUP, "5G Backup", "5.6.7.8", None)


def make_script(path, body):
    """Write an executable shell script.

    Returns:
        The script path.
    """
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolve_relative_path(tmp_path):
    (tmp_path / "scripts").mkdir()
    script = make_script(tmp_path / "scripts" / "hook.sh", "exit 0\n")

    assert resolve_script_path("scripts/hook.sh", tmp_path) == script.resolve()


def test_resolve_absolute_path(tmp_path):
    script = make_script(tmp_path / "hook.sh", "exit 0\n")

    assert resolve_script_path(str(script), tmp_path / "elsewhere") == script.resolve()


def test_resolve_missing_script(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        resolve_script_path("missing.sh", tmp_path)


def test_resolve_directory(tmp_path):
    (tmp_path / "scripts").mkdir()

    with pytest.raises(ValueError, match="is not a file"):
        resolve_script_path("scripts", tmp_path)


def test_resolve_not_executable(tmp_path):
    (tmp_path / "hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not executable"):
        resolve_script_path("hook.sh", tmp_path)


@pytest.mark.parametrize(("status", "expected"), [(PRIMARY, "true"), (BACKUP, "false")])
def test_command(tmp_path, status, expected):
    script = StateChangeScript(tmp_path / "hook.sh", working_dir=tmp_path)

    assert script.command(status.on_primary) == [str(tmp_path / "hook.sh"), "--on_primary", expected]


def test_run_passes_args_env_and_cwd(tmp_path):
    path = make_script(
        tmp_path / "hook.sh",
        """
        echo "args=$*"
        echo "status=$WAN_STATUS ip=$WAN_EXTERNAL_IP rdns=$WAN_REVERSE_DNS"
        echo "cwd=$(pwd -P)"
        """,
    )
    script = StateChangeScript(path, working_dir=tmp_path)

    result = script.run(BACKUP)

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "args=--on_primary false"
    assert lines[1] == "status=5G Backup ip=5.6.7.8 rdns="
    assert lines[2] == f"cwd={tmp_path.resolve()}"


def test_run_returns_failure_code(tmp_path):
    path = make_script(tmp_path / "hook.sh", "echo oops >&2\nexit 3\n")

    result = StateChangeScript(path, working_dir=tmp_path).run(PRIMARY)

    assert result.returncode == 3
    assert result.stderr.strip() == "oops"


def test_run_timeout(tmp_path):
    path = make_script(tmp_path / "hook.sh", "sleep 5\n")

    with pytest.raises(RuntimeError, match="did not finish"):
        StateChangeScript(path, working_dir=tmp_path, timeout=0.2).run(PRIMARY)


def test_run_start_failure(tmp_path):
    with pytest.raises(RuntimeError, match="Failed to run"):
        StateChangeScript(tmp_path / "gone.sh", working_dir=tmp_path).run(PRIMARY)
