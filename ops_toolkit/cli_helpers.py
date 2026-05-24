"""Shared CLI helpers and non-REALITY utilities."""

import json
import os
import re
import sys
from pathlib import Path

from . import inventory
from .ssh import ServerSession
from .vault import Vault

BASELINE_PACKAGES = [
    "ca-certificates",
    "curl",
    "wget",
    "git",
    "jq",
    "unzip",
    "zip",
    "rsync",
    "tmux",
    "htop",
    "net-tools",
    "lsof",
    "psmisc",
    "procps",
    "dnsutils",
]

XUI_DB_CANDIDATES = [
    "/etc/x-ui/x-ui.db",
    "/etc/3x-ui/x-ui.db",
    "/usr/local/x-ui/x-ui.db",
    "/usr/local/3x-ui/x-ui.db",
    "/etc/x-ui/xui.db",
]

XRAY_CONFIG_CANDIDATES = [
    "/usr/local/etc/xray/config.json",
    "/usr/local/x-ui/bin/config.json",
]

APP_SNAPSHOT_GLOBS = (
    "-iname '*x-ui*' -o -iname '*3x-ui*' -o -iname '*xray*' "
    "-o -iname '*v2ray*' -o -iname 'config*.json' -o -iname '*.db' "
    "-o -iname '*.bak' -o -iname '*.backup' -o -iname '*.old'"
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RETRIEVED_DIR = DATA_DIR / "retrieved"


def _json_out(data):
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(body)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(body.encode("utf-8", errors="replace"))


def _get_vault() -> Vault:
    try:
        return Vault()
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


def _resolve_servers(vault: Vault, target: str) -> list[str]:
    if target == "all":
        return vault.list_servers()
    return [target]


def _multi(vault, target, fn):
    results = []
    for name in _resolve_servers(vault, target):
        try:
            with ServerSession(name, vault) as s:
                results.append(fn(name, s))
        except Exception as e:
            results.append({"server": name, "error": str(e)})
    _json_out(results if len(results) > 1 else results[0])


def _tail(text: str, size: int = 1200) -> str:
    return text[-size:] if text else ""


def _pct_to_int(value: str) -> int | None:
    if not value:
        return None
    raw = str(value).strip().replace("%", "")
    try:
        return int(float(raw))
    except ValueError:
        return None


def _to_float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _health_from_status(status: dict, disk_warn: int, disk_crit: int, mem_warn: int, load_warn: float) -> dict:
    alerts = []

    disk_pct = _pct_to_int(status.get("disk", {}).get("use_pct", ""))
    if disk_pct is not None:
        if disk_pct >= disk_crit:
            alerts.append({
                "level": "critical",
                "kind": "disk",
                "metric": "disk.use_pct",
                "value": disk_pct,
                "threshold": disk_crit,
                "message": f"Disk usage {disk_pct}% >= {disk_crit}%",
            })
        elif disk_pct >= disk_warn:
            alerts.append({
                "level": "warn",
                "kind": "disk",
                "metric": "disk.use_pct",
                "value": disk_pct,
                "threshold": disk_warn,
                "message": f"Disk usage {disk_pct}% >= {disk_warn}%",
            })

    mem_total = _to_float(status.get("memory", {}).get("total_mb"))
    mem_used = _to_float(status.get("memory", {}).get("used_mb"))
    mem_ratio = None
    if mem_total and mem_total > 0 and mem_used is not None:
        mem_ratio = round((mem_used / mem_total) * 100, 1)
        if mem_ratio >= mem_warn:
            alerts.append({
                "level": "warn",
                "kind": "memory",
                "metric": "memory.used_pct",
                "value": mem_ratio,
                "threshold": mem_warn,
                "message": f"Memory usage {mem_ratio}% >= {mem_warn}%",
            })

    load_first = None
    load_raw = str(status.get("load_avg", "")).split()
    if load_raw:
        load_first = _to_float(load_raw[0])
        if load_first is not None and load_first >= load_warn:
            alerts.append({
                "level": "warn",
                "kind": "cpu",
                "metric": "load_avg_1m",
                "value": load_first,
                "threshold": load_warn,
                "message": f"Load average(1m) {load_first} >= {load_warn}",
            })

    return {
        "healthy": len(alerts) == 0,
        "disk_pct": disk_pct,
        "memory_used_pct": mem_ratio,
        "load_1m": load_first,
        "alerts": alerts,
    }


def _pick_stdout(session: ServerSession, commands: list[str], timeout: int = 60) -> str:
    for cmd in commands:
        r = session.exec(cmd, timeout=timeout)
        if r.get("stdout", "").strip():
            return r.get("stdout", "").strip()
    return ""


def _parse_pm2(stdout: str) -> list[dict]:
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [
        {
            "name": p.get("name", ""),
            "status": p.get("pm2_env", {}).get("status", ""),
            "cpu": p.get("monit", {}).get("cpu", 0),
            "memory_mb": round(p.get("monit", {}).get("memory", 0) / 1024 / 1024, 1),
        }
        for p in data
    ]


def _parse_docker(stdout: str) -> list[dict]:
    if not stdout:
        return []
    containers = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            containers.append({
                "name": raw.get("Names", ""),
                "image": raw.get("Image", ""),
                "status": raw.get("Status", ""),
                "ports": raw.get("Ports", ""),
            })
        except json.JSONDecodeError:
            pass
    return containers


def _audit_apps(session: ServerSession, server_name: str, include_services: bool, refresh_services: bool) -> dict:
    item = {"server": server_name}
    item["status"] = session.system_status()

    py = session.exec(
        "ps -eo pid,user,comm,args --sort=pid | awk 'NR==1 || $3 ~ /^python3?$/ {print}'",
        timeout=60,
    )
    py_lines = [ln for ln in py.get("stdout", "").splitlines() if ln.strip()]
    item["python_processes"] = {
        "count": max(len(py_lines) - 1, 0) if py_lines else 0,
        "lines": py_lines,
    }

    pm2_out = _pick_stdout(session, [
        "pm2 jlist 2>/dev/null || true",
        "sudo -n pm2 jlist 2>/dev/null || true",
    ])
    item["pm2_processes"] = _parse_pm2(pm2_out)

    docker_out = _pick_stdout(session, [
        "docker ps --format '{{json .}}' 2>/dev/null || true",
        "sudo -n docker ps --format '{{json .}}' 2>/dev/null || true",
    ])
    item["docker_containers"] = _parse_docker(docker_out)

    x_active = session.exec("systemctl is-active x-ui 2>/dev/null || true", timeout=30).get("stdout", "").strip()
    x_enabled = session.exec("systemctl is-enabled x-ui 2>/dev/null || true", timeout=30).get("stdout", "").strip()
    x_proc = session.exec("ps -eo pid,args | grep -E '[x]-ui|[x]ray' || true", timeout=30).get("stdout", "").strip()
    x_docker = _pick_stdout(session, [
        "docker ps --format '{{.Names}}|{{.Image}}' 2>/dev/null | grep -Ei 'x-ui|xray' || true",
        "sudo -n docker ps --format '{{.Names}}|{{.Image}}' 2>/dev/null | grep -Ei 'x-ui|xray' || true",
    ], timeout=30)
    x_files = session.exec(
        "ls /usr/local/x-ui/x-ui /etc/systemd/system/x-ui.service "
        "/etc/systemd/system/multi-user.target.wants/x-ui.service 2>/dev/null || true",
        timeout=30,
    ).get("stdout", "").strip()

    x_ui = {
        "service_active": x_active,
        "service_enabled": x_enabled,
        "processes": x_proc.splitlines() if x_proc else [],
        "docker_matches": x_docker.splitlines() if x_docker else [],
        "file_matches": x_files.splitlines() if x_files else [],
    }
    x_ui["detected"] = bool(
        x_ui["service_active"] in ("active", "activating")
        or x_ui["service_enabled"] in ("enabled", "static")
        or x_ui["processes"]
        or x_ui["docker_matches"]
        or x_ui["file_matches"]
    )
    x_ui["running"] = bool(
        x_ui["service_active"] in ("active", "activating")
        or x_ui["processes"]
        or x_ui["docker_matches"]
    )
    item["x_ui"] = x_ui

    if include_services:
        if refresh_services:
            item["services"] = inventory.refresh(server_name, session)
        else:
            item["services"] = inventory.get_services(server_name)

    return item


def _validate_packages(packages: list[str]) -> tuple[list[str], list[str]]:
    valid, invalid = [], []
    for name in packages:
        if re.fullmatch(r"[a-zA-Z0-9.+:-]+", name or ""):
            valid.append(name)
        else:
            invalid.append(name)
    return valid, invalid


def _run_baseline(session: ServerSession, packages: list[str], clean_first: bool, timeout: int) -> dict:
    pkg = " ".join(packages)
    prefix = "if [ \"$(id -u)\" -eq 0 ]; then SUDO=''; else SUDO='sudo -n '; fi; "
    attempts = []

    if clean_first:
        clean_cmd = (
            prefix
            + "df -h /; "
            + "$SUDO apt-get clean; "
            + "$SUDO rm -rf /var/lib/apt/lists/*; "
            + "$SUDO journalctl --vacuum-size=100M >/dev/null 2>&1 || true; "
            + "$SUDO find /var/log -type f \\( -name '*.gz' -o -name '*.[0-9]' \\) -delete || true; "
            + "df -h /"
        )
        clean = session.exec(clean_cmd, timeout=300)
        attempts.append({
            "step": "clean-first",
            "exit_code": clean.get("exit_code", 1),
            "stdout_tail": _tail(clean.get("stdout", "")),
            "stderr_tail": _tail(clean.get("stderr", "")),
        })

    install_cmd = (
        prefix
        + "export DEBIAN_FRONTEND=noninteractive; "
        + "$SUDO apt-get update -qq && "
        + f"$SUDO apt-get install -y -qq {pkg}"
    )
    install = session.exec(install_cmd, timeout=timeout)
    attempts.append({
        "step": "install",
        "exit_code": install.get("exit_code", 1),
        "stdout_tail": _tail(install.get("stdout", "")),
        "stderr_tail": _tail(install.get("stderr", "")),
    })

    if install.get("exit_code", 1) == 0:
        return {"ok": True, "attempts": attempts}

    fix_cmd = (
        prefix
        + "export DEBIAN_FRONTEND=noninteractive; "
        + "$SUDO apt-get -y -f install && "
        + f"$SUDO apt-get install -y -qq {pkg}"
    )
    fix = session.exec(fix_cmd, timeout=timeout)
    attempts.append({
        "step": "fix-broken-and-retry",
        "exit_code": fix.get("exit_code", 1),
        "stdout_tail": _tail(fix.get("stdout", "")),
        "stderr_tail": _tail(fix.get("stderr", "")),
    })

    return {"ok": fix.get("exit_code", 1) == 0, "attempts": attempts}


def _normalize_port_rule(raw: str) -> str:
    value = (raw or "").strip()
    if re.fullmatch(r"\d+", value):
        value = f"{value}/tcp"
    if not re.fullmatch(r"\d+/(tcp|udp)", value):
        raise ValueError(f"Invalid port rule: {raw}")
    return value


def _build_firewall_command(allow_rules: list[str], deny_incoming: bool, allow_outgoing: bool, reset: bool, enable: bool) -> str:
    lines = [
        "set -e",
        "export DEBIAN_FRONTEND=noninteractive",
        "if [ \"$(id -u)\" -eq 0 ]; then SUDO=''; else SUDO='sudo -n '; fi",
        "if ! command -v ufw >/dev/null 2>&1; then",
        "  if command -v apt-get >/dev/null 2>&1; then",
        "    $SUDO apt-get update -qq",
        "    $SUDO apt-get install -y -qq ufw",
        "  else",
        "    echo 'UFW_NOT_AVAILABLE_NO_APT'",
        "    exit 1",
        "  fi",
        "fi",
    ]
    if reset:
        lines.append("$SUDO ufw --force reset")
    if deny_incoming:
        lines.append("$SUDO ufw default deny incoming")
    if allow_outgoing:
        lines.append("$SUDO ufw default allow outgoing")
    for rule in allow_rules:
        lines.append(f"$SUDO ufw allow {rule}")
    if enable:
        lines.append("$SUDO ufw --force enable")
    lines.extend([
        "$SUDO ufw status numbered",
        "$SUDO ufw status verbose",
    ])
    return "\n".join(lines)


def _port_audit_single(session: ServerSession, ports: list[int]) -> dict:
    ss = session.exec("ss -lntp 2>/dev/null || true", timeout=90).get("stdout", "")
    lsof = session.exec("lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null || true", timeout=90).get("stdout", "")
    docker = session.exec("docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null || true", timeout=90).get("stdout", "")
    results = {}
    ss_lines = [ln for ln in ss.splitlines() if ln.strip()]
    lsof_lines = [ln for ln in lsof.splitlines() if ln.strip()]
    docker_lines = [ln for ln in docker.splitlines() if ln.strip()]
    for port in ports:
        p = str(port)
        ss_match = [ln for ln in ss_lines if re.search(rf"[:\]]{re.escape(p)}(\s|$)", ln)]
        lsof_match = [ln for ln in lsof_lines if re.search(rf":{re.escape(p)}\s+\(LISTEN\)", ln)]
        docker_match = [ln for ln in docker_lines if re.search(rf"(^|,| |\[::\]:|0.0.0.0:){re.escape(p)}(->|/tcp)", ln)]
        occupied = bool(ss_match or lsof_match or docker_match)
        results[p] = {
            "occupied": occupied,
            "ss": ss_match,
            "lsof": lsof_match,
            "docker": docker_match,
        }
    return results


def _safe_name_from_remote(remote: str) -> str:
    return remote.strip("/").replace("/", "__")


def _snapshot_scan(session: ServerSession) -> dict:
    scan_cmd = (
        "for d in /etc /usr/local /opt /var/lib /root /home; do "
        f"find $d -maxdepth 6 -type f \\( {APP_SNAPSHOT_GLOBS} \\) 2>/dev/null; "
        "done | grep -Ei 'x-ui|3x-ui|xray|v2ray|reality|trojan|vmess|vless|/xray/|/x-ui/' | sort -u"
    )
    svc_cmd = (
        "(systemctl list-unit-files --type=service 2>/dev/null | grep -Ei '3x-ui|x-ui|xray|v2ray' || true); "
        "(systemctl list-units --type=service --all 2>/dev/null | grep -Ei '3x-ui|x-ui|xray|v2ray' || true)"
    )
    port_cmd = "ss -lntp 2>/dev/null | awk 'NR==1 || $4 ~ /:443$|:80$|:2053$|:8443$|:7271$/'"
    files = session.exec(scan_cmd, timeout=240).get("stdout", "")
    services = session.exec(svc_cmd, timeout=120).get("stdout", "")
    ports = session.exec(port_cmd, timeout=120).get("stdout", "")
    return {
        "files": [ln for ln in files.splitlines() if ln.strip()],
        "services": [ln for ln in services.splitlines() if ln.strip()],
        "listen": [ln for ln in ports.splitlines() if ln.strip()],
    }


def _inventory_server(inv: dict, server_name: str) -> dict:
    return inv.setdefault("servers", {}).setdefault(server_name, {})


def _inventory_meta(inv: dict, server_name: str) -> dict:
    return _inventory_server(inv, server_name).setdefault("metadata", {})


def _parse_domain_map_assignments(pairs: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"Invalid --domain-by-server value: {pair}")
        name, domain = pair.split("=", 1)
        name = name.strip()
        domain = domain.strip()
        if not name or not domain:
            raise ValueError(f"Invalid --domain-by-server value: {pair}")
        result[name] = domain
    return result


def _load_mapping_file(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Mapping file not found: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".json"}:
        data = json.loads(text)
    else:
        import yaml
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Mapping file must be a JSON/YAML object")
    return data


def _resolve_server_domain(server_name: str, inv: dict, default_domain: str | None, domain_by_server: dict[str, str], domain_map_file: dict | None) -> str:
    if server_name in domain_by_server:
        return domain_by_server[server_name]
    if domain_map_file:
        if server_name in domain_map_file and isinstance(domain_map_file[server_name], str):
            return domain_map_file[server_name]
        servers = domain_map_file.get("servers")
        if isinstance(servers, dict):
            v = servers.get(server_name)
            if isinstance(v, str):
                return v
            if isinstance(v, dict) and isinstance(v.get("domain"), str):
                return v["domain"]
    if default_domain:
        return default_domain
    meta = _inventory_meta(inv, server_name)
    domains = meta.get("domains", [])
    if isinstance(domains, list) and domains:
        return str(domains[0])
    raise RuntimeError(f"No domain configured for server '{server_name}'")


def _is_china_host(server_name: str, inv: dict) -> bool:
    name = server_name.lower()
    if "-cn" in name or name.endswith("cn") or "china" in name:
        return True
    meta = _inventory_meta(inv, server_name)
    for key in ("country", "region", "location"):
        value = str(meta.get(key, "")).strip().lower()
        if value in {"cn", "china", "中国", "mainland"}:
            return True
    return False


def _coerce_value(raw: str):
    text = (raw or "").strip()
    if text == "":
        return ""
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            pass
    if re.fullmatch(r"-?\d+\.\d+", text):
        try:
            return float(text)
        except ValueError:
            pass
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def _set_meta_field(meta: dict, dotted_key: str, value):
    parts = [p.strip() for p in dotted_key.split(".") if p.strip()]
    if not parts:
        raise ValueError("Metadata field key cannot be empty")
    node = meta
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    node[parts[-1]] = value


def _append_lines_file(path: str, lines: list[str]):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines).strip()
    if not content:
        return
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    p.write_text(existing + content + "\n", encoding="utf-8")
