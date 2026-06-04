# AMP / JISILU Operations Runbook

Last updated: 2026-06-03

This runbook captures the current Aurora multi-zone deployment, AMP backend,
JISILU task chain, notification behavior, and the safe maintenance steps needed
by future ops agents.

Do not write Telegram tokens, JISILU cookies, GitHub tokens, database URLs, or
AMP master keys into this document.

## Hosts And Entry Points

- Host: `racknerd-us`
- Public IP: `104.168.30.204`
- Aurora main: `https://430123.xyz/`
- Aurora status zone: `https://status.430123.xyz/`
- Aurora AMP zone: `https://amp.430123.xyz/`
- Public AMP summary: `https://amp.430123.xyz/api/summary`
- Public status summary: `https://status.430123.xyz/api/summary`

Raw AMP API is not public. Port `8000` listens on the host but UFW only allows
Docker bridge access:

```text
8000/tcp ALLOW 172.17.0.0/16
```

Task containers should use:

```text
AMP_API_URL=http://172.17.0.1:8000
```

Do not expose AMP task config, run output, logs, registry scan, or mutation
endpoints publicly without adding a separate auth layer.

## Current Services

Systemd services on `racknerd-us`:

```text
aurora-main.service    127.0.0.1:3001  https://430123.xyz/
aurora-status.service  127.0.0.1:3002  https://status.430123.xyz/
aurora-amp.service     127.0.0.1:3003  https://amp.430123.xyz/
amp-api.service        0.0.0.0:8000    internal AMP backend
```

Nginx routes:

```text
430123.xyz        -> 127.0.0.1:3001
status.430123.xyz -> 127.0.0.1:3002
amp.430123.xyz    -> 127.0.0.1:3003
```

AMP source and runtime paths:

```text
/opt/amp/current                current AMP release symlink
/opt/amp/current/.env           runtime env, mode should stay restricted
/var/lib/amp                    AMP workdir
/var/lib/amp/tasks              registry scan root
/var/lib/amp/task-backups       task source backups; keep outside scan root
```

Never put backup directories under `/var/lib/amp/tasks`. Registry scan is
recursive and will scan old `automation.yaml` files again.

## JISILU Task Chain

Cross-system compatibility notes for AMP outputs and future independent product
backends live in the AMP source repo:

```text
D:\CODE\AMP\docs\system-integration-protocol.md
```

Enabled AMP tasks:

```text
jisilu_lof_scraper              09:30 Asia/Shanghai
  -> jisilu_lof_analyzer        09:40 Asia/Shanghai
    -> jisilu_lof_telegram_notifier 09:45 Asia/Shanghai

amp_health_reporter             00:00 and 12:00 Asia/Shanghai
```

Task meanings:

- `jisilu_lof_scraper`: fetches LOF records from JISILU and outputs
  `records_processed` plus reusable `records`.
- `jisilu_lof_analyzer`: reads latest scraper success through AMP task-chain API
  and emits compact `candidates` plus `summary_text`.
- `jisilu_lof_telegram_notifier`: reads analyzer output and sends Telegram only
  when `nav_discount_rt > 10`.
- `amp_health_reporter`: computes expected runs from enabled cron tasks over the
  lookback window and sends a Telegram health report.

Current expected public summary:

```text
https://amp.430123.xyz/api/summary
tasks_total=4
tasks_failed=0
placeholder=false
```

`failed_runs` may include historical deployment-test failures. Use current task
status and latest run status to judge health.

## Telegram Notification Rules

Telegram bot token and chat id are stored through AMP secret config. They must
not appear in source, docs, shell output, Docker build args, or git history.

Notifier behavior:

- Threshold is strict: `nav_discount_rt > 10`, not `>= 10`.
- If no candidate matches, the task succeeds with `notified=false`.
- If candidates match, the task sends one compact Telegram message.
- `dry_run=true` validates upstream reads and message construction without
  requiring Telegram config or sending messages.

Health reporter behavior:

- Computes expected run counts from cron schedules.
- Counts successes, failures, skipped runs, and missing runs.
- If successful runs cover expected runs, older extra failures in the same window
  are counted but do not warn.
- Warns on missing expected runs or uncovered failures.
- Supports explicit `ignored_task_ids` and `ignored_error_types`.
- 2026-06-04 fix: scheduled health checks monitor `trigger_type=schedule` by
  default, so deployment `/test` runs do not produce production warnings.
  `PENDING`/`RUNNING` scheduled runs count toward expected coverage, so
  `amp_health_reporter` does not mark its own current run missing while it is
  still executing.

Do not add broad ignores for deployment noise. Prefer fixing the task or relying
on the success coverage rule.

Health reporter remote patch point:

