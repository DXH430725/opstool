"""NaiveProxy deployment toolkit commands."""

import base64
import datetime as dt
import json
import random
import shlex
import socket
import string
import subprocess
from pathlib import Path
from urllib.parse import quote

from . import inventory
from .cli_helpers import (
    _get_vault,
    _health_from_status,
    _inventory_meta,
    _is_china_host,
    _json_out,
    _parse_domain_map_assignments,
    _resolve_server_domain,
    _resolve_servers,
)
from .ssh import ServerSession

DEFAULT_NAIVE_BINARY_URL = (
    "https://github.com/klzgrad/forwardproxy/releases/download/"
    "v2.10.0-naive/caddy-forwardproxy-naive.tar.xz"
)


def _random_alnum(length: int) -> str:
    pool = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(pool) for _ in range(length))


def _pick_credentials(username: str | None, password: str | None) -> tuple[str, str]:
    user = username or f"np{_random_alnum(6)}"
    passwd = password or _random_alnum(20)
    return user, passwd


def _resolve_ipv4(domain: str) -> list[str]:
    found = set()
    try:
        infos = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
        for item in infos:
            ip = item[4][0]
            if ip:
                found.add(ip)
    except Exception:
        return []
    return sorted(found)


def _build_client_json(proxy_url: str, socks_port: int, http_port: int) -> dict:
    return {
        "listen": [
            f"socks://127.0.0.1:{socks_port}",
            f"http://127.0.0.1:{http_port}",
        ],
        "proxy": proxy_url,
        "log": "",
    }


