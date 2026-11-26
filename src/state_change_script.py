"""Run the optional user script when the WAN moves between the primary and backup connections."""

import os
import subprocess  # noqa: S404 - runs the script named in the app's own config
from pathlib import Path

from wan_detector import WANStatus

DEFAULT_SCRIPT_TIMEOUT = 60.0


def resolve_script_path(path_text: str, project_root: Path) -> Path:
    """Resolve and check the StateChangeScript path from the config.

    Args:
        path_text: The configured path, absolute or relative to the project root.
        project_root: The project root directory.

    Returns:
        The absolute script path.

    Raises:
        ValueError: If the script does not exist, is not a file, or is not executable.
    """
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = project_root / path
    path = path.resolve()
    if not path.exists():
        msg = f"StateChangeScript {path} does not exist."
        raise ValueError(msg)
    if not path.is_file():
        msg = f"StateChangeScript {path} is not a file."
        raise ValueError(msg)
    if not os.access(path, os.X_OK):
        msg = f"StateChangeScript {path} is not executable (try: chmod +x {path})."
        raise ValueError(msg)
    return path


class StateChangeScript:
    """Calls a script with ``--on_primary true|false`` when the WAN state changes."""

    def __init__(self, path: Path, working_dir: Path, timeout: float = DEFAULT_SCRIPT_TIMEOUT) -> None:
        """Create the script runner.

        Args:
            path: The absolute path to the executable script.
            working_dir: The directory the script runs in (the project root).
            timeout: Seconds to wait for the script before it is killed.
        """
        self.path = path
        self.working_dir = working_dir
        self.timeout = timeout

    def command(self, on_primary: bool) -> list[str]:
        """Build the command line for the script.

        Args:
            on_primary: True if the WAN is now on the primary connection.

        Returns:
            The command and its arguments.
        """
        return [str(self.path), "--on_primary", "true" if on_primary else "false"]

    def run(self, status: WANStatus) -> subprocess.CompletedProcess[str]:
        """Run the script for the new WAN status.

        As well as the ``--on_primary`` argument, the script gets the WAN_STATUS, WAN_EXTERNAL_IP
        and WAN_REVERSE_DNS environment variables.

        Args:
            status: The new WAN status.

        Returns:
            The completed process, with stdout and stderr captured as text.

        Raises:
            RuntimeError: If the script could not be started or did not finish within the timeout.
        """
        env = os.environ | {
            "WAN_STATUS": status.label,
            "WAN_EXTERNAL_IP": status.external_ip or "",
            "WAN_REVERSE_DNS": status.reverse_dns or "",
        }
        try:
            return subprocess.run(  # noqa: S603 - the command is the configured script, no shell
                self.command(status.on_primary),
                cwd=self.working_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            msg = f"State change script {self.path} did not finish within {self.timeout:g}s and was stopped."
            raise RuntimeError(msg) from e
        except OSError as e:
            msg = f"Failed to run state change script {self.path}: {e}"
            raise RuntimeError(msg) from e