```text
/var/lib/amp/tasks/amp-health-reporter/app/main.py
/var/lib/amp/tasks/amp-health-reporter/tests/test_reporter.py
image: amp-health-reporter:1.0.0
rollback backups:
  app/main.py.bak-healthfix-20260604-original
  tests/test_reporter.py.bak-healthfix-20260604-original
```

## JISILU Credential Rotation

Local source path:

```text
D:\CODE\AMP-DEV\JISILU-STOCK
```

When JISILU credentials expire:

```powershell
cd D:\CODE\AMP-DEV\JISILU-STOCK
python capture_auth.py
```

Manual login refreshes:

```text
D:\CODE\AMP-DEV\JISILU-STOCK\auth.json
```

Convert `auth.json` into AMP scraper config with these fields:

```text
jisilu_cookies
jisilu_post_hash
jisilu_user_id
target_page
sort_field
sort_order
top_n
rp
timeout
```

Patch the config through AMP API so `jisilu_cookies` is encrypted by AMP. Do not
print cookie JSON in terminal output.

After updating credentials, trigger and dispatch one scraper run:

```bash
curl -fsS -X POST http://127.0.0.1:8000/tasks/jisilu_lof_scraper/test
curl -fsS -X POST http://127.0.0.1:8000/dispatcher/dispatch-one
```

Verify the latest successful scraper output contains both:

```text
records_processed
records
```

## Standard Checks

From `D:\DXH\OPSTOOL`:

```powershell
$env:OPS_MASTER_KEY='<vault master key>'
python -m ops_toolkit exec --timeout 120 racknerd-us "curl -fsS http://127.0.0.1:8000/healthz"
python -m ops_toolkit exec --timeout 120 racknerd-us "systemctl is-active aurora-main.service aurora-status.service aurora-amp.service amp-api.service"
```

Task status query:

```bash
cd /opt/amp/current
uv run python - <<'PY'
import asyncio, json
from amp.db.session import create_engine
from amp.db.repository import get_task, list_runs_for_task

async def main():
    engine = create_engine()
    async with engine.begin() as conn:
        for tid in [
            "jisilu_lof_scraper",
            "jisilu_lof_analyzer",
            "jisilu_lof_telegram_notifier",
            "amp_health_reporter",
        ]:
            task = await get_task(conn, tid)
            runs = await list_runs_for_task(conn, tid, 2)
            print(json.dumps({
                "id": tid,
                "status": task.get("status"),
                "schedule": task.get("schedule_value"),
                "runs": [(r.get("id"), r.get("status")) for r in runs],
            }, ensure_ascii=False))
    await engine.dispose()

asyncio.run(main())
PY
```

External quick checks:

```powershell
Invoke-WebRequest -UseBasicParsing https://430123.xyz/ -TimeoutSec 20
Invoke-WebRequest -UseBasicParsing https://amp.430123.xyz/api/summary -TimeoutSec 20
Invoke-WebRequest -UseBasicParsing https://status.430123.xyz/api/summary -TimeoutSec 20
```

Public raw AMP API should not be reachable:

```powershell
Invoke-WebRequest -UseBasicParsing http://104.168.30.204:8000/healthz -TimeoutSec 5
```

Expected behavior: timeout or connection failure from public network.

## Deployment Notes And Pitfalls

- Prefer `ops_toolkit` over raw SSH.
- Use `uv run python` under `/opt/amp/current`; remote shell may not have plain
  `python`.
- Registry scan preserves task runtime state only after AMP commit
  `0625c07 fix: preserve task runtime state during registry scan`.
- Invalid config validation is redacted after AMP commit
  `c80fa29 fix: redact invalid config validation errors`.
- The AMP source guide was updated in commit
  `0a5e57b docs: update automation module agent guide`.
- Downstream task-chain tokens are generated when the downstream task is enabled.
  A `READY` downstream can scan but will not receive `AMP_API_TOKEN`.
- On Linux, `host.docker.internal` may not resolve. Current deployment uses
  `AMP_API_URL=http://172.17.0.1:8000`.
- AMP API service listens on `0.0.0.0:8000` only because UFW restricts access to
  Docker bridge. Keep that firewall rule in place.

## Relevant GitHub Commits

AMP platform:

```text
43acbf8 feat: add task chain API and token injection
0625c07 fix: preserve task runtime state during registry scan
c80fa29 fix: redact invalid config validation errors
0a5e57b docs: update automation module agent guide
```

Aurora:

```text
b4d79e8 fix: repair aurora zone auth nav and amp overview
```

## Local Source Notes

JISILU source directory is not a git repo:

```text
D:\CODE\AMP-DEV\JISILU-STOCK
```

It contains sensitive local files such as `auth.json` and HAR captures. Back up
locally if needed, but do not publish those files.
