"""REALITY/Xray helper functions."""

import datetime as dt
import json
import re
import secrets
import shlex
import sqlite3
import uuid
from pathlib import Path

from . import inventory
from .cli_helpers import _inventory_meta
from .ssh import ServerSession


def _parse_x25519_output(text: str) -> tuple[str, str]:
    private_key = ""
    public_key = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("PrivateKey:"):
            private_key = line.split(":", 1)[1].strip()
        if line.startswith("PublicKey:"):
            public_key = line.split(":", 1)[1].strip()
        if line.startswith("Password (PublicKey):"):
            public_key = line.split(":", 1)[1].strip()
    if not private_key or not public_key:
        raise RuntimeError("Failed to parse x25519 keypair from xray output")
    return private_key, public_key


def _build_reality_inbound_config(port: int, uuid_value: str, private_key: str, domain: str, short_id: str, flow: str) -> dict:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "0.0.0.0",
                "port": port,
                "protocol": "vless",
                "settings": {
                    "clients": [{"id": uuid_value, "flow": flow}],
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": f"{domain}:443",
                        "xver": 0,
                        "serverNames": [domain],
                        "privateKey": private_key,
                        "shortIds": [short_id],
                    },
                },
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            }
        ],
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
        ],
    }


def _build_vless_reality_uri(server_ip: str, port: int, uuid_value: str, flow: str, sni: str, fingerprint: str, public_key: str, short_id: str, label: str) -> str:
    return (
        f"vless://{uuid_value}@{server_ip}:{port}"
        f"?encryption=none&flow={flow}&security=reality&sni={sni}"
        f"&fp={fingerprint}&pbk={public_key}&sid={short_id}"
        f"&type=tcp&headerType=none#{label}"
    )


def _deploy_xray_reality(
    session: ServerSession,
    server_name: str,
    server_ip: str,
    domain: str,
    port: int,
    flow: str,
    fingerprint: str,
    uuid_value: str | None,
    short_id: str | None,
    open_firewall: bool,
) -> dict:
    install_cmd = (
        "set -e; "
        "if [ \"$(id -u)\" -eq 0 ]; then SUDO=''; else SUDO='sudo -n '; fi; "
        "if ! command -v xray >/dev/null 2>&1; then "
        "  if command -v apt-get >/dev/null 2>&1; then "
        "    $SUDO apt-get update -qq; $SUDO apt-get install -y -qq curl ca-certificates; "
        "  fi; "
        "  curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh -o /tmp/install-xray.sh; "
        "  $SUDO bash /tmp/install-xray.sh install; "
        "fi; "
        "xray x25519"
    )
    x25519 = session.exec(install_cmd, timeout=1200)
    private_key, public_key = _parse_x25519_output(x25519.get("stdout", ""))

    final_uuid = uuid_value or str(uuid.uuid4())
    final_short_id = short_id or secrets.token_hex(8)
    config = _build_reality_inbound_config(
        port=port,
        uuid_value=final_uuid,
        private_key=private_key,
        domain=domain,
        short_id=final_short_id,
        flow=flow,
    )
    config_json = json.dumps(config, ensure_ascii=False, indent=2)
    deploy_cmd = (
        "set -e; "
        "if [ \"$(id -u)\" -eq 0 ]; then SUDO=''; else SUDO='sudo -n '; fi; "
        "$SUDO mkdir -p /usr/local/etc/xray; "
        "if [ -f /usr/local/etc/xray/config.json ]; then "
        "  $SUDO cp -a /usr/local/etc/xray/config.json /usr/local/etc/xray/config.json.bak.$(date +%F-%H%M%S); "
        "fi; "
        "cat > /tmp/xray-config.json <<'EOF'\n"
        f"{config_json}\n"
        "EOF\n"
        "$SUDO cp /tmp/xray-config.json /usr/local/etc/xray/config.json; "
        "$SUDO xray -test -config /usr/local/etc/xray/config.json; "
        "$SUDO systemctl daemon-reload; "
        "$SUDO systemctl enable xray >/dev/null 2>&1 || true; "
        "$SUDO systemctl restart xray; "
        "sleep 1; "
        "$SUDO systemctl is-active xray; "
        "ss -lntp 2>/dev/null | grep -E '(:443$|:443 )' || true"
    )
    if open_firewall:
        deploy_cmd += "; if command -v ufw >/dev/null 2>&1; then $SUDO ufw allow 443/tcp >/dev/null 2>&1 || true; fi"
    verified = session.exec(deploy_cmd, timeout=600)
    node_uri = _build_vless_reality_uri(
        server_ip=server_ip,
        port=port,
        uuid_value=final_uuid,
        flow=flow,
        sni=domain,
        fingerprint=fingerprint,
        public_key=public_key,
        short_id=final_short_id,
        label=f"{server_name}-REALITY-{domain}",
    )
    return {
        "server": server_name,
        "ip": server_ip,
        "domain": domain,
        "port": port,
        "uuid": final_uuid,
        "public_key": public_key,
        "private_key": private_key,
        "short_id": final_short_id,
        "flow": flow,
        "fingerprint": fingerprint,
        "service_status": verified.get("stdout", "").splitlines()[-1] if verified.get("stdout", "") else "",
        "node_uri": node_uri,
    }


