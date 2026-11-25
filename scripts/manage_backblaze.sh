#!/usr/bin/env bash
: '=======================================================
Backblaze mode switcher

Runs a one-off WAN check and switches the external Backblaze helper script to
"continuous" mode on the primary WAN, or "manual" mode on the backup WAN.

Usage: manage_backblaze.sh [--config <path>]

The config file defaults to $APP_CONFIG, then configs/production.yaml.
=========================================================='

set -euo pipefail

TOGGLE_BACKBLAZE_SCRIPT="$HOME/scripts/toggle_backblaze_settings/toggle_backblaze_settings.sh"

ScriptDir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HomeDir="$(cd "$ScriptDir/.." && pwd)"
ConfigFile="${APP_CONFIG:-configs/production.yaml}"

while [ $# -gt 0 ]; do
	case "$1" in
		--config)
			ConfigFile="$2"
			shift 2
			;;
		*)
			echo "[manage_backblaze] Error: unknown argument: $1" >&2
			exit 1
			;;
	esac
done

if [ ! -x "$TOGGLE_BACKBLAZE_SCRIPT" ]; then
	echo "[manage_backblaze] Error: toggle_backblaze_settings.sh is not executable at $TOGGLE_BACKBLAZE_SCRIPT" >&2
	exit 1
fi

# Find uv reliably (cron/launchd often have a minimal PATH)
if command -v uv >/dev/null 2>&1; then
	UVCmd="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
	UVCmd="$HOME/.local/bin/uv"
else
	echo "[manage_backblaze] Error: 'uv' not found in PATH or at \$HOME/.local/bin/uv" >&2
	exit 1
fi

cd "$HomeDir"

# --onprimary prints exactly "Yes" or "No" on stdout; logging goes to stderr / the log file.
# uv's own progress output also goes to stderr, so it does not affect the captured result.
check_output="$("$UVCmd" run --quiet src/main.py --config "$ConfigFile" --onprimary)"

if [ "$check_output" = "Yes" ]; then
	exec "$TOGGLE_BACKBLAZE_SCRIPT" continuous
fi

if [ "$check_output" = "No" ]; then
	exec "$TOGGLE_BACKBLAZE_SCRIPT" manual
fi

echo "[manage_backblaze] Error: unexpected wan_check output: $check_output" >&2
exit 1