def _naive_install_cmd(
    *,
    domain: str,
    port: int,
    username: str,
    password: str,
    acme_email: str,
    ssh_port: int,
    open_firewall: bool,
    keep_old_port: bool,
    old_port: int,
    binary_url: str,
    acme_ca: str | None,
) -> str:
    q_domain = shlex.quote(domain)
    q_port = shlex.quote(str(port))
    q_user = shlex.quote(username)
    q_pass = shlex.quote(password)
    q_email = shlex.quote(acme_email)
    q_ssh = shlex.quote(str(ssh_port))
    q_keep_old = "1" if keep_old_port else "0"
    q_old_port = shlex.quote(str(old_port))
    q_bin = shlex.quote(binary_url)
    q_fw = "1" if open_firewall else "0"
    q_acme_ca = shlex.quote(acme_ca) if acme_ca else "''"

    return f"""
set -e
export DEBIAN_FRONTEND=noninteractive
if [ "$(id -u)" -eq 0 ]; then SUDO=''; else SUDO='sudo -n '; fi
DOMAIN={q_domain}
PORT={q_port}
NAIVE_USER={q_user}
NAIVE_PASS={q_pass}
ACME_EMAIL={q_email}
SSH_PORT={q_ssh}
KEEP_OLD_PORT={q_keep_old}
OLD_PORT={q_old_port}
OPEN_FIREWALL={q_fw}
BINARY_URL={q_bin}
ACME_CA={q_acme_ca}

if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq ca-certificates curl tar xz-utils
fi

$SUDO mkdir -p /opt/naiveproxy /etc/naiveproxy /var/www/naiveproxy /var/lib/naiveproxy /var/log/naiveproxy

cd /tmp
rm -f caddy-forwardproxy-naive.tar.xz
rm -rf caddy-forwardproxy-naive
curl -fsSL -o caddy-forwardproxy-naive.tar.xz "$BINARY_URL"
tar -xf caddy-forwardproxy-naive.tar.xz
$SUDO install -m 0755 caddy-forwardproxy-naive/caddy /opt/naiveproxy/caddy

cat >/tmp/naive-index.html <<'EOF'
<html><body><h1>naiveproxy ok</h1></body></html>
EOF
$SUDO cp /tmp/naive-index.html /var/www/naiveproxy/index.html

if [ -f /etc/naiveproxy/Caddyfile ]; then
  $SUDO cp -a /etc/naiveproxy/Caddyfile /etc/naiveproxy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)
fi

CERT_FULL="/etc/letsencrypt/live/${{DOMAIN}}/fullchain.pem"
CERT_KEY="/etc/letsencrypt/live/${{DOMAIN}}/privkey.pem"
PORT80_OCCUPIED=0
if ss -lnt '( sport = :80 )' | tail -n +2 | grep -q .; then
  PORT80_OCCUPIED=1
fi
USE_STATIC_CERT=0
if [ "$PORT80_OCCUPIED" = "1" ] && [ -f "$CERT_FULL" ] && [ -f "$CERT_KEY" ]; then
  USE_STATIC_CERT=1
fi

if [ "$USE_STATIC_CERT" = "1" ]; then
  cat >/tmp/Caddyfile <<EOF
{{
  order forward_proxy first
  auto_https off
}}
:${{PORT}}, ${{DOMAIN}}:${{PORT}} {{
  tls $CERT_FULL $CERT_KEY
  forward_proxy {{
    basic_auth ${{NAIVE_USER}} ${{NAIVE_PASS}}
    hide_ip
    hide_via
    probe_resistance
  }}
  file_server {{
    root /var/www/naiveproxy
  }}
}}
EOF
  echo "NAIVE_MODE=static_cert"
else
  if [ -n "$ACME_CA" ]; then
    GLOBAL_ACME_CA="  acme_ca $ACME_CA"
  else
    GLOBAL_ACME_CA=""
  fi
  cat >/tmp/Caddyfile <<EOF
{{
  order forward_proxy first
$GLOBAL_ACME_CA
}}
:${{PORT}}, ${{DOMAIN}}:${{PORT}} {{
  tls {{
    issuer acme {{
      email ${{ACME_EMAIL}}
      disable_tlsalpn_challenge
    }}
  }}
  forward_proxy {{
    basic_auth ${{NAIVE_USER}} ${{NAIVE_PASS}}
    hide_ip
    hide_via
    probe_resistance
  }}
  file_server {{
    root /var/www/naiveproxy
  }}
}}
EOF
  echo "NAIVE_MODE=acme"
fi
$SUDO cp /tmp/Caddyfile /etc/naiveproxy/Caddyfile

cat >/tmp/naiveproxy-caddy.service <<'EOF'
[Unit]
Description=NaiveProxy Caddy (independent)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=XDG_DATA_HOME=/var/lib/naiveproxy
Environment=XDG_CONFIG_HOME=/etc/naiveproxy
ExecStart=/opt/naiveproxy/caddy run --config /etc/naiveproxy/Caddyfile --adapter caddyfile
ExecReload=/opt/naiveproxy/caddy reload --config /etc/naiveproxy/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=2
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
$SUDO cp /tmp/naiveproxy-caddy.service /etc/systemd/system/naiveproxy-caddy.service

$SUDO systemctl daemon-reload
$SUDO systemctl enable naiveproxy-caddy >/dev/null 2>&1 || true
$SUDO systemctl restart naiveproxy-caddy
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  state=$($SUDO systemctl show -p ActiveState --value naiveproxy-caddy 2>/dev/null || true)
  [ "$state" = "active" ] && break
  sleep 2
done
$SUDO systemctl show -p ActiveState --value naiveproxy-caddy || true

if [ "$OPEN_FIREWALL" = "1" ]; then
  if ! command -v ufw >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      $SUDO apt-get install -y -qq ufw
    fi
  fi
  if command -v ufw >/dev/null 2>&1; then
    $SUDO ufw allow "${{SSH_PORT}}/tcp" >/dev/null 2>&1 || true
    $SUDO ufw allow 80/tcp >/dev/null 2>&1 || true
    $SUDO ufw allow "${{PORT}}/tcp" >/dev/null 2>&1 || true
    if [ "$KEEP_OLD_PORT" = "0" ] && [ "$OLD_PORT" != "$PORT" ]; then
      $SUDO ufw --force delete allow "${{OLD_PORT}}/tcp" >/dev/null 2>&1 || true
    fi
    $SUDO ufw --force enable >/dev/null 2>&1 || true
  fi
fi

ss -lntp | egrep ":80 |:${{PORT}} " || true
""".strip()


def _run_local_curl(args: list[str], timeout: int = 40) -> dict:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
            "command": args,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "command": args}


