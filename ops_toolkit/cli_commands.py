"""CLI command handlers (core + rollout)."""

import base64
import datetime as dt
import json
import os
import sys
import uuid
from pathlib import Path

from . import inventory
from .ssh import ServerSession
from .cli_helpers import (
    BASELINE_PACKAGES,
    RETRIEVED_DIR,
    XRAY_CONFIG_CANDIDATES,
    XUI_DB_CANDIDATES,
    _append_lines_file,
    _audit_apps,
    _build_firewall_command,
    _get_vault,
    _health_from_status,
    _is_china_host,
    _json_out,
    _load_mapping_file,
    _multi,
    _normalize_port_rule,
    _parse_domain_map_assignments,
    _port_audit_single,
    _resolve_server_domain,
    _resolve_servers,
    _run_baseline,
    _safe_name_from_remote,
    _snapshot_scan,
    _tail,
    _validate_packages,
)
from .cli_reality import _deploy_xray_reality, _fetch_remote_file, _update_xray_meta


def cmd_vault_init(args):
    vault = _get_vault()
    vault.save()
    _json_out({"ok": True, "message": "Vault initialized"})


def cmd_vault_add(args):
    vault = _get_vault()
    ip = input("IP: ").strip()
    user = input("User [root]: ").strip() or "root"
    password = input("Password: ").strip()
    port = int(input("Port [22]: ").strip() or "22")
    key_path = input("SSH key path [optional]: ").strip()
    vault.set(args.name, ip, user, password, port, key_path or None)
    _json_out({"ok": True, "server": args.name})


def cmd_vault_list(args):
    _json_out({"servers": _get_vault().list_servers()})


def cmd_vault_remove(args):
    vault = _get_vault()
    vault.remove(args.name)
    _json_out({"ok": True, "removed": args.name})


def cmd_status(args):
    _multi(_get_vault(), args.server, lambda n, s: s.system_status())


def cmd_exec(args):
    vault = _get_vault()
    with ServerSession(args.server, vault) as s:
        _json_out(s.exec(args.cmd, timeout=args.timeout))


def cmd_docker_ps(args):
    _multi(_get_vault(), args.server, lambda n, s: {"server": n, "containers": s.docker_ps()})


def cmd_pm2_list(args):
    _multi(_get_vault(), args.server, lambda n, s: {"server": n, "processes": s.pm2_list()})


def cmd_apt_update(args):
    _multi(_get_vault(), args.server, lambda n, s: s.apt_maintenance())


def cmd_services(args):
    vault = _get_vault()
    results = []
    for name in _resolve_servers(vault, args.server):
        results.append({"server": name, "services": inventory.get_services(name)})
    _json_out(results if len(results) > 1 else results[0])


def cmd_refresh(args):
    _multi(_get_vault(), args.server, lambda n, s: {"server": n, "services": inventory.refresh(n, s)})


def cmd_upload(args):
    vault = _get_vault()
    with ServerSession(args.server, vault) as s:
        _json_out(s.upload(args.local, args.remote))


def cmd_toolkit_add_server(args):
    password = args.password or os.environ.get(args.password_env, "")
    if not password and not args.key_path:
        _json_out({
            "ok": False,
            "error": f"Missing credential: provide --password, set {args.password_env}, or pass --key-path",
        })
        sys.exit(1)

    vault = _get_vault()
    vault.set(args.name, args.ip, args.user, password, args.port, args.key_path)
    result = {
        "ok": True,
        "workflow": "add-server",
        "server": args.name,
        "ip": args.ip,
        "user": args.user,
        "port": args.port,
    }
    if args.check:
        try:
            with ServerSession(args.name, vault) as s:
                result["connectivity"] = {"ok": True, "status": s.system_status()}
        except Exception as e:
            result["connectivity"] = {"ok": False, "error": str(e)}
    _json_out(result)


