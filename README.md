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
- drive helper automation such as Backblaze mode switching

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
  status_api.py         FastAPI app for GET /status
scripts/
  launch.sh             standard launcher (pyutil-managed; do not edit here)
  manage_backblaze.sh   switches Backblaze mode based on the WAN state
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

| Key             | Default              | Meaning                                       |
| --------------- | -------------------- | --------------------------------------------- |
| `AppName`       | —                    | Informational                                 |
| `CheckInterval` | `60`                 | Seconds between checks in monitor mode (5–3600) |

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

## Backblaze helper

`scripts/manage_backblaze.sh` runs `src/main.py --onprimary` and switches the external Backblaze
toggle script to `continuous` mode on the primary connection, or `manual` mode on the backup.

```bash
./scripts/manage_backblaze.sh                                  # uses $APP_CONFIG or configs/production.yaml
./scripts/manage_backblaze.sh --config configs/development.yaml
```

It expects the toggle script at
`$HOME/scripts/toggle_backblaze_settings/toggle_backblaze_settings.sh`.

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
| `manage_backblaze.sh` (repo root) | `scripts/manage_backblaze.sh`                    |

`status.json` keeps its 1.x fields and adds `reverse_dns`. Existing failover history files are
read and updated without changes.