def _verify_connectivity(domain: str, port: int, proxy_url: str) -> dict:
    endpoint = _run_local_curl(
        ["curl", "-I", "--max-time", "25", f"https://{domain}:{port}/"]
    )
    if not endpoint.get("ok"):
        endpoint_insecure = _run_local_curl(
            [
                "curl",
                "-k",
                "--ssl-no-revoke",
                "-I",
                "--max-time",
                "25",
                f"https://{domain}:{port}/",
            ]
        )
        endpoint["insecure_retry"] = endpoint_insecure

    proxy = _run_local_curl(
        [
            "curl",
            "-I",
            "--max-time",
            "30",
            "-x",
            proxy_url,
            "https://www.google.com",
        ]
    )
    if not proxy.get("ok"):
        proxy_insecure = _run_local_curl(
            [
                "curl",
                "-k",
                "--ssl-no-revoke",
                "--proxy-insecure",
                "-I",
                "--max-time",
                "30",
                "-x",
                proxy_url,
                "https://www.google.com",
            ]
        )
        proxy["insecure_retry"] = proxy_insecure
    return {"local": {"endpoint": endpoint, "proxy": proxy}}


def _verify_remote_proxy(
    session: ServerSession,
    *,
    domain: str,
    port: int,
    username: str,
    password: str,
) -> dict:
    cmd = (
        "set -e; "
        f"curl -k -I --max-time 20 https://{shlex.quote(domain)}:{int(port)}/; "
        "echo '---'; "
        f"curl -k -I --max-time 25 -x "
        f"\"https://{shlex.quote(username)}:{shlex.quote(password)}@{shlex.quote(domain)}:{int(port)}\" "
        "https://www.google.com"
    )
    r = session.exec(cmd, timeout=120)
    return {
        "ok": r.get("exit_code", 1) == 0,
        "exit_code": r.get("exit_code", 1),
        "stdout": r.get("stdout", "")[-4000:],
        "stderr": r.get("stderr", "")[-2000:],
    }


