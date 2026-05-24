#!/usr/bin/env python3
"""sub2api proxy rapid-response kit.

Runs on racknerd-us. Talks to the sub2api-postgres container via `docker exec`
(no DSN / DB password needed) and uses curl on the host for live proxy probes.

Pools (by proxies table):
  OLD  = cheap external residential pool (numeric-IP hosts, not WDC)
  NEW  = connpnt backup pool (host like 'pv3.%')
  WDC  = WDC gost egress (proxies.id = 40)
  SG   = Singapore Xray HTTP egress (proxies.id = 41)
  direct = account with no proxy

Commands:
  status [--window N]                 health snapshot: pool sizes + 502 by pool
  test <old|new|wdc|sg|all> [--target T] live connectivity+latency probe per proxy
  import <file.txt> --pool NAME [--protocol http]
                                      import a proxy txt (auto-detect
                                      'ip:port:user:pass' or 'user:pass@host:port')
  switch --scope <free|plus|all|group:N> --to <old|new|wdc|sg> [--dry-run]
                                      move scope accounts (that already have a
                                      proxy) onto a pool, even round-robin
  panic [--off]                       set proxied GPT accounts unschedulable
                                      (leaves direct accounts serving); --off restores
"""
import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

PG = ["docker", "exec", "-i", "sub2api-postgres", "psql", "-U", "sub2api", "-d", "sub2api"]
GPT_GROUPS = "(2, 4)"  # GPT PLUS, GPT-free
WDC_ID = 40
SG_ID = 41
STATE_DIR = "/opt/sub2api-ops/state"

# proxy-table predicates per logical pool (alias p.)
POOL_FILTER = {
    "old": f"p.host not like 'pv3.%' and p.id not in ({WDC_ID},{SG_ID})",
    "new": "p.host like 'pv3.%'",
    "wdc": f"p.id = {WDC_ID}",
    "sg": f"p.id = {SG_ID}",
}
POOL_CASE = (
    "case when p.id is null then 'direct' "
    f"when p.id = {WDC_ID} then 'WDC' "
    f"when p.id = {SG_ID} then 'SG' "
    "when p.host like 'pv3.%' then 'NEW' else 'OLD' end"
)


def run_psql(sql, csv_out=True):
    args = PG + (["--csv"] if csv_out else ["-P", "pager=off"]) + ["-v", "ON_ERROR_STOP=1", "-c", sql]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(2)
    return r.stdout


def rows(sql):
    return list(csv.DictReader(io.StringIO(run_psql(sql, csv_out=True))))


