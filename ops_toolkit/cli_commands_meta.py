"""CLI command handlers for host metadata and subscription recovery."""

import base64
import datetime as dt
import json
import os
import tempfile
from pathlib import Path

from . import inventory
from .ssh import ServerSession
from .cli_helpers import (
    RETRIEVED_DIR,
    XRAY_CONFIG_CANDIDATES,
    XUI_DB_CANDIDATES,
    _coerce_value,
    _get_vault,
    _inventory_meta,
    _inventory_server,
    _is_china_host,
    _json_out,
    _load_mapping_file,
    _parse_domain_map_assignments,
    _resolve_server_domain,
    _resolve_servers,
    _safe_name_from_remote,
    _set_meta_field,
)
from .cli_reality import (
    _fetch_remote_file,
    _recover_vless_from_xray_config,
    _recover_vless_from_xui_db,
)


def cmd_toolkit_host_meta(args):
    inv = inventory.load()
    if args.host_meta_cmd == "set":
        meta = _inventory_meta(inv, args.server)
        if args.provider is not None:
            meta["provider"] = args.provider
        if args.plan is not None:
            meta["plan"] = args.plan
        if args.server_label is not None:
            meta["server_label"] = args.server_label
        if args.public_ip is not None:
            meta["public_ip"] = args.public_ip
        if args.renewal_date is not None:
            meta["renewal_date"] = args.renewal_date
        if args.domain is not None:
            if args.append_domains:
                existing = meta.get("domains", [])
                if not isinstance(existing, list):
                    existing = []
                for d in args.domain:
                    if d not in existing:
                        existing.append(d)
                meta["domains"] = existing
            else:
                meta["domains"] = args.domain
        if args.note is not None:
            if args.append_notes:
                existing_notes = meta.get("notes", [])
                if not isinstance(existing_notes, list):
                    existing_notes = []
                for n in args.note:
                    if n not in existing_notes:
                        existing_notes.append(n)
                meta["notes"] = existing_notes
            else:
                meta["notes"] = args.note
        for pair in args.field or []:
            if "=" not in pair:
                raise ValueError(f"Invalid --field value: {pair}")
            key, raw = pair.split("=", 1)
            _set_meta_field(meta, key.strip(), _coerce_value(raw))
        inventory.save(inv)
        _json_out({"ok": True, "server": args.server, "metadata": meta})
        return

    if args.host_meta_cmd == "get":
        if args.server == "all":
            data = {name: _inventory_meta(inv, name) for name in inv.get("servers", {}).keys()}
            _json_out({"servers": data})
        else:
            _json_out({"server": args.server, "metadata": _inventory_meta(inv, args.server)})
        return

    if args.host_meta_cmd == "list":
        rows = []
        for name, data in inv.get("servers", {}).items():
            meta = data.get("metadata", {})
            rows.append({
                "server": name,
                "provider": meta.get("provider", ""),
                "public_ip": meta.get("public_ip", ""),
                "domains": meta.get("domains", []),
                "renewal_date": meta.get("renewal_date", ""),
            })
        _json_out({"count": len(rows), "servers": rows})
        return

    if args.host_meta_cmd == "import":
        data = _load_mapping_file(args.file)
        source_servers = data["servers"] if "servers" in data and isinstance(data["servers"], dict) else data
        if not isinstance(source_servers, dict):
            raise ValueError("Import file must be a mapping of server -> metadata")
        changed = []
        for name, payload in source_servers.items():
            if not isinstance(payload, dict):
                continue
            incoming_meta = payload.get("metadata", payload)
            if not isinstance(incoming_meta, dict):
                continue
            if args.replace:
                _inventory_server(inv, name)["metadata"] = incoming_meta
            else:
                target = _inventory_meta(inv, name)
                target.update(incoming_meta)
            changed.append(name)
        inventory.save(inv)
        _json_out({"ok": True, "updated_servers": changed, "count": len(changed)})
        return

    raise ValueError("Unknown host-meta subcommand")