def _update_naive_meta(server_name: str, domain: str, port: int, username: str, local_json_path: str):
    inv = inventory.load()
    meta = _inventory_meta(inv, server_name)
    domains = meta.get("domains", [])
    if not isinstance(domains, list):
        domains = []
    if domain and domain not in domains:
        domains.append(domain)
    meta["domains"] = domains
    meta["naiveproxy"] = {
        "domain": domain,
        "port": port,
        "username": username,
        "client_json": local_json_path,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    inventory.save(inv)


def cmd_toolkit_naive_deploy(args):
    vault = _get_vault()
    inv = inventory.load()
    domain_by_server = _parse_domain_map_assignments(args.domain_by_server)
    domain_map_file = None
    if args.domain_map_file:
        p = Path(args.domain_map_file)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".json":
            domain_map_file = json.loads(text)
        else:
            import yaml
            domain_map_file = yaml.safe_load(text)

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    excluded = set(args.exclude_server or [])
    servers = []
    for name in _resolve_servers(vault, args.server):
        if name in excluded:
            continue
        if args.exclude_china and _is_china_host(name, inv):
            continue
        servers.append(name)

    results = []
    all_proxy_urls = []
    for name in servers:
        item = {"server": name}
        try:
            creds = vault.get(name)
            server_ip = creds.get("ip", "")
            ssh_port = int(creds.get("port", 22))
            selected_domain = args.domain or _resolve_server_domain(
                server_name=name,
                inv=inv,
                default_domain=args.default_domain,
                domain_by_server=domain_by_server,
                domain_map_file=domain_map_file,
            )
            acme_email = args.acme_email or f"admin@{selected_domain}"
            username, password = _pick_credentials(args.username, args.password)
            proxy_url = (
                f"https://{quote(username, safe='')}:{quote(password, safe='')}"
                f"@{selected_domain}:{args.port}"
            )

            dns_ipv4 = _resolve_ipv4(selected_domain)
            item["dns_check"] = {
                "domain": selected_domain,
                "resolved_ipv4": dns_ipv4,
                "expected_ip": server_ip,
                "matched": (server_ip in dns_ipv4) if dns_ipv4 else False,
            }
            if dns_ipv4 and (server_ip not in dns_ipv4) and not args.allow_domain_mismatch:
                raise RuntimeError(
                    f"Domain '{selected_domain}' resolves to {dns_ipv4}, not server IP {server_ip}. "
                    "Skip deploy for safety. Set DNS to direct origin or use --allow-domain-mismatch."
                )

            with ServerSession(name, vault) as s:
                before = s.system_status()
                cmd = _naive_install_cmd(
                    domain=selected_domain,
                    port=args.port,
                    username=username,
                    password=password,
                    acme_email=acme_email,
                    ssh_port=ssh_port,
                    open_firewall=not args.no_open_firewall,
                    keep_old_port=args.keep_old_port,
                    old_port=args.old_port,
                    binary_url=args.binary_url or DEFAULT_NAIVE_BINARY_URL,
                    acme_ca=args.acme_ca,
                )
                deploy = s.exec(cmd, timeout=args.timeout)
                active = s.exec(
                    "systemctl show -p ActiveState --value naiveproxy-caddy 2>/dev/null || true",
                    timeout=40,
                )
                listen = s.exec(
                    f"ss -lntp | egrep ':80( |$)|:{args.port}( |$)' || true",
                    timeout=40,
                )
                ufw = s.exec("ufw status | sed -n '1,40p' || true", timeout=60)
                after = s.system_status()
                remote_verify = _verify_remote_proxy(
                    s,
                    domain=selected_domain,
                    port=args.port,
                    username=username,
                    password=password,
                )
            active_state = (active.get("stdout", "") or "").strip().splitlines()[-1] if active.get("stdout", "") else ""
            if deploy.get("exit_code", 1) != 0:
                raise RuntimeError(
                    f"Deploy script failed (exit={deploy.get('exit_code', 1)}): "
                    f"{deploy.get('stderr', '')[-400:] or deploy.get('stdout', '')[-400:]}"
                )
            if active_state != "active":
                raise RuntimeError(f"naiveproxy-caddy not active (state={active_state or 'unknown'})")

            client_json = _build_client_json(
                proxy_url=proxy_url,
                socks_port=args.socks_port,
                http_port=args.http_port,
            )
            local_json_path = output_root / f"{name}-naive-v2rayn.json"
            local_json_path.write_text(
                json.dumps(client_json, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            local_info_path = output_root / f"{name}-naive.txt"
            local_info_path.write_text(
                (
                    f"server={name}\n"
                    f"ip={server_ip}\n"
                    f"domain={selected_domain}\n"
                    f"port={args.port}\n"
                    f"username={username}\n"
                    f"password={password}\n"
                    f"proxy_url={proxy_url}\n"
                    f"json_file={local_json_path}\n"
                ),
                encoding="utf-8",
            )

            verify = {"skipped": True}
            if not args.no_verify:
                verify = _verify_connectivity(selected_domain, args.port, proxy_url)
                verify["remote"] = remote_verify
            else:
                verify = {"local": {"skipped": True}, "remote": remote_verify}

            if not args.no_write_meta:
                _update_naive_meta(
                    server_name=name,
                    domain=selected_domain,
                    port=args.port,
                    username=username,
                    local_json_path=str(local_json_path),
                )

            item.update(
                {
                    "ip": server_ip,
                    "domain": selected_domain,
                    "port": args.port,
                    "before": before,
                    "after": after,
                    "health_after": _health_from_status(
                        after,
                        disk_warn=args.disk_warn,
                        disk_crit=args.disk_crit,
                        mem_warn=args.mem_warn,
                        load_warn=args.load_warn,
                    ),
                    "deploy_exit_code": deploy.get("exit_code", 1),
                    "deploy_stdout_tail": deploy.get("stdout", "")[-4000:],
                    "deploy_stderr_tail": deploy.get("stderr", "")[-2000:],
                    "service_active": active_state,
                    "listen": listen.get("stdout", "").splitlines(),
                    "ufw_status": ufw.get("stdout", "").splitlines(),
                    "proxy_url": proxy_url,
                    "client_json_file": str(local_json_path),
                    "client_info_file": str(local_info_path),
                    "verify": verify,
                }
            )
            all_proxy_urls.append(proxy_url)
        except Exception as e:
            item["error"] = str(e)
        results.append(item)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    plain = "\n".join(all_proxy_urls)
    b64 = base64.b64encode(plain.encode("utf-8")).decode("ascii") if plain else ""
    plain_file = output_root / f"naive-proxy-urls-{ts}.txt"
    b64_file = output_root / f"naive-proxy-urls-{ts}.base64"
    if plain:
        plain_file.write_text(plain + "\n", encoding="utf-8")
        b64_file.write_text(b64 + "\n", encoding="utf-8")

    _json_out(
        {
            "workflow": "naive-deploy",
            "target_servers": servers,
            "total": len(servers),
            "success": sum(1 for r in results if r.get("service_active") == "active"),
            "output_root": str(output_root),
            "proxy_urls_file": str(plain_file) if plain else "",
            "proxy_urls_base64_file": str(b64_file) if b64 else "",
            "results": results,
        }
    )
