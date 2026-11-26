"""Tests for scripts/backblaze_control.sh.

The script's launchctl, sudo and pkill commands are replaced with stub scripts that record
their calls and keep the "loaded" state of each launchd job as a file in a temp directory.
"""

import os
import subprocess  # noqa: S404 - runs the script under test
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backblaze_control.sh"
DAEMON = "com.backblaze.bzserv"
AGENT = "com.backblaze.bzbmenu"

FAKE_LAUNCHCTL = """\
    #!/bin/sh
    echo "launchctl $*" >> "$FAKE_DIR/calls.log"
    case "$1" in
        print) [ -f "$FAKE_DIR/loaded/$(basename "$2")" ] ;;
        bootout) rm "$FAKE_DIR/loaded/$(basename "$2")" ;;
        bootstrap)
            case "$2" in
                system) touch "$FAKE_DIR/loaded/com.backblaze.bzserv" ;;
                *) [ -z "$FAKE_AGENT_FAIL" ] && touch "$FAKE_DIR/loaded/com.backblaze.bzbmenu" ;;
            esac
            ;;
    esac
"""

FAKE_SUDO = """\
    #!/bin/sh
    echo "sudo $*" >> "$FAKE_DIR/calls.log"
    [ -n "$FAKE_SUDO_DENY" ] && exit 1
    [ "$1" = "-n" ] && shift
    exec "$@"
"""

FAKE_PKILL = """\
    #!/bin/sh
    echo "pkill $*" >> "$FAKE_DIR/calls.log"
    exit "${FAKE_PKILL_RC:-1}"
"""


@dataclass
class FakeSystem:
    """Stub commands and their shared state directory."""

    root: Path
    env: dict[str, str]

    def load(self, *labels: str) -> None:
        """Mark launchd jobs as loaded."""
        for label in labels:
            (self.root / "loaded" / label).touch()

    def loaded(self) -> set[str]:
        """Return the labels of the loaded launchd jobs."""
        return {p.name for p in (self.root / "loaded").iterdir()}

    def calls(self) -> list[str]:
        """Return the stub calls made, in order."""
        log = self.root / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        """Run the script with the stubs in place.

        Returns:
            The completed process, with output captured as text.
        """
        return subprocess.run(  # noqa: S603 - fixed script path
            [str(SCRIPT), *args],
            env=self.env | extra_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


def _stub(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake(tmp_path: Path) -> FakeSystem:
    """Set up stub launchctl, sudo and pkill commands.

    Returns:
        The fake system, with no launchd jobs loaded.
    """
    (tmp_path / "loaded").mkdir()
    agent_plist = tmp_path / f"{AGENT}.plist"
    agent_plist.touch()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "FAKE_DIR": str(tmp_path),
        "BZ_LAUNCHCTL": str(_stub(tmp_path / "launchctl", FAKE_LAUNCHCTL)),
        "BZ_SUDO": str(_stub(tmp_path / "sudo", FAKE_SUDO)),
        "BZ_PKILL": str(_stub(tmp_path / "pkill", FAKE_PKILL)),
        "BZ_UID": "501",
        "BZ_DAEMON_PLIST": f"/Library/LaunchDaemons/{DAEMON}.plist",
        "BZ_AGENT_PLIST": str(agent_plist),
    }
    return FakeSystem(tmp_path, env)


def test_backup_wan_stops_backblaze(fake: FakeSystem) -> None:
    fake.load(DAEMON, AGENT)

    result = fake.run("--on_primary", "false", WAN_STATUS="5G Backup")

    assert result.returncode == 0, result.stderr
    assert fake.loaded() == set()
    calls = fake.calls()
    assert f"sudo -n {fake.env['BZ_LAUNCHCTL']} bootout system/{DAEMON}" in calls
    assert f"sudo -n {fake.env['BZ_PKILL']} -x bztransmit" in calls
    assert f"sudo -n {fake.env['BZ_PKILL']} -x bzfilelist" in calls
    assert f"launchctl bootout gui/501/{AGENT}" in calls
    assert "WAN is now: 5G Backup" in result.stdout
    assert f"Stopped {DAEMON}" in result.stdout


def test_primary_wan_starts_backblaze(fake: FakeSystem) -> None:
    result = fake.run("--on_primary", "true")

    assert result.returncode == 0, result.stderr
    assert fake.loaded() == {DAEMON, AGENT}
    calls = fake.calls()
    assert (
        f"sudo -n {fake.env['BZ_LAUNCHCTL']} bootstrap system /Library/LaunchDaemons/{DAEMON}.plist"
        in calls
    )
    assert f"launchctl bootstrap gui/501 {fake.env['BZ_AGENT_PLIST']}" in calls


def test_stop_when_already_stopped_does_not_boot_out(fake: FakeSystem) -> None:
    result = fake.run("stop")

    assert result.returncode == 0, result.stderr
    assert "already stopped" in result.stdout
    assert not any("bootout" in call for call in fake.calls())


def test_start_when_already_running_does_not_bootstrap(fake: FakeSystem) -> None:
    fake.load(DAEMON, AGENT)

    result = fake.run("start")

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout
    assert not any("bootstrap" in call for call in fake.calls())


def test_stop_reports_ended_helpers(fake: FakeSystem) -> None:
    result = fake.run("stop", FAKE_PKILL_RC="0")

    assert result.returncode == 0, result.stderr
    assert "Ended bztransmit" in result.stdout


def test_pkill_error_fails(fake: FakeSystem) -> None:
    result = fake.run("stop", FAKE_PKILL_RC="3")

    assert result.returncode == 1
    assert "sudoers" in result.stderr


def test_sudo_denied_fails_with_sudoers_hint(fake: FakeSystem) -> None:
    fake.load(DAEMON)

    result = fake.run("--on_primary", "false", FAKE_SUDO_DENY="1")

    assert result.returncode == 1
    assert "sudoers" in result.stderr
    assert DAEMON in fake.loaded()


def test_menu_app_failure_is_only_a_warning(fake: FakeSystem) -> None:
    result = fake.run("start", FAKE_AGENT_FAIL="1")

    assert result.returncode == 0, result.stderr
    assert "Warning: could not start the Backblaze menu bar app" in result.stdout
    assert fake.loaded() == {DAEMON}


def test_status_does_not_change_anything(fake: FakeSystem) -> None:
    fake.load(DAEMON)

    result = fake.run("status")

    assert result.returncode == 0
    assert f"{DAEMON}: running" in result.stdout
    assert f"{AGENT}: stopped" in result.stdout
    assert all(call.startswith("launchctl print") for call in fake.calls())


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--on_primary"],
        ["--on_primary", "yes"],
        ["--on_primary", "true", "extra"],
        ["restart"],
        ["stop", "x"],
    ],
)
def test_bad_arguments(fake: FakeSystem, args: list[str]) -> None:
    result = fake.run(*args)

    assert result.returncode == 2
    assert fake.calls() == []
