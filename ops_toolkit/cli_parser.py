"""CLI parser and dispatch."""

import argparse
import sys

from .cli_helpers import RETRIEVED_DIR
from .cli_commands import (
    cmd_apt_update,
    cmd_docker_ps,
    cmd_exec,
    cmd_pm2_list,
    cmd_refresh,
    cmd_services,
    cmd_status,
    cmd_toolkit_add_server,
    cmd_toolkit_app_snapshot,
    cmd_toolkit_audit_apps,
    cmd_toolkit_baseline,
    cmd_toolkit_deploy,
    cmd_toolkit_firewall,
    cmd_toolkit_patrol,
    cmd_toolkit_port_audit,
    cmd_toolkit_xray_deploy,
    cmd_toolkit_xray_rollout,
    cmd_upload,
    cmd_vault_add,
    cmd_vault_init,
    cmd_vault_list,
    cmd_vault_remove,
)
from .cli_commands_meta import cmd_toolkit_host_meta, cmd_toolkit_sub_recover
from .cli_naive import cmd_toolkit_naive_deploy
from .cli_hysteria import cmd_toolkit_hysteria_deploy


def build_parser():
    p = argparse.ArgumentParser(prog="ops-toolkit")
    sub = p.add_subparsers(dest="subcmd")

    v = sub.add_parser("vault")
    vsub = v.add_subparsers(dest="vault_cmd")
    vsub.add_parser("init")
    va = vsub.add_parser("add")
    va.add_argument("name")
    vsub.add_parser("list")
    vr = vsub.add_parser("remove")
    vr.add_argument("name")

    st = sub.add_parser("status")
    st.add_argument("server")
    ex = sub.add_parser("exec")
    ex.add_argument("server")
    ex.add_argument("cmd")
    ex.add_argument("--timeout", type=int, default=30)
    dp = sub.add_parser("docker-ps")
    dp.add_argument("server")
    pm = sub.add_parser("pm2-list")
    pm.add_argument("server")
    au = sub.add_parser("apt-update")
    au.add_argument("server")
    sv = sub.add_parser("services")
    sv.add_argument("server")
    rf = sub.add_parser("refresh")
    rf.add_argument("server")
    up = sub.add_parser("upload")
    up.add_argument("server")
    up.add_argument("local")
    up.add_argument("remote")

    tk = sub.add_parser("toolkit")
    tksub = tk.add_subparsers(dest="toolkit_cmd")

    tka = tksub.add_parser("add-server")
    tka.add_argument("name")
    tka.add_argument("--ip", required=True)
    tka.add_argument("--user", default="root")
    tka.add_argument("--port", type=int, default=22)
    tka.add_argument("--password")
    tka.add_argument("--password-env", default="OPS_SERVER_PASSWORD")
    tka.add_argument("--key-path")
    tka.add_argument("--check", action="store_true")

    tkd = tksub.add_parser("deploy")
    tkd.add_argument("server")
    tkd.add_argument("--local")
    tkd.add_argument("--remote")
    tkd.add_argument("--cmd", required=True)
    tkd.add_argument("--timeout", type=int, default=300)
    tkd.add_argument("--verify", choices=["status", "docker-ps", "pm2-list", "none"], default="status")

    tkp = tksub.add_parser("patrol")
    tkp.add_argument("server")
    tkp.add_argument("--services", action="store_true")
    tkp.add_argument("--docker", action="store_true")
    tkp.add_argument("--pm2", action="store_true")
    tkp.add_argument("--disk-warn", type=int, default=85)
    tkp.add_argument("--disk-crit", type=int, default=95)
    tkp.add_argument("--mem-warn", type=int, default=90)
    tkp.add_argument("--load-warn", type=float, default=4.0)

    tkb = tksub.add_parser("baseline")
    tkb.add_argument("server")
    tkb.add_argument("--package", action="append")
    tkb.add_argument("--clean-first", action="store_true")
    tkb.add_argument("--timeout", type=int, default=1200)
    tkb.add_argument("--disk-warn", type=int, default=85)
    tkb.add_argument("--disk-crit", type=int, default=95)
    tkb.add_argument("--mem-warn", type=int, default=90)
    tkb.add_argument("--load-warn", type=float, default=4.0)

    tkaudit = tksub.add_parser("audit-apps")
    tkaudit.add_argument("server")
    tkaudit.add_argument("--services", action="store_true")
    tkaudit.add_argument("--refresh-services", action="store_true")
    tkaudit.add_argument("--disk-warn", type=int, default=85)
    tkaudit.add_argument("--disk-crit", type=int, default=95)
    tkaudit.add_argument("--mem-warn", type=int, default=90)
    tkaudit.add_argument("--load-warn", type=float, default=4.0)

    tkfw = tksub.add_parser("firewall")
    tkfw.add_argument("server")
    tkfw.add_argument("--allow", action="append")
    tkfw.add_argument("--ssh-rule", help="Override SSH allow rule, e.g. 22 or 22/tcp")
    tkfw.add_argument("--exclude-server", action="append")
    tkfw.add_argument("--exclude-china", action="store_true")
    tkfw.add_argument("--reset", action="store_true")
    tkfw.add_argument("--no-deny-incoming", action="store_true")
    tkfw.add_argument("--no-allow-outgoing", action="store_true")
    tkfw.add_argument("--no-enable", action="store_true")
    tkfw.add_argument("--dry-run", action="store_true")
    tkfw.add_argument("--show-command", action="store_true")
    tkfw.add_argument("--timeout", type=int, default=600)

    tkporta = tksub.add_parser("port-audit")
    tkporta.add_argument("server")
    tkporta.add_argument("--port", type=int, action="append")
    tkporta.add_argument("--exclude-server", action="append")
    tkporta.add_argument("--exclude-china", action="store_true")

    tksnap = tksub.add_parser("app-snapshot")
    tksnap.add_argument("server")
    tksnap.add_argument("--download", action="store_true")
    tksnap.add_argument("--path", action="append")
    tksnap.add_argument("--max-files", type=int, default=30)
    tksnap.add_argument("--include-scan-files", action="store_true")
    tksnap.add_argument("--include-defaults", action="store_true")
    tksnap.add_argument("--output-root", default=str(RETRIEVED_DIR))

    tkx = tksub.add_parser("xray-deploy")
    tkx.add_argument("server")
    tkx.add_argument("--domain")
    tkx.add_argument("--default-domain")
    tkx.add_argument("--port", type=int, default=443)
    tkx.add_argument("--flow", default="xtls-rprx-vision")
    tkx.add_argument("--fingerprint", default="chrome")
    tkx.add_argument("--uuid")
    tkx.add_argument("--short-id")
    tkx.add_argument("--no-open-firewall", action="store_true")
    tkx.add_argument("--no-write-meta", action="store_true")
    tkx.add_argument("--output-file")

    tkxr = tksub.add_parser("xray-rollout")
    tkxr.add_argument("server")
    tkxr.add_argument("--default-domain")
    tkxr.add_argument("--domain-by-server", action="append")
    tkxr.add_argument("--domain-map-file")
    tkxr.add_argument("--exclude-server", action="append")
    tkxr.add_argument("--exclude-china", action="store_true")
    tkxr.add_argument("--port", type=int, default=443)
    tkxr.add_argument("--flow", default="xtls-rprx-vision")
    tkxr.add_argument("--fingerprint", default="chrome")
    tkxr.add_argument("--uuid")
    tkxr.add_argument("--short-id")
    tkxr.add_argument("--shared-uuid", action="store_true")
    tkxr.add_argument("--dry-run", action="store_true")
    tkxr.add_argument("--no-open-firewall", action="store_true")
    tkxr.add_argument("--no-write-meta", action="store_true")
    tkxr.add_argument("--output-file")
    tkxr.add_argument("--output-base64-file")
    tkxr.add_argument("--require-explicit-domain", action="store_true", default=True)
    tkxr.add_argument("--allow-inventory-domain", dest="require_explicit_domain", action="store_false")

    tkmeta = tksub.add_parser("host-meta")
    tkmeta_sub = tkmeta.add_subparsers(dest="host_meta_cmd")
    tkmeta_set = tkmeta_sub.add_parser("set")
    tkmeta_set.add_argument("server")
    tkmeta_set.add_argument("--provider")
    tkmeta_set.add_argument("--plan")
    tkmeta_set.add_argument("--server-label")
    tkmeta_set.add_argument("--public-ip")
    tkmeta_set.add_argument("--renewal-date")
    tkmeta_set.add_argument("--domain", action="append")
    tkmeta_set.add_argument("--append-domains", action="store_true")
    tkmeta_set.add_argument("--note", action="append")
    tkmeta_set.add_argument("--append-notes", action="store_true")
    tkmeta_set.add_argument("--field", action="append", help="Custom metadata key=value, supports dotted keys")
    tkmeta_get = tkmeta_sub.add_parser("get")
    tkmeta_get.add_argument("server")
    tkmeta_sub.add_parser("list")
    tkmeta_import = tkmeta_sub.add_parser("import")
    tkmeta_import.add_argument("file")
    tkmeta_import.add_argument("--replace", action="store_true")

    tksubrec = tksub.add_parser("sub-recover")
    tksubrec.add_argument("server")
    tksubrec.add_argument("--default-domain")
    tksubrec.add_argument("--domain-by-server", action="append")
    tksubrec.add_argument("--domain-map-file")
    tksubrec.add_argument("--exclude-server", action="append")
    tksubrec.add_argument("--exclude-china", action="store_true")
    tksubrec.add_argument("--no-xray-config", action="store_true")
    tksubrec.add_argument("--no-xui-db", action="store_true")
    tksubrec.add_argument("--save-files", action="store_true")
    tksubrec.add_argument("--show-nodes", action="store_true")
    tksubrec.add_argument("--output-root", default=str(RETRIEVED_DIR))
    tksubrec.add_argument("--output-file")
    tksubrec.add_argument("--output-base64-file")

    tknaive = tksub.add_parser("naive-deploy")
    tknaive.add_argument("server")
    tknaive.add_argument("--domain")
    tknaive.add_argument("--default-domain")
    tknaive.add_argument("--domain-by-server", action="append")
    tknaive.add_argument("--domain-map-file")
    tknaive.add_argument("--exclude-server", action="append")
    tknaive.add_argument("--exclude-china", action="store_true")
    tknaive.add_argument("--allow-domain-mismatch", action="store_true")
    tknaive.add_argument("--port", type=int, default=8443)
    tknaive.add_argument("--username")
    tknaive.add_argument("--password")
    tknaive.add_argument("--acme-email")
    tknaive.add_argument("--binary-url")
    tknaive.add_argument("--acme-ca")
    tknaive.add_argument("--timeout", type=int, default=1800)
    tknaive.add_argument("--output-root", default="data/subscriptions/naive")
    tknaive.add_argument("--socks-port", type=int, default=1080)
    tknaive.add_argument("--http-port", type=int, default=8080)
    tknaive.add_argument("--old-port", type=int, default=24443)
    tknaive.add_argument("--keep-old-port", action="store_true")
    tknaive.add_argument("--no-open-firewall", action="store_true")
    tknaive.add_argument("--no-verify", action="store_true")
    tknaive.add_argument("--no-write-meta", action="store_true")
    tknaive.add_argument("--disk-warn", type=int, default=85)
    tknaive.add_argument("--disk-crit", type=int, default=95)
    tknaive.add_argument("--mem-warn", type=int, default=90)
    tknaive.add_argument("--load-warn", type=float, default=4.0)

    tkhy2 = tksub.add_parser("hysteria-deploy")
    tkhy2.add_argument("server")
    tkhy2.add_argument("--domain")
    tkhy2.add_argument("--default-domain")
    tkhy2.add_argument("--domain-by-server", action="append")
    tkhy2.add_argument("--domain-map-file")
    tkhy2.add_argument("--exclude-server", action="append")
    tkhy2.add_argument("--exclude-china", action="store_true")
    tkhy2.add_argument("--allow-domain-mismatch", action="store_true")
    tkhy2.add_argument("--listen-range", default="20000-50000")
    tkhy2.add_argument("--hop-interval", default="30s")
    tkhy2.add_argument("--auth")
    tkhy2.add_argument("--acme-email")
    tkhy2.add_argument("--masquerade-url", default="https://www.cloudflare.com/")
    tkhy2.add_argument("--cert-file")
    tkhy2.add_argument("--key-file")
    tkhy2.add_argument("--no-prefer-existing-cert", action="store_true")
    tkhy2.add_argument("--version")
    tkhy2.add_argument("--install-script-url", default="https://get.hy2.sh/")
    tkhy2.add_argument("--firewall-backend", choices=["auto", "iptables", "nftables"], default="auto")
    tkhy2.add_argument("--socks-port", type=int, default=1080)
    tkhy2.add_argument("--http-port", type=int, default=8080)
    tkhy2.add_argument("--timeout", type=int, default=1800)
    tkhy2.add_argument("--output-root", default="data/subscriptions/hysteria2")
    tkhy2.add_argument("--dry-run", action="store_true")
    tkhy2.add_argument("--no-open-firewall", action="store_true")
    tkhy2.add_argument("--no-run-as-root", action="store_true")
    tkhy2.add_argument("--no-write-meta", action="store_true")
    tkhy2.add_argument("--disk-warn", type=int, default=85)
    tkhy2.add_argument("--disk-crit", type=int, default=95)
    tkhy2.add_argument("--mem-warn", type=int, default=90)
    tkhy2.add_argument("--load-warn", type=float, default=4.0)

    return p, tk, tkmeta