def scalar(sql):
    args = PG + ["-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(2)
    return r.stdout.strip()


def sqlstr(v):
    return "'" + str(v).replace("'", "''") + "'"


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def scope_clause(scope):
    if scope in ("free", "plus", "all"):
        gid = {"free": "(4)", "plus": "(2)", "all": GPT_GROUPS}[scope]
        return f"a.id in (select account_id from account_groups where group_id in {gid}) and a.proxy_id is not null"
    if scope.startswith("group:"):
        n = int(scope.split(":", 1)[1])
        return f"a.id in (select account_id from account_groups where group_id = {n}) and a.proxy_id is not null"
    sys.exit(f"bad scope: {scope}")


# ---------------------------------------------------------------- status
def cmd_status(args):
    win = int(args.window)
    print(f"== proxy pools (accounts in GPT groups {GPT_GROUPS}) ==")
    sql = f"""
      select {POOL_CASE} pool, count(distinct p.id) proxies, count(a.id) accts,
             min(c.cnt) min_px, max(c.cnt) max_px
      from proxies p
      left join accounts a on a.proxy_id = p.id and a.deleted_at is null
            and a.id in (select account_id from account_groups where group_id in {GPT_GROUPS})
      left join (select proxy_id, count(*) cnt from accounts
                 where deleted_at is null
                   and id in (select account_id from account_groups where group_id in {GPT_GROUPS})
                 group by proxy_id) c on c.proxy_id = p.id
      where p.deleted_at is null and p.status = 'active'
      group by 1 order by 1;"""
    for r in rows(sql):
        print(f"  {r['pool']:<6} proxies={r['proxies']:<3} accounts={r['accts']:<4} per-proxy[{r['min_px'] or '-'}..{r['max_px'] or '-'}]")
    direct = rows(
        f"select count(*) n from accounts a where a.deleted_at is null and a.proxy_id is null "
        f"and a.id in (select account_id from account_groups where group_id in {GPT_GROUPS})"
    )
    print(f"  direct proxies=-   accounts={direct[0]['n']}")
    print(f"\n== errors last {win}m (by pool) ==")
    sql2 = f"""
      select {POOL_CASE} pool,
             count(*) filter (where e.status_code = 502) err502,
             count(*) filter (where e.status_code >= 400) err_all,
             count(distinct e.account_id) accts
      from ops_error_logs e
      left join accounts a on a.id = e.account_id
      left join proxies p on p.id = a.proxy_id
      where e.created_at > now() - interval '{win} minutes'
      group by 1 order by err502 desc;"""
    res = rows(sql2)
    if not res:
        print("  (no errors)")
    for r in res:
        print(f"  {r['pool']:<6} 502={r['err502']:<5} 4xx+={r['err_all']:<5} accounts={r['accts']}")
    succ = rows(f"select count(*) n from usage_logs where created_at > now() - interval '{win} minutes'")
    print(f"\n  successful requests last {win}m: {succ[0]['n']}")


# ---------------------------------------------------------------- test
def cmd_test(args):
    pool = args.pool
    if pool == "all":
        where = "p.deleted_at is null and p.status='active'"
    elif pool in POOL_FILTER:
        where = f"p.deleted_at is null and p.status='active' and {POOL_FILTER[pool]}"
    else:
        sys.exit(f"bad pool: {pool}")
    px = rows(f"select id,name,protocol,host,port,username,password from proxies p where {where} order by id")
    if not px:
        print("no proxies in pool")
        return
    target = args.target
    print(f"probing {len(px)} proxies ({pool}) -> {target}")
    for p in px:
        proxy = f"{p['protocol']}://{p['username']}:{p['password']}@{p['host']}:{p['port']}"
        try:
            code = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "--max-time", "15", "-w", "%{http_code}",
                 "-X", "POST", "--proxy", proxy, "-H", "Content-Type: application/json", "-d", "{}", target],
                capture_output=True, text=True, timeout=20).stdout.strip()
            tot = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "--max-time", "15", "-w", "%{time_total}",
                 "--proxy", proxy, target],
                capture_output=True, text=True, timeout=20).stdout.strip()
        except subprocess.TimeoutExpired:
            code, tot = "TIMEOUT", "-"
        flag = "" if code in ("200", "401", "403") else "  <-- CHECK"
        print(f"  id={p['id']:<3} {p['name'][:18]:<18} {p['host']}:{p['port']:<6} code={code:<8} {tot}s{flag}")


# ---------------------------------------------------------------- import
def cmd_import(args):
    path = args.file
    with open(path, encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]
    parsed = []
    for ln in lines:
        if "@" in ln:  # user:pass@host:port
            creds, hp = ln.rsplit("@", 1)
            login, pw = creds.split(":", 1)
            host, port = hp.rsplit(":", 1)
        else:  # ip:port:user:pass
            parts = ln.split(":")
            if len(parts) != 4:
                print(f"skip (bad format): {ln}")
                continue
            host, port, login, pw = parts
        parsed.append((args.pool, args.protocol, host, int(port), login, pw))
    if not parsed:
        sys.exit("no valid proxies parsed")
    print(f"parsed {len(parsed)} proxies; importing as protocol={args.protocol}, name prefix '{args.pool}-'")
    values = ",".join(
        f"({sqlstr(args.pool + '-' + login[-10:])},{sqlstr(proto)},{sqlstr(host)},{port},{sqlstr(login)},{sqlstr(pw)},'active',now(),now())"
        for (_, proto, host, port, login, pw) in parsed
    )
    out = run_psql(
        "insert into proxies (name,protocol,host,port,username,password,status,created_at,updated_at) "
        f"values {values} returning id,name,host,port;",
        csv_out=False,
    )
    print(out)


