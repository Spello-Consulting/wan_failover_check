# Issue #4 — Refactor app and use reverse DNS for WAN state detection

Branch: `issue-4-refactor-rdns` · Status: approved and implemented (v2.0.0)

## Goals

1. Rebuild the app on the `~/dev/py_templates/basic` construction pattern: `src/` layout, YAML
   config via `SCConfigManager`, logging via `SCLogger`, `.env.*.template` + `scripts/launch.sh`,
   pytest suite, strict typing, ruff-clean.
2. Replace static-IP detection with reverse-DNS matching: look up the PTR name of the current
   public IP and treat the connection as primary when it matches a configured pattern such as
   `*.aussiebb.net`.
3. Keep today's external behaviour (status file, failover CSV, heartbeats, `/status` API,
   `--onetime` / `--onprimary` / `--noheartbeat`) so `manage_backblaze.sh` and anything reading
   `/status` keep working.

## Target layout

```text
configs/
  development.yaml
  production.yaml
src/
  __init__.py
  main.py               CLI parsing, config + logger init, mode dispatch, thread start-up
  config_schemas.py     Cerberus schema for the app-specific config sections
  wan_detector.py       public IP -> reverse DNS -> pattern match -> WANStatus
  wan_monitor.py        one check + outputs + heartbeat, and the monitor loop with back-off
  status_store.py       write / read status.json (JSONEncoder)
  failover_history.py   open / close failover rows in the CSV (CSVReader)
  status_api.py         FastAPI app + uvicorn server thread with clean shutdown
tests/
  conftest.py           from template (op inject is skipped when no .env.test.template)
  test_main.py
  test_wan_detector.py
  test_status_store.py
  test_failover_history.py
  test_status_api.py
  test_wan_monitor.py
scripts/
  launch.sh             unchanged (pyutil-managed)
  manage_backblaze.sh   moved from repo root, updated to call src/main.py
deploy/sydneyapp/wan-failover-check.service   add EnvironmentFile line from template
.env.dev.template / .env.prod.template        APP_ENV + APP_CONFIG only
.pre-commit-config.yaml                       ggshield, from template
```