def main():
    p, tk, tkmeta = build_parser()
    args = p.parse_args()
    if args.subcmd == "toolkit" and not args.toolkit_cmd:
        tk.print_help()
        sys.exit(1)
    if args.subcmd == "toolkit" and args.toolkit_cmd == "host-meta" and not args.host_meta_cmd:
        tkmeta.print_help()
        sys.exit(1)

    dispatch = {
        "vault": lambda: {"init": cmd_vault_init, "add": cmd_vault_add, "list": cmd_vault_list, "remove": cmd_vault_remove}[args.vault_cmd](args),
        "status": lambda: cmd_status(args),
        "exec": lambda: cmd_exec(args),
        "docker-ps": lambda: cmd_docker_ps(args),
        "pm2-list": lambda: cmd_pm2_list(args),
        "apt-update": lambda: cmd_apt_update(args),
        "services": lambda: cmd_services(args),
        "refresh": lambda: cmd_refresh(args),
        "upload": lambda: cmd_upload(args),
        "toolkit": lambda: {
            "add-server": cmd_toolkit_add_server,
            "deploy": cmd_toolkit_deploy,
            "patrol": cmd_toolkit_patrol,
            "baseline": cmd_toolkit_baseline,
            "audit-apps": cmd_toolkit_audit_apps,
            "firewall": cmd_toolkit_firewall,
            "port-audit": cmd_toolkit_port_audit,
            "app-snapshot": cmd_toolkit_app_snapshot,
            "xray-deploy": cmd_toolkit_xray_deploy,
            "xray-rollout": cmd_toolkit_xray_rollout,
            "host-meta": cmd_toolkit_host_meta,
            "sub-recover": cmd_toolkit_sub_recover,
            "naive-deploy": cmd_toolkit_naive_deploy,
            "hysteria-deploy": cmd_toolkit_hysteria_deploy,
        }[args.toolkit_cmd](args),
    }
    if args.subcmd in dispatch:
        dispatch[args.subcmd]()
    else:
        p.print_help()
