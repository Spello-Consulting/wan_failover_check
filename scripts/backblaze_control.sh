#!/usr/bin/env bash
: '=======================================================
Backblaze control

Stops Backblaze while the WAN is on the 5G backup connection and starts it again when the
primary NBN connection is back. Written for General.StateChangeScript, which calls it as:

    backblaze_control.sh --on_primary true     # back on NBN: start Backblaze
    backblaze_control.sh --on_primary false    # on 5G backup: stop Backblaze

It can also be run by hand:

    backblaze_control.sh stop | start | status

Backblaze on macOS has two launchd jobs:

  com.backblaze.bzserv   system LaunchDaemon (root) that runs the backups. It has KeepAlive,
                         so it must be booted out of launchd; killing it just restarts it.
  com.backblaze.bzbmenu  per-user LaunchAgent for the menu bar icon.

Stopping boots both jobs out of launchd and ends any bztransmit / bzfilelist helpers still
running. Starting bootstraps them again. Neither change persists across a reboot: the plists
stay in place, so Backblaze always comes back after a restart.

The bzserv and helper commands need root, so they are run with "sudo -n". Add a sudoers rule
for exactly these commands (see the README, "Backblaze control script"), or the script fails
with an error rather than prompting for a password.

Exit codes: 0 success, 1 failure, 2 bad arguments.
=========================================================='

set -euo pipefail

# Paths can be overridden for testing. The sudoers rule names the default paths, so leave
# them alone in real use.
LAUNCHCTL="${BZ_LAUNCHCTL:-/bin/launchctl}"
SUDO="${BZ_SUDO:-/usr/bin/sudo}"
PKILL="${BZ_PKILL:-/usr/bin/pkill}"
USER_ID="${BZ_UID:-$(id -u)}"

DAEMON_LABEL="com.backblaze.bzserv"
DAEMON_PLIST="${BZ_DAEMON_PLIST:-/Library/LaunchDaemons/com.backblaze.bzserv.plist}"
AGENT_LABEL="com.backblaze.bzbmenu"
AGENT_PLIST="${BZ_AGENT_PLIST:-$HOME/Library/LaunchAgents/com.backblaze.bzbmenu.plist}"
HELPERS=(bztransmit bzfilelist)

log() {
	echo "[backblaze_control] $*"
}

err() {
	echo "[backblaze_control] Error: $*" >&2
}

usage() {
	cat <<EOF
Usage: $(basename "$0") --on_primary true|false
       $(basename "$0") stop | start | status
EOF
}

daemon_loaded() {
	"$LAUNCHCTL" print "system/$DAEMON_LABEL" >/dev/null 2>&1
}

agent_loaded() {
	"$LAUNCHCTL" print "gui/$USER_ID/$AGENT_LABEL" >/dev/null 2>&1
}

# Run a root command without prompting. Fails with a pointer to the sudoers rule.
as_root() {
	if ! "$SUDO" -n "$@"; then
		err "'sudo -n $*' failed. Check the sudoers rule described in the README."
		return 1
	fi
}

stop_backblaze() {
	local failed=0

	if daemon_loaded; then
		if as_root "$LAUNCHCTL" bootout "system/$DAEMON_LABEL"; then
			log "Stopped $DAEMON_LABEL"
		else
			failed=1
		fi
	else
		log "$DAEMON_LABEL is already stopped"
	fi

	# Upload / scan helpers can outlive bzserv. pkill exits 1 when nothing matched.
	local helper rc
	for helper in "${HELPERS[@]}"; do
		rc=0
		"$SUDO" -n "$PKILL" -x "$helper" || rc=$?
		case "$rc" in
			0) log "Ended $helper" ;;
			1) ;;
			*)
				err "'sudo -n $PKILL -x $helper' failed (exit $rc). Check the sudoers rule described in the README."
				failed=1
				;;
		esac
	done

	# The menu bar app is cosmetic, so a failure here is only a warning.
	if agent_loaded; then
		if "$LAUNCHCTL" bootout "gui/$USER_ID/$AGENT_LABEL"; then
			log "Quit the Backblaze menu bar app"
		else
			log "Warning: could not quit the Backblaze menu bar app"
		fi
	fi

	return "$failed"
}

start_backblaze() {
	local failed=0

	if daemon_loaded; then
		log "$DAEMON_LABEL is already running"
	elif as_root "$LAUNCHCTL" bootstrap system "$DAEMON_PLIST"; then
		log "Started $DAEMON_LABEL"
	else
		failed=1
	fi

	if agent_loaded; then
		:
	elif [ ! -f "$AGENT_PLIST" ]; then
		log "Warning: $AGENT_PLIST not found, menu bar app not started"
	elif "$LAUNCHCTL" bootstrap "gui/$USER_ID" "$AGENT_PLIST"; then
		log "Started the Backblaze menu bar app"
	else
		log "Warning: could not start the Backblaze menu bar app"
	fi

	return "$failed"
}

show_status() {
	if daemon_loaded; then
		log "$DAEMON_LABEL: running"
	else
		log "$DAEMON_LABEL: stopped"
	fi
	if agent_loaded; then
		log "$AGENT_LABEL: running"
	else
		log "$AGENT_LABEL: stopped"
	fi
}

action=""
case "${1:-}" in
	--on_primary)
		case "${2:-}" in
			true) action=start ;;
			false) action=stop ;;
			*)
				err "--on_primary needs true or false, got '${2:-}'"
				usage >&2
				exit 2
				;;
		esac
		[ $# -eq 2 ] || { err "unexpected arguments: ${*:3}"; usage >&2; exit 2; }
		;;
	stop | start | status)
		action="$1"
		[ $# -eq 1 ] || { err "unexpected arguments: ${*:2}"; usage >&2; exit 2; }
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		usage >&2
		exit 2
		;;
esac

if [ -n "${WAN_STATUS:-}" ]; then
	log "WAN is now: $WAN_STATUS"
fi

case "$action" in
	stop) stop_backblaze || exit 1 ;;
	start) start_backblaze || exit 1 ;;
	status) show_status ;;
esac