`wan_check.py` is deleted. `pyproject.toml`: `launch_path = "src/main.py"`, add
`sc-foundation-services` and `python-dotenv`, drop the unused `flask`, add `pytest-mock`,
`httpx` (for FastAPI's `TestClient`) and `mypy` to the `all` extra, add the template's
setuptools / pytest `pythonpath` sections, bump version to `2.0.0`.

## Configuration

Everything that is currently an environment variable moves into YAML. The `.env` templates keep
only `APP_ENV` and `APP_CONFIG` (there are no secrets in this app).

```yaml
General:
  AppName: wan-failover-check
  CheckInterval: 60            # seconds between checks (was MONITOR_INTERVAL)

WAN:
  PrimaryMatch:                # fnmatch globs tested against the PTR name, case-insensitive
    - "*.aussiebb.net"
  PrimaryLabel: NBN (primary)
  BackupLabel: 5G Backup
  StatusFile: status.json
  FailoverHistoryFile: failover_history.csv   # was FAILOVER_CSV_FILE
  DNSTimeout: 5                # seconds allowed for the reverse lookup

StatusAPI:
  Enable: true                 # was "API_IP is set"
  Host: 0.0.0.0                # was API_IP
  Port: 8087                   # was API_PORT

Files:          # standard SCLogger section (logs/logfile.log)
Email:          # standard section; EnableEmail false by default
HeartbeatMonitor:
  Enable: true
  WebsiteURL: https://uptime.betterstack.com/api/v1/heartbeat/<id>   # was HEARTBEAT_PRIMARY
  HeartbeatTimeout: 5
  Frequency: 30
```

`HEARTBEAT_LTE` goes away: it was always `HEARTBEAT_PRIMARY + "/fail"`, which is exactly what
`SCLogger.ping_heartbeat(is_fail=True)` sends. So: primary → `ping_heartbeat()`, backup →
`ping_heartbeat(is_fail=True)`, no internet → no ping (as today). `Frequency` must be ≤
`CheckInterval` or pings are throttled.

## WAN detection

```text
SCCommon.get_external_ip()          (same four echo services as today)
  └─ fails  -> state "No internet", on_primary false, external_ip null
socket.gethostbyaddr(ip)            (run with DNSTimeout)
  ├─ PTR found      -> strip trailing ".", lower-case, fnmatch against PrimaryMatch
  │                      match -> PrimaryLabel / on_primary true
  │                      none  -> BackupLabel  / on_primary false
  ├─ no PTR (herror) -> BackupLabel / on_primary false, reverse_dns null
  └─ resolver error / timeout -> state "DNS lookup failed", on_primary false
```

The resolver-error case is deliberately not reported as a failover: a flaky resolver must not
open a CSV row or send a fail heartbeat. It is treated like "No internet" — status file updated,
CSV and heartbeat skipped, counted as an error for back-off.

Verified today: `180.150.43.236` → `180-150-43-236.b4962b.syd.static.aussiebb.net.`, which
`*.aussiebb.net` matches.

## Outputs

`status.json` keeps its existing fields and gains `reverse_dns`:

```json
{
  "timestamp": "2026-11-26T01:00:00Z",
  "on_primary": true,
  "status": "NBN (primary)",
  "external_ip": "180.150.43.236",
  "reverse_dns": "180-150-43-236.b4962b.syd.static.aussiebb.net"
}
```

Written with `JSONEncoder.save_to_file` (atomic replace). Read back with the standard `json`
module, because `JSONEncoder.read_from_file` converts the ISO timestamp into a `datetime`. Failover CSV format is unchanged
(`Start Time`, `End Time`, `Duration Hours`, local `yyyy-mm-dd hh:mm:ss`), read/written through
`CSVReader` with a header config; existing history files remain compatible. Timestamps via
`DateHelper`.

## CLI and run modes

```text
--config FILE     config file (launch.sh adds it from APP_CONFIG)
--homedir PATH    project root override (template)
--onetime         one check, write outputs, print "[ok] <state> ext-ip=<ip> rdns=<name>", exit
--onprimary       one check, write outputs, print exactly "Yes" or "No" on stdout, exit
--noheartbeat     disable heartbeats for this run
```

`--nbn-ip` is removed (no static IP any more). In `--onprimary` mode the logger's console
verbosity is forced to `error` and the startup banner goes to stderr, so stdout carries only
`Yes`/`No` for `manage_backblaze.sh`. Exit codes stay as documented today.

Monitor mode follows the eufy-siren pattern: SIGINT/SIGTERM set a `stop_event`; a
`ThreadManager` runs the monitor loop and (when enabled) the API server thread; uvicorn is
stopped via `server.should_exit` on shutdown, so `systemctl stop` exits 0 cleanly. The
existing ×1/×2/×3 back-off on consecutive errors is kept. Fatal errors use
`logger.log_fatal_error` (fatal-error file + recovery email, as in the template).

## Docs

README rewritten around the new layout: setup (`ln -s .env.dev.template .env.target`),
config reference for every YAML key, detection logic, run modes, outputs, API, Backblaze helper,
systemd/deploy, development commands, and a short "upgrading from 1.x" section mapping each old
environment variable to its YAML key.

## Testing

pytest with `socket.gethostbyaddr`, `SCCommon.get_external_ip`, and heartbeats mocked:
pattern matching (case, trailing dot, multiple patterns, no match), each detection branch,
status file contents per state, CSV open/no-duplicate/close/duration, `/status` 200 and 503,
CLI parsing, `--onprimary` stdout is exactly `Yes`/`No`. Then `ruff check`, `mypy --strict src`,
and one real `--onetime` run against the live connection.

## Open questions

1. **`.env.target` is tracked in git** (a symlink to `.env.dev.template`), while the template
   gitignores it. Untracking it means the next deploy's `git pull` deletes the symlink on
   sydneyapp, and you would need to recreate it there (`ln -s .env.prod.template .env.target`).
   Leaving it tracked means production keeps resolving to the dev template, which after this
   change selects `configs/development.yaml`. **Proposed:** untrack and gitignore it, and you
   recreate the prod symlink on sydneyapp after merge.
   
   > Agree
2. **No PTR record → backup.** Proposed above. The alternative is to treat a missing PTR as
   "unknown" like a resolver error. Backup seems right for a 5G link, but you know that
   provider's behaviour.
   
   > Agree
3. **Heartbeat URL in YAML.** It's currently in the env templates in plain text, so moving it to
   the committed YAML changes nothing security-wise. If you'd rather it come from 1Password, I'd
   read it from an `op://` reference in the env template instead — tell me the vault/item/field.
   
   > OK with keeping in yaml config, same as other projects
