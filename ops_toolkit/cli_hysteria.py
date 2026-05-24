"""Hysteria2 deployment toolkit command."""

import base64
import datetime as dt
import json
import random
import re
import shlex
import socket
import string
from pathlib import Path
from urllib.parse import quote

import yaml

from . import inventory
from .cli_helpers import (
    _get_vault,
    _health_from_status,
    _inventory_meta,
    _is_china_host,
    _json_out,
    _load_mapping_file,
    _parse_domain_map_assignments,
    _resolve_server_domain,
    _resolve_servers,
)
from .ssh import ServerSession

DEFAULT_HYSTERIA_INSTALL_SCRIPT = "https://get.hy2.sh/"


def _random_alnum(length: int) -> str:
    pool = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(pool) for _ in range(length))


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


def _parse_port_range(text: str) -> tuple[int, int]:
    raw = (text or "").strip()
    m = re.fullmatch(r"(\d+)-(\d+)", raw)
    if not m:
        raise ValueError(f"Invalid port range format: {text}. Use start-end, e.g. 20000-50000")
    start = int(m.group(1))
    end = int(m.group(2))
    if start < 1 or start > 65535 or end < 1 or end > 65535 or start > end:
        raise ValueError(f"Invalid port range value: {text}")
    return start, end


def _pick_auth(auth_value: str | None) -> str:
    return auth_value or _random_alnum(24)


def _build_server_config_yaml(
    *,
    listen_range: str,
    domain: str,
    acme_email: str,
    auth: str,
    masquerade_url: str,
    cert_file: str | None,
    key_file: str | None,
) -> str:
    config = {
        "listen": f":{listen_range}",
        "auth": {
            "type": "password",
            "password": auth,
        },
        "masquerade": {
            "type": "proxy",
            "proxy": {
                "url": masquerade_url,
                "rewriteHost": True,
            },
        },
    }
    if cert_file and key_file:
        config["tls"] = {
            "cert": cert_file,
            "key": key_file,
        }
    else:
        config["acme"] = {
            "domains": [domain],
            "email": acme_email,
        }
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def _build_client_yaml(*, domain: str, listen_range: str, auth: str, hop_interval: str, socks_port: int, http_port: int) -> str:
    config = {
        "server": f"{domain}:{listen_range}",
        "auth": auth,
        "tls": {
            "sni": domain,
            "insecure": False,
        },
        "transport": {
            "type": "udp",
            "udp": {
                "hopInterval": hop_interval,
            },
        },
        "socks5": {
            "listen": f"127.0.0.1:{socks_port}",
        },
        "http": {
            "listen": f"127.0.0.1:{http_port}",
        },
    }
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def _build_hysteria_uri(*, auth: str, domain: str, listen_range: str, sni: str, label: str) -> str:
    auth_part = quote(auth, safe="")
    server_part = f"{domain}:{listen_range}"
    query = f"sni={quote(sni, safe='')}"
    frag = quote(label, safe="")
    return f"hysteria2://{auth_part}@{server_part}/?{query}#{frag}"