def cmd_toolkit_deploy(args):
    if bool(args.local) != bool(args.remote):
        _json_out({"ok": False, "error": "--local and --remote must be provided together"})
        sys.exit(1)

    vault = _get_vault()
    with ServerSession(args.server, vault) as s:
        steps = {}
        if args.local and args.remote:
            steps["upload"] = s.upload(args.local, args.remote)
        steps["exec"] = s.exec(args.cmd, timeout=args.timeout)
        if args.verify == "status":
            steps["verify"] = s.system_status()
        elif args.verify == "docker-ps":
            steps["verify"] = {"server": args.server, "containers": s.docker_ps()}
        elif args.verify == "pm2-list":
            steps["verify"] = {"server": args.server, "processes": s.pm2_list()}
        else:
            steps["verify"] = {"server": args.server, "skipped": True}

    _json_out({
        "ok": True,
        "workflow": "deploy",
        "server": args.server,
        "steps": steps,
    })


def cmd_toolkit_patrol(args):
    vault = _get_vault()
    results = []
    for name in _resolve_servers(vault, args.server):
        item = {"server": name}
        try:
            with ServerSession(name, vault) as s:
                item["status"] = s.system_status()
                item["health"] = _health_from_status(
                    item["status"],
                    disk_warn=args.disk_warn,
                    disk_crit=args.disk_crit,
                    mem_warn=args.mem_warn,
                    load_warn=args.load_warn,
                )
                if args.docker:
                    item["docker"] = s.docker_ps()
                if args.pm2:
                    item["pm2"] = s.pm2_list()
                if args.services:
                    item["services"] = inventory.get_services(name)
        except Exception as e:
            item["error"] = str(e)
        results.append(item)
    _json_out(results if len(results) > 1 else results[0])


def cmd_toolkit_baseline(args):
    packages = args.package if args.package else list(BASELINE_PACKAGES)
    valid, invalid = _validate_packages(packages)
    if invalid:
        _json_out({"ok": False, "error": "Invalid package names", "invalid": invalid})
        sys.exit(1)

    vault = _get_vault()
    results = []
    for name in _resolve_servers(vault, args.server):
        item = {"server": name, "packages": valid}
        try:
            with ServerSession(name, vault) as s:
                item["before"] = s.system_status()
                item["baseline"] = _run_baseline(s, valid, clean_first=args.clean_first, timeout=args.timeout)
                item["after"] = s.system_status()
                item["health"] = _health_from_status(
                    item["after"],
                    disk_warn=args.disk_warn,
                    disk_crit=args.disk_crit,
                    mem_warn=args.mem_warn,
                    load_warn=args.load_warn,
                )
        except Exception as e:
            item["error"] = str(e)
        results.append(item)
    _json_out(results if len(results) > 1 else results[0])


def cmd_toolkit_audit_apps(args):
    vault = _get_vault()
    results = []
    for name in _resolve_servers(vault, args.server):
        item = {"server": name}
        try:
            with ServerSession(name, vault) as s:
                item = _audit_apps(
                    s,
                    server_name=name,
                    include_services=args.services,
                    refresh_services=args.refresh_services,
                )
                item["health"] = _health_from_status(
                    item["status"],
                    disk_warn=args.disk_warn,
                    disk_crit=args.disk_crit,
                    mem_warn=args.mem_warn,
                    load_warn=args.load_warn,
                )
        except Exception as e:
            item["error"] = str(e)
        results.append(item)
    _json_out(results if len(results) > 1 else results[0])


def cmd_toolkit_firewall(args):
    vault = _get_vault()
    inv = inventory.load()
    excluded = set(args.exclude_server or [])
    allow_extra = []
    invalid_rules = []
    for raw in args.allow or []:
        try:
            allow_extra.append(_normalize_port_rule(raw))
        except ValueError:
            invalid_rules.append(raw)
    if invalid_rules:
        _json_out({"ok": False, "error": "Invalid allow rules", "invalid": invalid_rules})
        sys.exit(1)

    results = []
    for name in _resolve_servers(vault, args.server):
        if name in excluded:
            continue
        if args.exclude_china and _is_china_host(name, inv):
            continue
        item = {"server": name}
        try:
            creds = vault.get(name)
            ssh_rule = _normalize_port_rule(args.ssh_rule or str(creds.get("port", 22)))
            allow_rules = [ssh_rule]
            for rule in allow_extra:
                if rule not in allow_rules:
                    allow_rules.append(rule)
            command = _build_firewall_command(
                allow_rules=allow_rules,
                deny_incoming=not args.no_deny_incoming,
                allow_outgoing=not args.no_allow_outgoing,
                reset=args.reset,
                enable=not args.no_enable,
            )
            item["allow_rules"] = allow_rules
            if args.show_command:
                item["command"] = command
            if args.dry_run:
                item["ok"] = True
                item["dry_run"] = True
            else:
                with ServerSession(name, vault) as s:
                    run = s.exec(command, timeout=args.timeout)
                item["ok"] = run.get("exit_code", 1) == 0
                item["exit_code"] = run.get("exit_code", 1)
                item["stdout_tail"] = _tail(run.get("stdout", ""), 4000)
                item["stderr_tail"] = _tail(run.get("stderr", ""), 2000)
        except Exception as e:
            item["ok"] = False
            item["error"] = str(e)
        results.append(item)
    _json_out(results if len(results) > 1 else results[0])


