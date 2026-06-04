# Monitoring Migration Handoff - 2026-05-31

Status: TICKET-2 and TICKET-3 completed. Awaiting operator browser/API validation.

Scope: `TICKET-2-rewrite-migrate-to-us.md` and pending `TICKET-3-rewrite-public-exposure-us.md`.

## Current State

- US `racknerd-us` is now the monitoring center.
- Dublin Prometheus container has been stopped and removed.
- Dublin Prometheus data/config remains at `/opt/prometheus` for rollback.
- US Prometheus runs as Docker container `prometheus`.
- US blackbox runs as Docker container `blackbox_exporter`.
- US Prometheus and blackbox are bound only to localhost on the host:
  - `127.0.0.1:9090`
  - `127.0.0.1:9115`
- `status.430123.xyz` is mounted through US Nginx to Aurora.
- Aurora/Next.js frontend is deployed on US and bound only to localhost.
- TICKET-3 public exposure is complete.
- 2026-06-04 update: Prometheus node monitoring is now configured for all 7
  OPSTOOL vault servers. Six are currently `up`; `Tencent-CN` is present in the
  status panel but remains `down` because `106.55.164.100` is unreachable from
  US on SSH, HTTP/HTTPS, ICMP, and `9100/tcp`.
- 2026-06-04 update: status monitor charts default to 12h instead of 1h. Range
  queries are downsampled to about 144 points per series, so the 12h view uses a
  300s step and range polling is 60s instead of 15s.

## Completed Stages

### TICKET-2 Stage 1

Read-only US inspection completed.

Important findings:

- US 80/443 are served by Nginx.
- `api2.430123.xyz` is an Nginx server block proxying to `https://api.430123.xyz`.
- US ports `9090`, `9115`, `3000` were initially free.
- US already had `node_exporter` running on `*:9100`, with UFW allowing only Dublin `192.227.147.101`.

### TICKET-2 Stage 2

US Prometheus deployed:

- Config: `/opt/prometheus/config/prometheus.yml`
- Data: `/opt/prometheus/data`
- Container: `prometheus`
- Image: `prom/prometheus:latest`
- Host binding: `127.0.0.1:9090`
- Docker port: `9090/tcp -> 127.0.0.1:9090`
- Retention: `15d`

Config note:

- Dublin target was changed from old-center `127.0.0.1:9100` to `192.227.147.101:9100`.
- US node_exporter was intentionally not added as a target, following TICKET-2 section 4.3.

### TICKET-2 Stage 3

Node exporter firewall source was migrated to allow US `104.168.30.204`.

Changed nodes:

- `byvirt-jp`
  - UFW allow added: `104.168.30.204 -> 9100/tcp`
  - Old Dublin allow retained.
  - Reboot persistence test completed successfully on this first node.
- `LengendVPS-SG`
  - UFW allow added: `104.168.30.204 -> 9100/tcp`
  - Old Dublin allow retained.
- `Massivegird-Longdong`
  - UFW inactive; existing rule was a systemd oneshot using iptables-nft.
  - Drop-in added: `/etc/systemd/system/prometheus-node-exporter-firewall.service.d/10-us-prometheus.conf`
  - Runtime order: Dublin allow, US allow, drop all other `9100`.
- `racknerd-dublin`
  - UFW allow added: `104.168.30.204 -> 9100/tcp`
  - Old Dublin/self allow retained.

Validation performed:

- US can reach each migrated node on `9100`.
- Dublin old path remained reachable during migration where applicable.
- WDC third-party checks to `9100` timed out.
- US Prometheus targets became `up` without Prometheus restart after each firewall change.

### TICKET-2 Stage 4

Dublin Prometheus removed:

```bash
docker stop prometheus
docker rm prometheus
```

Retained on Dublin:

- `/opt/prometheus/prometheus.yml`
- `/opt/prometheus/docker-compose.yml`
- `/opt/prometheus/data`
- backup files under `/opt/prometheus`

Dublin still runs:

- `node_exporter` on `*:9100`

Dublin no longer listens on:

- `127.0.0.1:9090`

### TICKET-2 Stage 5

US blackbox deployed:

- Config: `/opt/blackbox/blackbox.yml`
- Container: `blackbox_exporter`
- Image: `prom/blackbox-exporter:latest`
- Host binding: `127.0.0.1:9115`
- Docker port: `9115/tcp -> 127.0.0.1:9115`
- Docker network added: `prometheus-monitor`

Prometheus config backups:

- `/opt/prometheus/config/prometheus.yml.bak-blackbox-20260531081344`
- `/opt/prometheus/config/prometheus.yml.bak-blackbox-network-20260531081610`

Blackbox targets currently configured:

- `https://api.430123.xyz/health`
- `https://api2.430123.xyz/health`

Docker-network note:

- Prometheus cannot scrape blackbox at `127.0.0.1:9115` from inside the Prometheus container.
- Both containers were attached to Docker network `prometheus-monitor`.
- Prometheus scrape address is `blackbox_exporter:9115`.
- Host port `9115` remains loopback-only for local debugging.

## Last Verified State