def _hysteria_install_cmd(
    *,
    config_yaml: str,
    listen_range: str,
    ufw_udp_range: str,
    ssh_port: int,
    open_firewall: bool,
    run_as_root: bool,
    install_script_url: str,
    version: str | None,
    firewall_backend: str | None,
) -> str:
    q_script = shlex.quote(install_script_url)
    q_listen_range = shlex.quote(listen_range)
    q_ssh = shlex.quote(str(ssh_port))
    q_ufw_udp_range = shlex.quote(ufw_udp_range)
    q_open_firewall = "1" if open_firewall else "0"
    q_run_as_root = "1" if run_as_root else "0"
    q_version = shlex.quote(version or "")
    q_backend = shlex.quote(firewall_backend or "")

    return f"""
set -e
export DEBIAN_FRONTEND=noninteractive
if [ "$(id -u)" -eq 0 ]; then SUDO=''; else SUDO='sudo -n '; fi
INSTALL_SCRIPT_URL={q_script}
LISTEN_RANGE={q_listen_range}
SSH_PORT={q_ssh}
OPEN_FIREWALL={q_open_firewall}
RUN_AS_ROOT={q_run_as_root}
HY2_VERSION={q_version}
FIREWALL_BACKEND={q_backend}
UFW_UDP_RANGE={q_ufw_udp_range}

if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq ca-certificates curl ufw iptables nftables
fi

curl -fsSL -o /tmp/get-hy2.sh "$INSTALL_SCRIPT_URL"
chmod +x /tmp/get-hy2.sh
if [ -n "$HY2_VERSION" ]; then
  if [ "$RUN_AS_ROOT" = "1" ]; then
    HYSTERIA_USER=root bash /tmp/get-hy2.sh --version "$HY2_VERSION"
  else
    bash /tmp/get-hy2.sh --version "$HY2_VERSION"
  fi
else
  if [ "$RUN_AS_ROOT" = "1" ]; then
    HYSTERIA_USER=root bash /tmp/get-hy2.sh
  else
    bash /tmp/get-hy2.sh
  fi
fi

$SUDO mkdir -p /etc/hysteria
if [ -f /etc/hysteria/config.yaml ]; then
  $SUDO cp -a /etc/hysteria/config.yaml /etc/hysteria/config.yaml.bak.$(date +%Y%m%d%H%M%S)
fi
cat >/tmp/hysteria-config.yaml <<'EOF'
{config_yaml}
EOF
$SUDO cp /tmp/hysteria-config.yaml /etc/hysteria/config.yaml

if [ -n "$FIREWALL_BACKEND" ] && [ "$FIREWALL_BACKEND" != "auto" ]; then
  $SUDO mkdir -p /etc/systemd/system/hysteria-server.service.d
  cat >/tmp/hysteria-override.conf <<EOF
[Service]
Environment=HYSTERIA_FIREWALL_BACKEND=$FIREWALL_BACKEND
EOF
  $SUDO cp /tmp/hysteria-override.conf /etc/systemd/system/hysteria-server.service.d/override.conf
fi

$SUDO systemctl daemon-reload
$SUDO systemctl enable hysteria-server.service >/dev/null 2>&1 || true
$SUDO systemctl restart hysteria-server.service
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  state=$($SUDO systemctl show -p ActiveState --value hysteria-server.service 2>/dev/null || true)
  [ "$state" = "active" ] && break
  sleep 2
done
sleep 6
$SUDO systemctl show -p ActiveState --value hysteria-server.service || true

if [ "$OPEN_FIREWALL" = "1" ]; then
  if command -v ufw >/dev/null 2>&1; then
    $SUDO ufw allow "${{SSH_PORT}}/tcp" >/dev/null 2>&1 || true
    $SUDO ufw allow "${{UFW_UDP_RANGE}}/udp" >/dev/null 2>&1 || true
    $SUDO ufw allow 80/tcp >/dev/null 2>&1 || true
    $SUDO ufw allow 443/tcp >/dev/null 2>&1 || true
    $SUDO ufw --force enable >/dev/null 2>&1 || true
  fi
fi
""".strip()


def _find_existing_cert_files(session: ServerSession, domain: str) -> tuple[str, str] | None:
    q_domain = shlex.quote(domain)
    cmd = f"""
set -e
DOMAIN={q_domain}
if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] && [ -f "/etc/letsencrypt/live/$DOMAIN/privkey.pem" ]; then
  echo "/etc/letsencrypt/live/$DOMAIN/fullchain.pem|/etc/letsencrypt/live/$DOMAIN/privkey.pem"
  exit 0
fi
for root in /var/lib/naiveproxy /var/lib/caddy /root/.local/share/caddy /var/lib/caddy/.local/share/caddy
do
  [ -d "$root" ] || continue
  found_crt=$(find "$root" -type f -path "*/certificates/*/$DOMAIN/$DOMAIN.crt" 2>/dev/null | head -n 1 || true)
  if [ -n "$found_crt" ]; then
    found_key="${{found_crt%.crt}}.key"
    if [ -f "$found_key" ]; then
      echo "$found_crt|$found_key"
      exit 0
    fi
  fi
done
""".strip()
    out = session.exec(cmd, timeout=40).get("stdout", "").strip()
    if "|" not in out:
        return None
    cert_file, key_file = out.split("|", 1)
    cert_file = cert_file.strip()
    key_file = key_file.strip()
    if not cert_file or not key_file:
        return None
    return cert_file, key_file