def cmd_toolkit_sub_recover(args):
    vault = _get_vault()
    inv = inventory.load()
    now = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = Path(args.output_root)
    domain_by_server = _parse_domain_map_assignments(args.domain_by_server)
    domain_map_file = _load_mapping_file(args.domain_map_file) if args.domain_map_file else None
    excluded = set(args.exclude_server or [])

    targets = []
    for name in _resolve_servers(vault, args.server):
        if name in excluded:
            continue
        if args.exclude_china and _is_china_host(name, inv):
            continue
        targets.append(name)

    all_links = []
    all_seen = set()
    results = []
    for name in targets:
        item = {"server": name}
        try:
            creds = vault.get(name)
            server_ip = creds.get("ip", "")
            try:
                domain_hint = _resolve_server_domain(
                    server_name=name,
                    inv=inv,
                    default_domain=args.default_domain,
                    domain_by_server=domain_by_server,
                    domain_map_file=domain_map_file,
                )
            except Exception:
                domain_hint = args.default_domain or server_ip

            local_dir = output_root / name / now
            if args.save_files:
                local_dir.mkdir(parents=True, exist_ok=True)
            with ServerSession(name, vault) as s:
                nodes = []
                recovered_files = []
                failed_files = []

                if not args.no_xray_config:
                    for remote in XRAY_CONFIG_CANDIDATES:
                        try:
                            raw = _fetch_remote_file(s, remote)
                            decoded = raw.decode("utf-8", errors="replace")
                            cfg = json.loads(decoded)
                            nodes.extend(_recover_vless_from_xray_config(s, name, server_ip, cfg, domain_hint))
                            recovered_files.append(remote)
                            if args.save_files:
                                (local_dir / _safe_name_from_remote(remote)).write_bytes(raw)
                        except FileNotFoundError:
                            continue
                        except Exception as e:
                            failed_files.append({"remote": remote, "error": str(e)})

                if not args.no_xui_db:
                    for remote in XUI_DB_CANDIDATES:
                        try:
                            raw = _fetch_remote_file(s, remote)
                            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
                            try:
                                tmp.write(raw)
                                tmp.close()
                                nodes.extend(
                                    _recover_vless_from_xui_db(
                                        server_name=name,
                                        server_ip=server_ip,
                                        db_path=Path(tmp.name),
                                        domain_hint=domain_hint,
                                    )
                                )
                            finally:
                                try:
                                    os.remove(tmp.name)
                                except Exception:
                                    pass
                            recovered_files.append(remote)
                            if args.save_files:
                                (local_dir / _safe_name_from_remote(remote)).write_bytes(raw)
                        except FileNotFoundError:
                            continue
                        except Exception as e:
                            failed_files.append({"remote": remote, "error": str(e)})

                unique_nodes = []
                seen = set()
                for node in nodes:
                    uri = node.get("uri", "").strip()
                    if not uri or uri in seen:
                        continue
                    seen.add(uri)
                    unique_nodes.append(node)
                    if uri not in all_seen:
                        all_seen.add(uri)
                        all_links.append(uri)

                item["ip"] = server_ip
                item["domain_hint"] = domain_hint
                item["node_count"] = len(unique_nodes)
                item["links"] = [n["uri"] for n in unique_nodes]
                if args.show_nodes:
                    item["nodes"] = unique_nodes
                item["recovered_files"] = recovered_files
                item["failed_files"] = failed_files
                if args.save_files:
                    report = {
                        "server": name,
                        "generated_at": now,
                        "domain_hint": domain_hint,
                        "node_count": len(unique_nodes),
                        "links": item["links"],
                        "recovered_files": recovered_files,
                        "failed_files": failed_files,
                    }
                    (local_dir / "subscription_recover_report.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    item["local_dir"] = str(local_dir)
        except Exception as e:
            item["error"] = str(e)
        results.append(item)

    plain = "\n".join(all_links)
    b64 = base64.b64encode(plain.encode("utf-8")).decode("ascii") if plain else ""
    if args.output_file:
        p = Path(args.output_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((plain + "\n") if plain else "", encoding="utf-8")
    if args.output_base64_file:
        p = Path(args.output_base64_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((b64 + "\n") if b64 else "", encoding="utf-8")

    _json_out({
        "workflow": "sub-recover",
        "generated_at": now,
        "target_servers": targets,
        "total_servers": len(targets),
        "total_links": len(all_links),
        "subscription_plain": plain,
        "subscription_base64": b64,
        "results": results,
    })