# ---------------------------------------------------------------- switch
def cmd_switch(args):
    scope_sql = scope_clause(args.scope)
    if args.to not in POOL_FILTER:
        sys.exit(f"bad --to pool: {args.to}")
    pool_sql = POOL_FILTER[args.to]
    n_acc = rows(f"select count(*) n from accounts a where a.deleted_at is null and {scope_sql}")[0]["n"]
    n_px = rows(f"select count(*) n from proxies p where p.deleted_at is null and p.status='active' and {pool_sql}")[0]["n"]
    print(f"scope '{args.scope}': {n_acc} accounts (with proxy)  ->  pool '{args.to}': {n_px} proxies")
    if int(n_px) == 0:
        sys.exit("target pool has 0 active proxies; aborting")
    if args.dry_run:
        preview = rows(f"""
          with px as (select id, row_number() over (order by id)-1 pn, count(*) over () np
                      from proxies p where p.deleted_at is null and p.status='active' and {pool_sql}),
               acc as (select a.id, row_number() over (order by a.id)-1 rn
                       from accounts a where a.deleted_at is null and {scope_sql})
          select px.id proxy, count(*) cnt from acc join px on px.pn=(acc.rn % px.np) group by px.id order by px.id;""")
        print("dry-run target distribution:")
        for r in preview:
            print(f"  proxy {r['proxy']}: {r['cnt']}")
        return
    ensure_state_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bak = f"{STATE_DIR}/switch-{args.scope.replace(':','_')}-{ts}.json"
    data = scalar(
        "select coalesce(json_agg(json_build_object('id',a.id,'proxy_id',a.proxy_id)),'[]') "
        f"from accounts a where a.deleted_at is null and {scope_sql}"
    )
    with open(bak, "w", encoding="utf-8") as fh:
        fh.write(data)
    print(f"backup written: {bak}")
    out = run_psql(f"""
      with px as (select id, row_number() over (order by id)-1 pn, count(*) over () np
                  from proxies p where p.deleted_at is null and p.status='active' and {pool_sql}),
           acc as (select a.id, row_number() over (order by a.id)-1 rn
                   from accounts a where a.deleted_at is null and {scope_sql})
      update accounts a set proxy_id=px.id, updated_at=now()
      from acc join px on px.pn=(acc.rn % px.np) where a.id=acc.id;""", csv_out=False)
    print(out.strip())


# ---------------------------------------------------------------- panic
def cmd_panic(args):
    ensure_state_dir()
    state = f"{STATE_DIR}/panic_state.json"
    if args.off:
        if not os.path.exists(state):
            sys.exit("no panic_state.json; nothing to restore")
        ids = json.load(open(state))
        if not ids:
            print("panic state empty")
            return
        idlist = ",".join(str(int(i)) for i in ids)
        out = run_psql(f"update accounts set schedulable=true, updated_at=now() where id in ({idlist});", csv_out=False)
        print(f"restored schedulable for {len(ids)} accounts:", out.strip())
        os.remove(state)
        return
    affected = rows(
        f"select a.id from accounts a where a.deleted_at is null and a.proxy_id is not null "
        f"and a.schedulable = true and a.id in (select account_id from account_groups where group_id in {GPT_GROUPS})"
    )
    ids = [int(r["id"]) for r in affected]
    json.dump(ids, open(state, "w"))
    if not ids:
        print("no proxied schedulable accounts to pause")
        return
    idlist = ",".join(str(i) for i in ids)
    out = run_psql(f"update accounts set schedulable=false, updated_at=now() where id in ({idlist});", csv_out=False)
    print(f"PANIC: paused {len(ids)} proxied accounts (direct accounts still serving):", out.strip())
    print(f"state saved to {state}; restore with: proxykit panic --off")


def main():
    ap = argparse.ArgumentParser(prog="proxykit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--window", default=10); s.set_defaults(fn=cmd_status)
    t = sub.add_parser("test"); t.add_argument("pool"); t.add_argument("--target", default="https://chatgpt.com/backend-api/codex/responses"); t.set_defaults(fn=cmd_test)
    i = sub.add_parser("import"); i.add_argument("file"); i.add_argument("--pool", required=True); i.add_argument("--protocol", default="http"); i.set_defaults(fn=cmd_import)
    w = sub.add_parser("switch"); w.add_argument("--scope", required=True); w.add_argument("--to", required=True); w.add_argument("--dry-run", action="store_true"); w.set_defaults(fn=cmd_switch)
    p = sub.add_parser("panic"); p.add_argument("--off", action="store_true"); p.set_defaults(fn=cmd_panic)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