def _update_hysteria_meta(
    *,
    server_name: str,
    domain: str,
    listen_range: str,
    masquerade_url: str,
    hop_interval: str,
    client_yaml_path: str,
    client_json_path: str,
):
    inv = inventory.load()
    meta = _inventory_meta(inv, server_name)
    domains = meta.get("domains", [])
    if not isinstance(domains, list):
        domains = []
    if domain and domain not in domains:
        domains.append(domain)
    meta["domains"] = domains
    meta["hysteria2"] = {
        "domain": domain,
        "listen_range": listen_range,
        "hop_interval": hop_interval,
        "masquerade_url": masquerade_url,
        "client_yaml": client_yaml_path,
        "client_json": client_json_path,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    inventory.save(inv)


def cmd_toolkit_hysteria_deploy(args):
    start_port, end_port = _parse_port_range(args.listen_range)
    ufw_udp_range = f"{start_port}:{end_port}"
    if bool(args.cert_file) != bool(args.key_file):
        raise ValueError("--cert-file and --key-file must be provided together")
    if args.firewall_backend and args.firewall_backend not in {"auto", "iptables", "nftables"}:
        raise ValueError("--firewall-backend must be one of: auto, iptables, nftables")

    vault = _get_vault()
    inv = inventory.load()
    domain_by_server = _parse_domain_map_assignments(args.domain_by_server)
    domain_map_file = _load_mapping_file(args.domain_map_file) if args.domain_map_file else None
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
    all_uris = []
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
            auth = _pick_auth(args.auth)
            masquerade_url = args.masquerade_url
            if not masquerade_url.endswith("/"):
                masquerade_url += "/"
            uri = _build_hysteria_uri(
                auth=auth,
                domain=selected_domain,
                listen_range=args.listen_range,
                sni=selected_domain,
                label=f"{name}-hysteria2-{selected_domain}",
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
                    "Skip deploy for safety. Use --allow-domain-mismatch to override."
                )

            client_yaml = _build_client_yaml(
                domain=selected_domain,
                listen_range=args.listen_range,
                auth=auth,
                hop_interval=args.hop_interval,
                socks_port=args.socks_port,
                http_port=args.http_port,
            )
            client_json = {
                "type": "hysteria2",
                "server": name,
                "ip": server_ip,
                "domain": selected_domain,
                "listen_range": args.listen_range,
                "auth": auth,
                "sni": selected_domain,
                "hop_interval": args.hop_interval,
                "masquerade_url": masquerade_url,
                "uri": uri,
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }

            local_yaml_path = output_root / f"{name}-hysteria2-client.yaml"
            local_json_path = output_root / f"{name}-hysteria2-client.json"
            local_uri_path = output_root / f"{name}-hysteria2-uri.txt"
            local_yaml_path.write_text(client_yaml, encoding="utf-8")
            local_json_path.write_text(
                json.dumps(client_json, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            local_uri_path.write_text(uri + "\n", encoding="utf-8")

            deploy = {"exit_code": 0, "stdout": "", "stderr": ""}
            before = None
            after = None
            active_state = ""
            listen_out = ""
            ufw_out = ""
            logs_out = ""
            tls_mode = "acme"
            cert_file = args.cert_file
            key_file = args.key_file
            if not args.dry_run:
                with ServerSession(name, vault) as s:
                    if (not cert_file or not key_file) and not args.no_prefer_existing_cert:
                        found = _find_existing_cert_files(s, selected_domain)
                        if found:
                            cert_file, key_file = found
                    if cert_file and key_file:
                        tls_mode = "static-cert"
                    config_yaml = _build_server_config_yaml(
                        listen_range=args.listen_range,
                        domain=selected_domain,
                        acme_email=acme_email,
                        auth=auth,
                        masquerade_url=masquerade_url,
                        cert_file=cert_file,
                        key_file=key_file,
                    )
                    before = s.system_status()
                    cmd = _hysteria_install_cmd(
                        config_yaml=config_yaml,
                        listen_range=args.listen_range,
                        ufw_udp_range=ufw_udp_range,
                        ssh_port=ssh_port,
                        open_firewall=not args.no_open_firewall,
                        run_as_root=not args.no_run_as_root,
                        install_script_url=args.install_script_url,
                        version=args.version,
                        firewall_backend=args.firewall_backend,
                    )
                    deploy = s.exec(cmd, timeout=args.timeout)
                    active = s.exec(
                        "systemctl show -p ActiveState --value hysteria-server.service 2>/dev/null || true",
                        timeout=40,
                    )
                    active_state = (active.get("stdout", "") or "").strip().splitlines()[-1] if active.get("stdout", "") else ""
                    listen_out = s.exec(
                        f"ss -lunp | grep -E ':{start_port}( |$)' || true",
                        timeout=40,
                    ).get("stdout", "")
                    ufw_out = s.exec("ufw status | sed -n '1,60p' || true", timeout=60).get("stdout", "")
                    logs_out = s.exec(
                        "journalctl --no-pager -n 80 -u hysteria-server.service || true",
                        timeout=60,
                    ).get("stdout", "")
                    after = s.system_status()
                if deploy.get("exit_code", 1) != 0:
                    raise RuntimeError(
                        f"Deploy script failed (exit={deploy.get('exit_code', 1)}): "
                        f"{deploy.get('stderr', '')[-400:] or deploy.get('stdout', '')[-400:]}"
                    )
                if active_state != "active":
                    raise RuntimeError(f"hysteria-server not active (state={active_state or 'unknown'})")
            else:
                item["dry_run"] = True
                if cert_file and key_file:
                    tls_mode = "static-cert"

            if not args.no_write_meta:
                _update_hysteria_meta(
                    server_name=name,
                    domain=selected_domain,
                    listen_range=args.listen_range,
                    masquerade_url=masquerade_url,
                    hop_interval=args.hop_interval,
                    client_yaml_path=str(local_yaml_path),
                    client_json_path=str(local_json_path),
                )

            item.update(
                {
                    "ip": server_ip,
                    "domain": selected_domain,
                    "listen_range": args.listen_range,
                    "auth_length": len(auth),
                    "hop_interval": args.hop_interval,
                    "masquerade_url": masquerade_url,
                    "tls_mode": tls_mode,
                    "cert_file": cert_file or "",
                    "key_file": key_file or "",
                    "uri": uri,
                    "client_yaml_file": str(local_yaml_path),
                    "client_json_file": str(local_json_path),
                    "client_uri_file": str(local_uri_path),
                    "before": before,
                    "after": after,
                    "health_after": (
                        _health_from_status(
                            after,
                            disk_warn=args.disk_warn,
                            disk_crit=args.disk_crit,
                            mem_warn=args.mem_warn,
                            load_warn=args.load_warn,
                        )
                        if after
                        else None
                    ),
                    "deploy_exit_code": deploy.get("exit_code", 1),
                    "deploy_stdout_tail": (deploy.get("stdout", "") or "")[-4000:],
                    "deploy_stderr_tail": (deploy.get("stderr", "") or "")[-2000:],
                    "service_active": active_state,
                    "listen_check": listen_out.splitlines() if listen_out else [],
                    "ufw_status": ufw_out.splitlines() if ufw_out else [],
                    "service_logs_tail": logs_out.splitlines()[-80:] if logs_out else [],
                }
            )
            all_uris.append(uri)
        except Exception as e:
            item["error"] = str(e)
        results.append(item)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    plain = "\n".join(all_uris)
    b64 = base64.b64encode(plain.encode("utf-8")).decode("ascii") if plain else ""
    plain_file = output_root / f"hysteria2-uris-{ts}.txt"
    b64_file = output_root / f"hysteria2-uris-{ts}.base64"
    if plain:
        plain_file.write_text(plain + "\n", encoding="utf-8")
        b64_file.write_text(b64 + "\n", encoding="utf-8")

    _json_out(
        {
            "workflow": "hysteria-deploy",
            "target_servers": servers,
            "total": len(servers),
            "success": sum(1 for r in results if r.get("service_active") == "active" or r.get("dry_run")),
            "output_root": str(output_root),
            "uris_file": str(plain_file) if plain else "",
            "uris_base64_file": str(b64_file) if b64 else "",
            "results": results,
        }
    )