US Prometheus `up` returned:

```text
node_exporter      LengendVPS-SG                         Singapore  1
node_exporter      Massivegird-Longdong                  London     1
node_exporter      racknerd-dublin                       Dublin     1
node_exporter      byvirt-jp                             Japan      1
blackbox_http_2xx  https://api.430123.xyz/health                    1
blackbox_http_2xx  https://api2.430123.xyz/health                   1
```

`probe_success` returned:

```text
https://api.430123.xyz/health   1
https://api2.430123.xyz/health  1
```

Host bindings verified:

```text
127.0.0.1:9090
127.0.0.1:9115
```

No public bind was seen for `9090` or `9115`.

### TICKET-2 Stage 6

US Aurora/Next.js frontend deployed:

- Repo: `https://github.com/DXH430725/project-aurora-starter`
- Checkout path: `/opt/aurora`
- Commit at deploy time: `a45ce96`
- Local compatibility patch:
  - `PROMETHEUS_NODE_JOB` defaults to `node_exporter`
  - `PROMETHEUS_BLACKBOX_JOB` defaults to `blackbox_http_2xx`
- Env file: `/opt/aurora/.env.production.local`
- Env file owner/mode: `aurora:aurora 600`
- Systemd service: `aurora.service`
- Service user: `aurora`
- Start command:

```bash
/usr/bin/corepack pnpm exec next start -H 127.0.0.1 -p 3000
```

Validation performed:

- `aurora.service` is enabled and active.
- `ss` shows `127.0.0.1:3000` only.
- Local authenticated `/api/monitor` returned 4 node targets, all up:
  - `byvirt-jp` / `Japan`
  - `LengendVPS-SG` / `Singapore`
  - `Massivegird-Longdong` / `London`
  - `racknerd-dublin` / `Dublin`
- Local authenticated `/api/status` returned 2 blackbox services, all up:
  - `https://api.430123.xyz/health`
  - `https://api2.430123.xyz/health`
- Third-party direct curl with proxy variables cleared to `http://104.168.30.204:3000/` timed out.

Note: TICKET-2 intentionally excludes US node_exporter from Prometheus scraping, so `/api/monitor` returning 4 nodes is expected.

2026-06-04 all-node status panel update:

- Previous 4-node behavior came from `/opt/prometheus/config/prometheus.yml`,
  where only Dublin, JP, London, and SG were configured as `node_exporter`
  targets. US was explicitly excluded by the migration ticket note, and WDC /
  Tencent-CN were not configured.
- Prometheus `node_exporter` targets now cover:
  - `racknerd-dublin` / `192.227.147.101:9100`
  - `racknerd-us` / `172.17.0.1:9100` from the Prometheus container
  - `byvirt-jp` / `155.117.155.95:9100`
  - `Massivegird-Longdong` / `185.184.69.81:9100`
  - `Tencent-CN` / `106.55.164.100:9100`
  - `LengendVPS-SG` / `217.116.171.135:9100`
  - `wdc-usa-1` / `216.22.13.154:9100`
- WDC now runs `node-exporter-docker.service` using
  `quay.io/prometheus/node-exporter:latest`; UFW allows `104.168.30.204` to
  `9100/tcp`.
- US UFW allows Docker bridge `172.17.0.0/16` to local `9100/tcp` so the
  Prometheus container can scrape the host exporter without public hairpin.
- `status.430123.xyz/api/summary` now reads the real Prometheus snapshot instead
  of mock data. Current expected result after the change is `nodes_total=7`,
  `nodes_up=6`, `alerts=1`.
- Aurora status frontend changes:
  - `/opt/aurora-multizone/packages/ui/src/hooks/use-monitor-api.ts`
    defaults monitor range queries to `12 * 60` minutes and polls range data
    every 60s.
  - `/opt/aurora-multizone/packages/ui/src/lib/prometheus.ts` caps range output
    at about 144 points per series; 12h therefore uses `step=300s`.
  - `/opt/aurora-multizone/apps/status/src/app/api/summary/route.ts` uses
    `fetchMonitorSnapshotFromPrometheus()` unless `MONITOR_MOCK=1`.
- Rollback backups:
  - `/opt/prometheus/config/prometheus.yml.bak-allnodes-20260604-bridge`
  - `/opt/aurora-multizone/packages/ui/src/lib/prometheus.ts.bak-20260604-allnodes-12h`
  - `/opt/aurora-multizone/packages/ui/src/hooks/use-monitor-api.ts.bak-20260604-allnodes-12h`
  - `/opt/aurora-multizone/apps/status/src/app/api/summary/route.ts.bak-20260604-allnodes-12h`

Post-validation performance patch:

- Initial browser login felt slow because the authenticated home page auto-prefetched multiple Next.js routes and chunks immediately after login.
- Service-side timing was not the bottleneck: `/api/auth` and authenticated pages were around 80-125 ms from origin tests.
- Applied patch on US `/opt/aurora`:
  - disabled automatic Next.js `Link` prefetch for navigation links
  - disabled default WebSocket connection when `NEXT_PUBLIC_WS_URL` is unset
