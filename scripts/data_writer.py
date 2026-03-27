#!/usr/bin/env python3
"""
Data writer for OpenClaw agents. Auto-generated from result-schema.yml.

Supports two backends (auto-detected):
  1. Supabase REST API (HTTPS) — when SUPABASE_URL + SUPABASE_KEY are set
  2. psycopg2 (TCP)           — when PG_CONNECTION_STRING is set

The Supabase REST backend is preferred in Daytona sandboxes where
outbound TCP on port 5432 is blocked but HTTPS (443) works fine.

Commands:
  provision  — Create schema and tables (no-op for Supabase REST; tables pre-created)
  write      — Upsert records into a result table
  query      — Read records from a result table (SELECT only)
"""

import os
import sys
import json
import uuid
import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
PG_URL = os.environ.get("PG_CONNECTION_STRING", "")

USE_SUPABASE_REST = bool(SUPABASE_URL and SUPABASE_KEY)

SCRIPT_DIR = Path(__file__).parent
SCHEMA_PATH = SCRIPT_DIR.parent / "result-schema.yml"


# ══════════════════════════════════════════════════════════════════════════════
# Supabase REST backend (HTTPS, port 443)
# ══════════════════════════════════════════════════════════════════════════════

def _supabase_request(method, path, body=None, params=None, prefer=None):
    """Make authenticated request to Supabase PostgREST."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{qs}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer

    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)

    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else []
    except HTTPError as e:
        err_body = e.read().decode() if e.fp else str(e)
        print(f"ERROR: Supabase {method} {path} → {e.code}: {err_body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Supabase connection failed: {e.reason}", file=sys.stderr)
        sys.exit(1)


def _supabase_provision():
    """Tables are pre-created in public schema. Just verify they exist."""
    for table in ("result_task_scores", "result_daily_digests"):
        try:
            _supabase_request("GET", table, params={"select": "id", "limit": "1"})
        except SystemExit:
            print(f"ERROR: Table '{table}' not accessible via Supabase REST. "
                  "Ensure tables are created in the public schema.", file=sys.stderr)
            sys.exit(1)
    print(json.dumps({"success": True, "backend": "supabase-rest",
                       "tables": ["result_task_scores", "result_daily_digests"]}))


def _supabase_write(table_name, records_json, conflict_columns_csv, run_id=None):
    """Insert/upsert via PostgREST."""
    if not run_id:
        run_id = str(uuid.uuid4())

    records = json.loads(records_json)
    conflict_columns = (
        [c.strip() for c in conflict_columns_csv.split(",") if c.strip()]
        if conflict_columns_csv and conflict_columns_csv.strip()
        and conflict_columns_csv.strip().lower() != "none"
        else []
    )

    for record in records:
        record["run_id"] = run_id
        if "computed_at" not in record:
            record["computed_at"] = datetime.now(timezone.utc).isoformat()
        # Ensure jsonb fields are strings for PostgREST
        for k, v in record.items():
            if isinstance(v, (dict, list)):
                record[k] = json.dumps(v)

    # PostgREST upsert: Prefer: resolution=merge-duplicates
    prefer = "return=minimal"
    params = {}
    if conflict_columns:
        prefer = "resolution=merge-duplicates,return=minimal"
        params["on_conflict"] = ",".join(conflict_columns)

    _supabase_request("POST", table_name, body=records, params=params, prefer=prefer)
    print(json.dumps({"success": True, "records_affected": len(records),
                       "run_id": run_id, "table": table_name, "backend": "supabase-rest"}))


def _supabase_query(table_name, limit=100, order_by=None, where_json=None):
    """Read via PostgREST."""
    params = {"select": "*", "limit": str(limit)}

    if where_json:
        where = json.loads(where_json)
        for col, val in where.items():
            params[col] = f"eq.{val}"

    if order_by:
        # Convert SQL-style "col DESC" to PostgREST "col.desc"
        parts = order_by.strip().split()
        col = parts[0].strip('"')
        direction = parts[1].lower() if len(parts) > 1 else "asc"
        params["order"] = f"{col}.{direction}"

    rows = _supabase_request("GET", table_name, params=params)
    print(json.dumps({"success": True, "table": table_name, "count": len(rows),
                       "records": rows, "backend": "supabase-rest"}))


# ══════════════════════════════════════════════════════════════════════════════
# psycopg2 backend (TCP, port 5432) — fallback for non-Daytona environments
# ══════════════════════════════════════════════════════════════════════════════

def _check_pg_url():
    if not PG_URL:
        print("ERROR: Neither SUPABASE_URL+SUPABASE_KEY nor PG_CONNECTION_STRING set.",
              file=sys.stderr)
        sys.exit(1)


def _get_conn():
    _check_pg_url()
    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2-binary not installed. Run: pip install psycopg2-binary",
              file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(PG_URL)


def _load_schema():
    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
        sys.exit(1)
    if not SCHEMA_PATH.exists():
        print(f"ERROR: result-schema.yml not found at {SCHEMA_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(SCHEMA_PATH) as f:
        return yaml.safe_load(f)


PG_TYPE_MAP = {
    "uuid": "UUID DEFAULT gen_random_uuid()",
    "string": "VARCHAR({})",
    "text": "TEXT",
    "integer": "INTEGER",
    "float": "FLOAT",
    "boolean": "BOOLEAN",
    "datetime": "TIMESTAMPTZ",
    "jsonb": "JSONB",
}


def _sql_col_type(col_cfg):
    col_type = col_cfg.get("type", "text")
    pg = PG_TYPE_MAP.get(col_type, "TEXT")
    if col_type == "string":
        pg = pg.format(col_cfg.get("max_length", 255))
    return pg


def _pg_provision():
    schema_def = _load_schema()
    conn = _get_conn()
    cur = conn.cursor()

    tables = schema_def.get("tables", {})
    for table_name, table_def in tables.items():
        cols_sql = []
        for col_name, col_cfg in table_def.get("columns", {}).items():
            parts = [f'"{col_name}"', _sql_col_type(col_cfg)]
            if col_cfg.get("primary_key"):
                parts.append("PRIMARY KEY")
            elif col_cfg.get("required"):
                parts.append("NOT NULL")
            cols_sql.append(" ".join(parts))

        conflict = table_def.get("conflict_columns", [])
        if conflict:
            quoted = ['"' + c + '"' for c in conflict]
            cols_sql.append("UNIQUE (" + ", ".join(quoted) + ")")

        ddl = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(cols_sql)})'
        cur.execute(ddl)

        if conflict:
            idx_name = f"idx_{table_name}_{'_'.join(conflict)}"
            idx_cols = ", ".join(f'"{c}"' for c in conflict)
            cur.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table_name}" ({idx_cols})')

    conn.commit()
    cur.close()
    conn.close()
    print(json.dumps({"success": True, "backend": "psycopg2", "tables": list(tables.keys())}))


BLOCKED_SQL_KEYWORDS = frozenset({"DROP", "DELETE", "TRUNCATE", "ALTER", "GRANT", "REVOKE"})


def _pg_write(table_name, records_json, conflict_columns_csv, run_id=None):
    if not run_id:
        run_id = str(uuid.uuid4())

    records = json.loads(records_json)
    conflict_columns = (
        [c.strip() for c in conflict_columns_csv.split(",") if c.strip()]
        if conflict_columns_csv and conflict_columns_csv.strip()
        and conflict_columns_csv.strip().lower() != "none"
        else []
    )

    conn = _get_conn()
    cur = conn.cursor()
    total = 0

    for record in records:
        record["run_id"] = run_id
        if "computed_at" not in record:
            record["computed_at"] = datetime.now(timezone.utc).isoformat()

        cols = list(record.keys())
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in record.values()]

        col_str = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))

        if conflict_columns:
            update_cols = [c for c in cols if c not in conflict_columns]
            update_str = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            conflict_str = ", ".join(f'"{c}"' for c in conflict_columns)
            sql = (
                f'INSERT INTO "{table_name}" ({col_str}) '
                f"VALUES ({placeholders}) "
                f"ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
            )
        else:
            sql = f'INSERT INTO "{table_name}" ({col_str}) VALUES ({placeholders})'

        cur.execute(sql, vals)
        total += 1

    conn.commit()
    cur.close()
    conn.close()
    print(json.dumps({"success": True, "records_affected": total,
                       "run_id": run_id, "table": table_name, "backend": "psycopg2"}))


def _pg_query(table_name, limit=100, order_by=None, where_json=None):
    import psycopg2.extras
    conn = _get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    sql = f'SELECT * FROM "{table_name}"'
    params = []

    if where_json:
        where = json.loads(where_json)
        conditions = []
        for col, val in where.items():
            conditions.append(f'"{col}" = %s')
            params.append(val)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

    if order_by:
        safe_order = order_by.replace(";", "").replace("--", "")
        for kw in BLOCKED_SQL_KEYWORDS:
            if kw in safe_order.upper():
                print(json.dumps({"error": f"Blocked keyword in order_by: {kw}"}), file=sys.stderr)
                sys.exit(1)
        sql += f" ORDER BY {safe_order}"

    sql += " LIMIT %s"
    params.append(limit)

    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]

    for row in rows:
        for k, v in row.items():
            if isinstance(v, datetime):
                row[k] = v.isoformat()
            elif isinstance(v, uuid.UUID):
                row[k] = str(v)

    cur.close()
    conn.close()
    print(json.dumps({"success": True, "table": table_name, "count": len(rows),
                       "records": rows, "backend": "psycopg2"}))


# ══════════════════════════════════════════════════════════════════════════════
# Router — picks the right backend automatically
# ══════════════════════════════════════════════════════════════════════════════

def cmd_provision():
    if USE_SUPABASE_REST:
        _supabase_provision()
    else:
        _pg_provision()

def cmd_write(table_name, records_json, conflict_columns_csv, run_id=None):
    if USE_SUPABASE_REST:
        _supabase_write(table_name, records_json, conflict_columns_csv, run_id)
    else:
        _pg_write(table_name, records_json, conflict_columns_csv, run_id)

def cmd_query(table_name, limit=100, order_by=None, where_json=None):
    if USE_SUPABASE_REST:
        _supabase_query(table_name, limit, order_by, where_json)
    else:
        _pg_query(table_name, limit, order_by, where_json)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    backend = "supabase-rest" if USE_SUPABASE_REST else "psycopg2"
    print(f"[data_writer] Using backend: {backend}", file=sys.stderr)

    parser = argparse.ArgumentParser(description="Data writer for OpenClaw agent")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("provision", help="Create/verify tables")

    wp = sub.add_parser("write", help="Upsert records into a result table")
    wp.add_argument("--table", required=True)
    wp.add_argument("--records", required=True, help="JSON array of records")
    wp.add_argument("--conflict", default="", help="Comma-separated conflict columns")
    wp.add_argument("--run-id", default=None)

    qp = sub.add_parser("query", help="Read records from a result table")
    qp.add_argument("--table", required=True)
    qp.add_argument("--limit", type=int, default=100)
    qp.add_argument("--order-by", default=None)
    qp.add_argument("--where", default=None, help="JSON object of column=value filters")

    args = parser.parse_args()

    if args.action == "provision":
        cmd_provision()
    elif args.action == "write":
        cmd_write(args.table, args.records, args.conflict, args.run_id)
    elif args.action == "query":
        cmd_query(args.table, args.limit, args.order_by, args.where)


if __name__ == "__main__":
    main()
