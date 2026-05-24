#!/usr/bin/env python3
"""Generate and insert Sub2API redeem codes on racknerd-us."""

from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"


def normalize_prefix(raw: str) -> str:
    prefix = raw.strip().lower()
    if not prefix:
        raise SystemExit("--prefix is required")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(ch not in allowed for ch in prefix):
        raise SystemExit("--prefix may only contain letters, digits, '-' and '_'")
    return prefix if prefix.endswith("-") else prefix + "-"


def parse_value(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise SystemExit(f"invalid --value: {raw}") from exc
    if value <= 0:
        raise SystemExit("--value must be positive")
    return value.quantize(Decimal("0.00000001"))


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def make_codes(prefix: str, count: int, suffix_length: int | None = None) -> list[str]:
    if count <= 0:
        raise SystemExit("--count must be positive")
    codes: set[str] = set()
    max_suffix_len = 32 - len(prefix)
    if max_suffix_len < 1:
        raise SystemExit("prefix is too long; redeem_codes.code max length is 32")
    suffix_len = suffix_length or min(24, max_suffix_len)
    if suffix_len < 1:
        raise SystemExit("--suffix-length must be positive")
    if suffix_len > max_suffix_len:
        raise SystemExit("prefix plus --suffix-length exceeds redeem_codes.code max length of 32")
    if count > 16**suffix_len:
        raise SystemExit("--count exceeds possible unique codes for --suffix-length")
    while len(codes) < count:
        codes.add(prefix + secrets.token_hex(16)[:suffix_len])
    return sorted(codes)


def build_sql(codes: list[str], value: Decimal, validity_days: int, notes: str) -> str:
    rows = []
    for code in codes:
        rows.append(
            "("
            f"{sql_literal(code)}, "
            "'balance', "
            f"{sql_literal(format(value, 'f'))}::numeric, "
            "'unused', "
            f"{int(validity_days)}, "
            f"{sql_literal(notes)}"
            ")"
        )
    return (
        "BEGIN;\n"
        "DO $$\n"
        "DECLARE inserted_count integer;\n"
        "BEGIN\n"
        "  INSERT INTO redeem_codes (code, type, value, status, validity_days, notes)\n"
        "  VALUES\n    "
        + ",\n    ".join(rows)
        + "\n  ON CONFLICT (code) DO NOTHING;\n"
        "  GET DIAGNOSTICS inserted_count = ROW_COUNT;\n"
        f"  IF inserted_count <> {len(codes)} THEN\n"
        f"    RAISE EXCEPTION 'expected {len(codes)} inserted redeem codes, got %', inserted_count;\n"
        "  END IF;\n"
        "END $$;\n"
        "COMMIT;\n"
    )


def load_codes(path: Path, prefix: str) -> list[str]:
    codes = []
    seen = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        code = raw.strip()
        if not code:
            continue
        if len(code) > 32:
            raise SystemExit(f"code is longer than 32 chars: {code}")
        if not code.startswith(prefix):
            raise SystemExit(f"code does not match prefix {prefix}: {code}")
        if code in seen:
            raise SystemExit(f"duplicate code in input file: {code}")
        seen.add(code)
        codes.append(code)
    if not codes:
        raise SystemExit("--input-file contains no codes")
    return codes


def run_remote_sql(sql_file: Path, timeout: int) -> None:
    sql_text = sql_file.read_text(encoding="utf-8")
    remote_cmd = (
        "set -euo pipefail\n"
        "docker exec -i sub2api-postgres psql -U sub2api -d sub2api -v ON_ERROR_STOP=1 <<'SQL'\n"
        + sql_text
        + "\nSQL\n"
    )
    cmd = [
        sys.executable,
        "-m",
        "ops_toolkit",
        "exec",
        "racknerd-us",
        remote_cmd,
        "--timeout",
        str(timeout),
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Sub2API balance redeem codes")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--count", type=int, help="number of codes to generate; required unless --input-file is used")
    parser.add_argument("--value", required=True, help="USD balance value")
    parser.add_argument("--validity-days", type=int, default=30)
    parser.add_argument("--notes", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--input-file", type=Path, help="reuse an existing newline-separated code file")
    parser.add_argument("--suffix-length", type=int, help="hex suffix length for generated codes")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    prefix = normalize_prefix(args.prefix)
    value = parse_value(args.value)
    if args.validity_days <= 0:
        raise SystemExit("--validity-days must be positive")
    if args.input_file:
        codes = load_codes(args.input_file, prefix)
    else:
        if args.count is None:
            raise SystemExit("--count is required unless --input-file is used")
        codes = make_codes(prefix, args.count, args.suffix_length)

    notes = args.notes.strip() or f"{prefix.rstrip('-')} {len(codes)}x{format(value, 'f').rstrip('0').rstrip('.')}usd batch {datetime.now():%Y%m%d}"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    value_label = format(value, "f").rstrip("0").rstrip(".").replace(".", "p")
    base = OUTPUT_DIR / f"{prefix.rstrip('-')}_{value_label}usd_codes_{ts}"
    txt_file = base.with_suffix(".txt")
    sql_file = base.with_suffix(".sql")

    txt_file.write_text("\n".join(codes) + "\n", encoding="utf-8")
    sql_file.write_text(build_sql(codes, value, args.validity_days, notes), encoding="utf-8")

    if not args.dry_run:
        run_remote_sql(sql_file, args.timeout)

    print(f"codes: {txt_file}")
    print(f"sql:   {sql_file}")
    print(f"count: {len(codes)} value={value} validity_days={args.validity_days} dry_run={args.dry_run}")


if __name__ == "__main__":
    main()
