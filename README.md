# WAN Failover Check

WAN Failover Check monitors whether the network is on the primary NBN connection or on a backup
WAN connection such as 5G failover.

It finds the current public IP address, looks up that address's reverse DNS (PTR) name, and
compares the name with patterns you configure. With Aussie Broadband, for example, the primary
connection's PTR name looks like `180-150-43-236.b4962b.syd.aussiebb.net`, so the pattern
`*.aussiebb.net` identifies it. Any other name, or no PTR record at all, means the backup
connection is active. This works whether the primary connection has a static or a dynamic IP.

From each check the app can:

- report the current WAN state on the console and in a log file
- write the latest state to `status.json`
- keep a CSV history of failover periods
- send Better Stack heartbeats (a "fail" heartbeat while on the backup connection)
- serve the latest state over HTTP at `GET /status`
- run a script of your own when the WAN moves between primary and backup

## Prerequisites

- Python 3.13+ — `brew install python@3.13`
- [uv](https://docs.astral.sh/uv/) — `brew install uv`
- Internet access to at least one of the public IP echo services listed under
  [How WAN detection works](#how-wan-detection-works)

`scripts/launch.sh` runs `uv sync` to install dependencies into the virtual environment
automatically.

> **macOS note:** to let the app reach the local network, allow your terminal/IDE under
> *System Settings > Privacy and Security > Local Network*.

## Project layout

```text
configs/
  development.yaml      config for the dev environment
  production.yaml       config for production (sydneyapp)
src/
  main.py               entry point: CLI, config and logging set-up, run modes
  config_schemas.py     validation schema for the app's config sections
  wan_detector.py       public IP -> reverse DNS -> pattern match
  wan_monitor.py        runs checks, writes outputs, sends heartbeats, monitor loop
  status_store.py       reads and writes status.json
  failover_history.py   maintains the failover history CSV
  state_change_script.py  runs the optional script on primary/backup changes
  status_api.py         FastAPI app for GET /status
scripts/
  launch.sh             standard launcher (pyutil-managed; do not edit here)
  backblaze_control.sh  state change script: stops Backblaze on the backup WAN
deploy/sydneyapp/       systemd service file for the production host
tests/                  pytest suite
```

The app is built on [sc-foundation-services](https://github.com/Spello-Consulting/sc-foundation):
`SCConfigManager` for the YAML config, `SCLogger` for logging, email and heartbeats,
`ThreadManager` for the monitor and API threads, `CSVReader` for the failover history,
`JSONEncoder` for atomic status file writes, `DateHelper` for timestamps, and
`SCCommon.get_external_ip()` for the public IP lookup.

## Setup

```bash
uv sync --extra all
```

Then select the environment file for this checkout:

```bash
ln -s .env.dev.template .env.target
```

Use `.env.prod.template` on the production host instead. `.env.target` is gitignored, so each
checkout keeps its own choice.

## Environment files

The app has no secrets, so the environment templates only select the environment and config file:

| Variable     | Purpose                                                              |
| ------------ | -------------------------------------------------------------------- |
| `APP_ENV`    | `development` or `production`; `launch.sh` refuses to run without it |
| `APP_CONFIG` | config file passed to the app as `--config`                          |

`launch.sh` resolves `.env.target` through `op run`, so 1Password `op://` references can be
added later if the app ever needs secrets.

## Configuration

All settings live in the YAML config file. The file is validated at start-up, and the app exits
with an error that names the bad setting if validation fails.

### `General`

| Key                 | Default | Meaning                                                                       |
| ------------------- | ------- | ----------------------------------------------------------------------------- |
| `AppName`           | —       | Informational                                                                 |
| `CheckInterval`     | `60`    | Seconds between checks in monitor mode (5–3600)                               |
| `StateChangeScript` | none    | Executable to run when the WAN moves between primary and backup. See [State change script](#state-change-script). |

### `WAN`

| Key                   | Default                | Meaning                                                                                    |
| --------------------- | ---------------------- | ------------------------------------------------------------------------------------------ |
| `PrimaryMatch`        | required               | List of glob patterns matched against the PTR name, e.g. `"*.aussiebb.net"`. Case-insensitive; a trailing dot on the PTR name is ignored. A match on any pattern means primary. |
| `PrimaryLabel`        | `NBN (primary)`        | State label when on the primary connection                                                 |
| `BackupLabel`         | `5G Backup`            | State label when on the backup connection                                                  |
| `StatusFile`          | `status.json`          | Where to write the latest state                                                            |
| `FailoverHistoryFile` | `failover_history.csv` | Where to record failover periods                                                           |
| `DNSTimeout`          | `5`                    | Seconds allowed for the reverse DNS lookup (1–60)                                          |

A bare file name such as `status.json` goes in the project root. A relative path such as
`data/status.json` is resolved from the working directory (`launch.sh` changes to the project
root first). Absolute paths are used as given.

Patterns use shell-style globbing: `*` matches any run of characters (including dots), `?`
matches one character. `*.aussiebb.net` matches `x.syd.aussiebb.net` but not `aussiebb.net` itself.

### `StatusAPI`

| Key      | Default   | Meaning                         |
| -------- | --------- | ------------------------------- |
| `Enable` | `False`   | Serve `GET /status`             |
| `Host`   | `0.0.0.0` | Bind address                    |
| `Port`   | `8080`    | TCP port (the configs use 8087) |

### `HeartbeatMonitor`

Standard sc-foundation heartbeat settings.

| Key                | Meaning                                                                       |
| ------------------ | ----------------------------------------------------------------------------- |
| `Enable`           | Send heartbeats in monitor mode                                               |
| `WebsiteURL`       | Better Stack heartbeat URL                                                    |
| `HeartbeatTimeout` | Request timeout in seconds                                                    |
| `Frequency`        | Minimum seconds between pings. Keep it at or below `CheckInterval`, or some checks won't ping. |

After each successful check the app pings `WebsiteURL` on the primary connection and
`WebsiteURL/fail` on the backup connection. It sends no heartbeat when the state can't be
determined. Heartbeats are only sent in monitor mode.

### `Files` and `Email`

Standard sc-foundation logging and email sections. Logs go to `logs/logfile.log`. Email is
disabled in both configs. If you enable it, the SMTP settings come from the `SMTP_*` environment
variables. A fatal error is recorded in `<project>_fatal_error.txt` and, when email is enabled,
reported by email, with a recovery email on the next successful start.

## How WAN detection works

1. Get the public IP from the first of these services that answers: `https://api.ipify.org`,
   `https://icanhazip.com`, `https://ifconfig.me/ip`, `https://checkip.amazonaws.com`.
2. Look up the PTR name for that IP, waiting at most `DNSTimeout` seconds.
3. Classify the result:

| Outcome                                     | State label         | `on_primary` | Failover CSV | Heartbeat     |
| ------------------------------------------- | ------------------- | ------------ | ------------ | ------------- |
| PTR name matches a `PrimaryMatch` pattern   | `PrimaryLabel`      | `true`       | closes an open row | primary |
| PTR name doesn't match, or no PTR record    | `BackupLabel`       | `false`      | opens a row  | fail          |
| Every IP echo service failed                | `No internet`       | `false`      | unchanged    | none          |
| Resolver error or timeout on the PTR lookup | `DNS lookup failed` | `false`      | unchanged    | none          |

The last two states always update `status.json`, so the API never serves stale data. They don't
touch the failover history, so a flaky resolver can't record a false failover.

## Running the app

### Monitor mode (default)

```bash
./scripts/launch.sh
```

`launch.sh` loads `.env.target`, adds `--config $APP_CONFIG`, runs `uv sync`, and starts
`src/main.py`. To run the app directly instead:

```bash
uv run src/main.py --config configs/development.yaml
```

In monitor mode the app:

- starts the status API if `StatusAPI.Enable` is set
- checks the WAN every `CheckInterval` seconds, writing `status.json` and the failover CSV
- sends heartbeats unless disabled
- logs a summary line whenever the state changes, and a detailed line on every check
- runs the `StateChangeScript`, if set, when the WAN moves between primary and backup
- backs off to 2× and then 3× the interval after consecutive failed checks or heartbeats
- shuts down cleanly on Ctrl-C or SIGTERM (`systemctl stop`)

### Command-line options

| Option            | Meaning                                                                    |
| ----------------- | -------------------------------------------------------------------------- |
| `--config FILE`   | Config file, relative to the project root or absolute                      |
| `--homedir PATH`  | Override the project root                                                  |
| `--onetime`       | Run one check, write the outputs, print the result, and exit               |
| `--onprimary`     | Run one check, write the outputs, print exactly `Yes` or `No`, and exit    |
| `--noheartbeat`   | Don't send heartbeats in this run                                          |

`--onetime` and `--onprimary` can't be combined, and neither sends heartbeats.

```bash
uv run src/main.py --config configs/development.yaml --onetime
```

```text
[ok] NBN (primary)  ext-ip=180.150.43.236  rdns=180-150-43-236.b4962b.syd.static.aussiebb.net
```

In `--onprimary` mode, stdout carries only `Yes` or `No`. Log output is limited to errors, which
go to stderr, so scripts can capture the result directly.

### Exit codes

| Code | Meaning                                                                      |
| ---- | ---------------------------------------------------------------------------- |
| `0`  | Successful one-time check, or clean shutdown of monitor mode                 |
| `1`  | Configuration error, start-up error, or a one-time check that couldn't determine the state |
| `1`  | A managed thread crashed fatally in monitor mode, so systemd restarts the service |

## Outputs

### `status.json`

```json
{
    "timestamp": "2026-11-26T01:00:00Z",
    "on_primary": true,
    "status": "NBN (primary)",
    "external_ip": "180.150.43.236",
    "reverse_dns": "180-150-43-236.b4962b.syd.static.aussiebb.net"
}
```

| Field         | Meaning                                                        |
| ------------- | -------------------------------------------------------------- |
| `timestamp`   | UTC time of the check                                          |
| `on_primary`  | `true` only when the primary connection was detected           |
| `status`      | State label (see the table under How WAN detection works)      |
| `external_ip` | Public IP, or `null` when no echo service answered             |
| `reverse_dns` | PTR name (lower case, no trailing dot), or `null` if none      |

The file is replaced atomically on every check.

### Failover history CSV

| Column           | Meaning                                                  |
| ---------------- | -------------------------------------------------------- |
| `Start Time`     | When the backup connection was first seen (local time)   |
| `End Time`       | When the primary connection returned; empty while open   |
| `Duration Hours` | Length of the failover period, to two decimal places     |

Times are written as `yyyy-mm-dd hh:mm:ss`. The file is created on the first failover. Only one
row is open at a time, and durations that span a daylight-saving change are calculated correctly.

```csv
Start Time,End Time,Duration Hours
2026-04-09 10:00:00,2026-04-09 12:30:00,2.50
```

## HTTP status API

When `StatusAPI.Enable` is set, the app serves the contents of `status.json`:

```bash
curl http://127.0.0.1:8087/status
```

The endpoint returns HTTP `503` with an `error` field if the status file doesn't exist yet or
can't be read.

## State change script

Set `General.StateChangeScript` to have the app run a script whenever the WAN moves from primary
to backup or back again:

```yaml
General:
  StateChangeScript: scripts/wan_state_change.sh
```

The script is called with one argument pair giving the new state:

```bash
scripts/wan_state_change.sh --on_primary true    # now on the primary connection
scripts/wan_state_change.sh --on_primary false   # now on the backup connection
```

It also gets these environment variables:

| Variable          | Value                                              |
| ----------------- | -------------------------------------------------- |
| `WAN_STATUS`      | The state label, e.g. `5G Backup`                  |
| `WAN_EXTERNAL_IP` | The public IP                                      |
| `WAN_REVERSE_DNS` | The PTR name, or empty if there is none            |

How it behaves:

- **When it runs.** The trigger is the failover history: the script runs when a failover row is
  opened (primary → backup) or closed (backup → primary). The history file holds the last known
  state, so each change runs the script once, even across restarts, and whether the change is
  first seen in monitor mode or by an `--onetime` / `--onprimary` run. Nothing runs on a check
  that finds the same state, or at start-up when the state hasn't changed.
- **What doesn't count as a change.** `No internet` and `DNS lookup failed` don't run the script.
  Going primary → no internet → primary runs nothing; primary → no internet → backup runs it once.
- **Where it runs.** The working directory is the project root, and a relative path is resolved
  from there. The script must exist and be executable (`chmod +x`), or the app exits at start-up
  with a configuration error.
- **Output and failures.** Stdout and stderr are captured and written to the log, so
  `--onprimary` output stays `Yes` / `No`. A non-zero exit code, a failure to start, or a run
  longer than 60 seconds (the script is then killed) is logged as an error; the app carries on.
  Checks wait while the script runs, so keep it short.
- **If the history file can't be read or written,** the change can't be detected and the script
  doesn't run. The error is logged.

A minimal script:

```bash
#!/usr/bin/env bash
set -euo pipefail
if [[ "${2:-}" == "true" ]]; then
    echo "Back on ${WAN_STATUS}"
else
    echo "Failed over to ${WAN_STATUS}"
fi
```

### Backblaze control script

`scripts/backblaze_control.sh` is a ready-made state change script for a Mac running Backblaze.
It stops Backblaze when the WAN fails over to the backup connection, so backups don't use 5G
data, and starts it again when the primary connection is back.

```yaml
General:
  StateChangeScript: scripts/backblaze_control.sh
```

It can also be run by hand:

```bash
scripts/backblaze_control.sh status
scripts/backblaze_control.sh stop
scripts/backblaze_control.sh start
```

Backblaze runs as two launchd jobs, and the script handles both:

| Job                     | What it is                                  | Stop / start                                      |
| ----------------------- | ------------------------------------------- | ------------------------------------------------- |
| `com.backblaze.bzserv`  | System LaunchDaemon (root) that runs backups | `launchctl bootout` / `bootstrap`, through `sudo -n` |
| `com.backblaze.bzbmenu` | Your LaunchAgent for the menu bar icon      | `launchctl bootout` / `bootstrap`, no root needed |

Both jobs have `KeepAlive`, so quitting or killing them only makes launchd restart them; they
have to be booted out. On stop the script also ends any `bztransmit` / `bzfilelist` helpers
that are still running. Stopping and starting are idempotent, and a problem with the menu bar
app is only a warning. Nothing persists across a reboot: Backblaze always starts after a
restart, even on the backup connection, until the next primary → backup change.

**It must run on the Mac that runs Backblaze**, not on sydneyapp. Run the app there as well,
for example from cron with `--onprimary` (a one-shot run still triggers the script on a change):

```text
*/5 * * * * cd /path/to/wan_failover_check && uv run --quiet src/main.py --config configs/<config>.yaml --onprimary >/dev/null 2>&1
```

**Sudoers rule.** The script runs its root commands with `sudo -n`, which fails instead of
asking for a password. Allow exactly those commands, without a password, for the user that runs
the app:

```bash
sudo visudo -f /etc/sudoers.d/backblaze-control
```

```text
<user> ALL=(root) NOPASSWD: /bin/launchctl bootout system/com.backblaze.bzserv, /bin/launchctl bootstrap system /Library/LaunchDaemons/com.backblaze.bzserv.plist, /usr/bin/pkill -x bztransmit, /usr/bin/pkill -x bzfilelist
```

Check it with `sudo -n -l`. Without the rule, `stop` and `start` exit 1 with an error that
points here, and the app logs it.

## Deployment

The `Deploy wan_failover_check` GitHub workflow and `deploy/auto-deploy.yaml` deploy to
sydneyapp, which runs the app as a systemd service using
`deploy/sydneyapp/wan-failover-check.service`.

To install the service manually:

```bash
sudo cp deploy/sydneyapp/wan-failover-check.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wan-failover-check
journalctl -u wan-failover-check -f
```

On the production host, `.env.target` must point at the production template:

```bash
ln -s .env.prod.template .env.target
```

## Development

```bash
uv sync --extra all          # install dev dependencies
uv run ruff check src tests  # lint
uv run mypy src              # type-check (strict)
uv run pytest                # run the test suite
```

The tests mock all network access (IP echo services, DNS and heartbeats), so they run offline.

## Upgrading from 1.x

Version 2.0 replaces the static-IP comparison with reverse DNS matching and moves all settings
from environment variables to YAML:

| 1.x environment variable / option | 2.0 setting                                      |
| --------------------------------- | ------------------------------------------------ |
| `NBN_STATIC_IP`, `--nbn-ip`       | `WAN.PrimaryMatch` (PTR name patterns)           |
| `HEARTBEAT_PRIMARY`               | `HeartbeatMonitor.WebsiteURL`                    |
| `HEARTBEAT_LTE`                   | not needed: `WebsiteURL/fail` is sent automatically |
| `MONITOR_INTERVAL`                | `General.CheckInterval`                          |
| `API_IP`                          | `StatusAPI.Enable` and `StatusAPI.Host`          |
| `API_PORT`                        | `StatusAPI.Port`                                 |
| `FAILOVER_CSV_FILE`               | `WAN.FailoverHistoryFile`                        |
| `wan_check.py`                    | `src/main.py`                                    |
| `manage_backblaze.sh`             | removed: use `General.StateChangeScript` with `scripts/backblaze_control.sh` |

`status.json` keeps its 1.x fields and adds `reverse_dns`. Existing failover history files are
read and updated without changes.