def cmd_toolkit_port_audit(args):
    vault = _get_vault()
    inv = inventory.load()
    ports = sorted(set(args.port or [443]))
    excluded = set(args.exclude_server or [])
    target_servers = []
    for name in _resolve_servers(vault, args.server):
        if name in excluded:
            continue
        if args.exclude_china and _is_china_host(name, inv):
            continue
        target_servers.append(name)

    results = []
    for name in target_servers:
        item = {"server": name, "ports": ports}
        try:
            with ServerSession(name, vault) as s:
                detail = _port_audit_single(s, ports)
                item["ports_detail"] = detail
                item["occupied_ports"] = [p for p, v in detail.items() if v.get("occupied")]
        except Exception as e:
            item["error"] = str(e)
        results.append(item)

    summary = {
        "total_servers": len(target_servers),
        "ports": ports,
        "occupied": {
            str(p): [r["server"] for r in results if str(p) in r.get("occupied_ports", [])]
            for p in ports
        },
    }
    _json_out({"workflow": "port-audit", "summary": summary, "results": results})


def cmd_toolkit_app_snapshot(args):
    vault = _get_vault()
    now = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = Path(args.output_root)
    results = []
    for name in _resolve_servers(vault, args.server):
        item = {"server": name}
        try:
            with ServerSession(name, vault) as s:
                snapshot = _snapshot_scan(s)
                item["snapshot"] = snapshot
                if args.download:
                    local_dir = output_root / name / now
                    local_dir.mkdir(parents=True, exist_ok=True)
                    (local_dir / "scan_report.json").write_text(
                        json.dumps(snapshot, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    candidates = []
                    if args.path:
                        candidates.extend(args.path)
                    use_scan_files = args.include_scan_files or (not args.path and not args.include_defaults)
                    use_defaults = args.include_defaults or (not args.path and not args.include_scan_files)
                    if use_scan_files:
                        candidates.extend(snapshot.get("files", []))
                    if use_defaults:
                        candidates.extend(XRAY_CONFIG_CANDIDATES)
                        candidates.extend(XUI_DB_CANDIDATES)
                    dedup = []
                    seen = set()
                    for remote in candidates:
                        if not remote or remote in seen:
                            continue
                        seen.add(remote)
                        dedup.append(remote)
                    if args.max_files > 0:
                        dedup = dedup[:args.max_files]

                    downloaded = []
                    failed = []
                    for remote in dedup:
                        try:
                            raw = _fetch_remote_file(s, remote)
                            local_name = _safe_name_from_remote(remote)
                            local_path = local_dir / local_name
                            local_path.write_bytes(raw)
                            downloaded.append({
                                "remote": remote,
                                "local": str(local_path),
                                "bytes": len(raw),
                            })
                        except Exception as e:
                            failed.append({"remote": remote, "error": str(e)})
                    report = {
                        "server": name,
                        "generated_at": now,
                        "downloaded_count": len(downloaded),
                        "failed_count": len(failed),
                        "downloaded": downloaded,
                        "failed": failed,
                    }
                    (local_dir / "download_report.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    item["download"] = {
                        "local_dir": str(local_dir),
                        "downloaded_count": len(downloaded),
                        "failed_count": len(failed),
                        "downloaded": downloaded,
                        "failed": failed,
                    }
        except Exception as e:
            item["error"] = str(e)
        results.append(item)
    _json_out(results if len(results) > 1 else results[0])


def cmd_toolkit_xray_deploy(args):
    vault = _get_vault()
    inv = inventory.load()
    domain = args.domain
    if not domain:
        domain = _resolve_server_domain(
            server_name=args.server,
            inv=inv,
            default_domain=args.default_domain,
            domain_by_server={},
            domain_map_file=None,
        )

    creds = vault.get(args.server)
    server_ip = creds.get("ip", "")
    with ServerSession(args.server, vault) as s:
        result = _deploy_xray_reality(
            session=s,
            server_name=args.server,
            server_ip=server_ip,
            domain=domain,
            port=args.port,
            flow=args.flow,
            fingerprint=args.fingerprint,
            uuid_value=args.uuid,
            short_id=args.short_id,
            open_firewall=not args.no_open_firewall,
        )
    if not args.no_write_meta:
        _update_xray_meta(
            server_name=args.server,
            domain=domain,
            port=args.port,
            flow=args.flow,
            fingerprint=args.fingerprint,
            node_uri=result.get("node_uri", ""),
        )
    if args.output_file and result.get("node_uri"):
        _append_lines_file(args.output_file, [result["node_uri"]])
    _json_out({"workflow": "xray-deploy", "result": result})


def cmd_toolkit_xray_rollout(args):
    vault = _get_vault()
    inv = inventory.load()
    excluded = set(args.exclude_server or [])
    domain_by_server = _parse_domain_map_assignments(args.domain_by_server)
    domain_map_file = _load_mapping_file(args.domain_map_file) if args.domain_map_file else None
    shared_uuid = args.uuid or (str(uuid.uuid4()) if args.shared_uuid else None)

    servers = []
    for name in _resolve_servers(vault, args.server):
        if name in excluded:
            continue
        if args.exclude_china and _is_china_host(name, inv):
            continue
        servers.append(name)

    results = []
    for name in servers:
        item = {"server": name}
        try:
            creds = vault.get(name)
            server_ip = creds.get("ip", "")
            explicit_domain = domain_by_server.get(name)
            if not explicit_domain and domain_map_file:
                try:
                    explicit_domain = _resolve_server_domain(
                        server_name=name,
                        inv=inv,
                        default_domain=None,
                        domain_by_server={},
                        domain_map_file=domain_map_file,
                    )
                except Exception:
                    explicit_domain = None
            if args.require_explicit_domain and not explicit_domain:
                raise RuntimeError(
                    f"Missing explicit domain for '{name}'. Use --domain-by-server or --domain-map-file."
                )
            domain = explicit_domain or _resolve_server_domain(
                server_name=name,
                inv=inv,
                default_domain=args.default_domain,
                domain_by_server=domain_by_server,
                domain_map_file=domain_map_file,
            )
            item["ip"] = server_ip
            item["domain"] = domain
            item["port"] = args.port
            if args.dry_run:
                item["planned"] = True
            else:
                with ServerSession(name, vault) as s:
                    deployed = _deploy_xray_reality(
                        session=s,
                        server_name=name,
                        server_ip=server_ip,
                        domain=domain,
                        port=args.port,
                        flow=args.flow,
                        fingerprint=args.fingerprint,
                        uuid_value=shared_uuid if args.shared_uuid else args.uuid,
                        short_id=args.short_id,
                        open_firewall=not args.no_open_firewall,
                    )
                item.update(deployed)
                if not args.no_write_meta:
                    _update_xray_meta(
                        server_name=name,
                        domain=domain,
                        port=args.port,
                        flow=args.flow,
                        fingerprint=args.fingerprint,
                        node_uri=item.get("node_uri", ""),
                    )
        except Exception as e:
            item["error"] = str(e)
        results.append(item)

    links = []
    seen = set()
    for item in results:
        uri = item.get("node_uri", "")
        if uri and uri not in seen:
            seen.add(uri)
            links.append(uri)

    plain_sub = "\n".join(links)
    b64_sub = base64.b64encode(plain_sub.encode("utf-8")).decode("ascii") if plain_sub else ""
    if args.output_file and links:
        _append_lines_file(args.output_file, links)
    if args.output_base64_file and b64_sub:
        p = Path(args.output_base64_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(b64_sub + "\n", encoding="utf-8")

    _json_out({
        "workflow": "xray-rollout",
        "target_servers": servers,
        "total": len(servers),
        "success": sum(1 for r in results if r.get("node_uri")),
        "results": results,
        "subscription_plain": plain_sub,
        "subscription_base64": b64_sub,
    })