- Backup before patch: `/opt/aurora-backup-before-perf-20260531105801.tar.gz`.
- Rebuilt with `/usr/bin/corepack pnpm build` and restarted `aurora.service`.
- Recheck after patch:
  - `/api/auth`: HTTP 200, total about 0.17s in origin test
  - `/`: HTTP 200, total about 0.11s in origin test
  - `/monitor`: HTTP 200, total about 0.09s in origin test
  - `/status`: HTTP 200, total about 0.10s in origin test
  - no marker-test evidence of mass route prefetch after login

## Current Pause Point

Paused after TICKET-3 completion.

Do not change Nginx, Cloudflare, or monitoring components further without a new instruction.

Operator validation still recommended:

1. Open `https://status.430123.xyz/` in a browser.
2. Log in through the Aurora gate.
3. Confirm `/monitor` and `/status` show real data.
4. Confirm `api2.430123.xyz` AI/API behavior is unchanged from the operator side.

## TICKET-3 Public Exposure

Stage 1 read-only reconnaissance concluded the US 80/443 shape is Nginx, i.e. TICKET-3 shape A:

- Nginx listens on public 80/443.
- `api2.430123.xyz` is an independent Nginx server block.
- `api2.430123.xyz` proxies to `https://api.430123.xyz`.
- Existing certbot webroot flow uses `/var/www/html`.
- No sub2api process, upstream, or config was modified.

Stage 2 changes:

- Added `/etc/nginx/sites-available/status.430123.xyz`.
- Enabled `/etc/nginx/sites-enabled/status.430123.xyz`.
- Issued Let's Encrypt certificate:
  - `/etc/letsencrypt/live/status.430123.xyz/fullchain.pem`
  - `/etc/letsencrypt/live/status.430123.xyz/privkey.pem`
- Added certbot renewal file:
  - `/etc/letsencrypt/renewal/status.430123.xyz.conf`
- Configured HTTPS reverse proxy to Aurora:

```nginx
proxy_pass http://127.0.0.1:3000;
```

- Added redirect handling so Aurora gate redirects stay on `https://status.430123.xyz/...`.
- Used `nginx -t` before reload.
- Used `systemctl reload nginx`, not restart.
- No firewall rule was added.

Validation performed:

- DNS for `status.430123.xyz` resolves through Cloudflare to the US origin.
- `https://status.430123.xyz/` returns a gate redirect to `https://status.430123.xyz/gate?from=%2F`.
- Origin certificate subject is `CN = status.430123.xyz`, valid until 2026-08-29.
- Authenticated origin-SNI API checks returned:
  - `/api/monitor`: 4 nodes, 4 up
  - `/api/status`: 2 services, 2 up
- Cloudflare path to `/gate` returned `HTTP/2 200`.
- `https://api2.430123.xyz/` returned `HTTP/2 200` after Nginx reload.
- `certbot renew --dry-run --cert-name status.430123.xyz` succeeded.
- Final bindings:
  - public: Nginx `:80`, `:443`
  - loopback only: `127.0.0.1:3000`, `127.0.0.1:9090`, `127.0.0.1:9115`

## Rollback Pointers

US Prometheus rollback:

```bash
docker rm -f prometheus
rm -rf /opt/prometheus
```

US blackbox rollback:

```bash
docker rm -f blackbox_exporter
docker network disconnect prometheus-monitor prometheus 2>/dev/null || true
docker network rm prometheus-monitor 2>/dev/null || true
rm -rf /opt/blackbox
cp /opt/prometheus/config/prometheus.yml.bak-blackbox-20260531081344 /opt/prometheus/config/prometheus.yml
docker restart prometheus
```

US Aurora rollback:

```bash
systemctl disable --now aurora
rm -f /etc/systemd/system/aurora.service
systemctl daemon-reload
rm -rf /opt/aurora
userdel aurora 2>/dev/null || true
```

US status exposure rollback:

```bash
rm -f /etc/nginx/sites-enabled/status.430123.xyz
rm -f /etc/nginx/sites-available/status.430123.xyz
nginx -t
systemctl reload nginx
certbot delete --cert-name status.430123.xyz --non-interactive
```

Restore Dublin Prometheus from retained data:

```bash
cd /opt/prometheus
docker run -d --name prometheus --restart unless-stopped \
  --net=host \
  -v /opt/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro \
  -v /opt/prometheus/data:/prometheus \
  prom/prometheus:latest \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/prometheus \
  --storage.tsdb.retention.time=15d \
  --web.listen-address=127.0.0.1:9090
```

Remove newly added US source rules:

```bash
# UFW nodes:
ufw delete allow from 104.168.30.204 to any port 9100 proto tcp

# London:
rm -f /etc/systemd/system/prometheus-node-exporter-firewall.service.d/10-us-prometheus.conf
systemctl daemon-reload
systemctl restart prometheus-node-exporter-firewall.service
iptables -D INPUT -p tcp -s 104.168.30.204 --dport 9100 -j ACCEPT 2>/dev/null || true
```

Keep old Dublin-source rules unless explicitly retiring the old fallback path.