def _fetch_remote_file(session: ServerSession, remote_path: str) -> bytes:
    sftp = session._client.open_sftp()
    try:
        with sftp.open(remote_path, "rb") as fp:
            return fp.read()
    finally:
        sftp.close()


def _derive_public_from_private(session: ServerSession, private_key: str) -> str:
    cmd = f"xray x25519 -i {shlex.quote(private_key)}"
    out = session.exec(cmd, timeout=120).get("stdout", "")
    _, public_key = _parse_x25519_output(out)
    return public_key


def _recover_vless_from_xray_config(session: ServerSession, server_name: str, server_ip: str, config_data: dict, domain_hint: str) -> list[dict]:
    nodes = []
    for idx, inbound in enumerate(config_data.get("inbounds", []), start=1):
        if inbound.get("protocol") != "vless":
            continue
        port = inbound.get("port", 443)
        settings = inbound.get("settings", {})
        stream = inbound.get("streamSettings", {})
        security = stream.get("security", "")
        clients = settings.get("clients", [])
        if not isinstance(clients, list):
            continue
        if security == "reality":
            reality = stream.get("realitySettings", {})
            server_names = reality.get("serverNames", [])
            sni = server_names[0] if isinstance(server_names, list) and server_names else domain_hint
            short_ids = reality.get("shortIds", [])
            short_id = short_ids[0] if isinstance(short_ids, list) and short_ids else ""
            pub = ""
            try:
                pub = _derive_public_from_private(session, reality.get("privateKey", ""))
            except Exception:
                pass
            for c in clients:
                cid = c.get("id", "")
                if not cid:
                    continue
                flow = c.get("flow", "xtls-rprx-vision")
                uri = _build_vless_reality_uri(
                    server_ip=server_ip,
                    port=port,
                    uuid_value=cid,
                    flow=flow,
                    sni=sni,
                    fingerprint="chrome",
                    public_key=pub,
                    short_id=short_id,
                    label=f"{server_name}-recover-xray-{idx}",
                )
                nodes.append({
                    "source": "xray-config",
                    "inbound_index": idx,
                    "protocol": "vless",
                    "security": "reality",
                    "uri": uri,
                    "has_public_key": bool(pub),
                })
    return nodes


def _recover_vless_from_xui_db(server_name: str, server_ip: str, db_path: Path, domain_hint: str) -> list[dict]:
    nodes = []
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='inbounds'")
        if not cur.fetchone():
            return []
        cur.execute("SELECT id,remark,enable,port,protocol,settings,stream_settings FROM inbounds ORDER BY id")
        rows = cur.fetchall()
        for row in rows:
            inbound_id, remark, enable, port, protocol, settings_raw, stream_raw = row
            if str(protocol).lower() != "vless" or int(enable) != 1:
                continue
            try:
                settings = json.loads(settings_raw) if settings_raw else {}
            except Exception:
                continue
            try:
                stream = json.loads(stream_raw) if stream_raw else {}
            except Exception:
                stream = {}
            clients = settings.get("clients", [])
            reality = stream.get("realitySettings", {}) if stream.get("security") == "reality" else {}
            server_names = reality.get("serverNames", [])
            sni = server_names[0] if isinstance(server_names, list) and server_names else domain_hint
            short_ids = reality.get("shortIds", [])
            short_id = short_ids[0] if isinstance(short_ids, list) and short_ids else ""
            pub = ""
            if isinstance(reality.get("settings"), dict):
                pub = reality["settings"].get("publicKey", "") or ""
            for c in clients:
                cid = c.get("id", "")
                if not cid:
                    continue
                flow = c.get("flow", "xtls-rprx-vision")
                uri = _build_vless_reality_uri(
                    server_ip=server_ip,
                    port=int(port),
                    uuid_value=cid,
                    flow=flow,
                    sni=sni,
                    fingerprint="chrome",
                    public_key=pub,
                    short_id=short_id,
                    label=f"{server_name}-{remark or f'inbound-{inbound_id}'}",
                )
                nodes.append({
                    "source": "x-ui-db",
                    "inbound_id": inbound_id,
                    "remark": remark,
                    "protocol": "vless",
                    "security": stream.get("security", ""),
                    "uri": uri,
                    "has_public_key": bool(pub),
                })
    finally:
        con.close()
    return nodes


def _update_xray_meta(server_name: str, domain: str, port: int, flow: str, fingerprint: str, node_uri: str):
    inv = inventory.load()
    meta = _inventory_meta(inv, server_name)
    domains = meta.get("domains", [])
    if not isinstance(domains, list):
        domains = []
    if domain and domain not in domains:
        domains.append(domain)
    meta["domains"] = domains
    meta["xray_reality"] = {
        "domain": domain,
        "port": port,
        "flow": flow,
        "fingerprint": fingerprint,
        "node_uri": node_uri,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    inventory.save(inv)
