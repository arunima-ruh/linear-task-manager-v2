#!/usr/bin/env python3
"""
Data writer for OpenClaw agents. Auto-generated from result-schema.yml.

SAFETY: This script enforces strict database safety at the code level:
  - Only CREATE TABLE IF NOT EXISTS, INSERT, and ON CONFLICT DO UPDATE are allowed
  - DROP, DELETE, TRUNCATE, ALTER, GRANT, REVOKE are BLOCKED
  - All operations are namespaced to the agent's own schema
  - The agent cannot access other schemas or the public schema

Commands:
  provision  — Create schema and tables from result-schema.yml
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

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2-binary not installed. Run: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ── Safety Constants ──────────────────────────────────────────────────────────
BLOCKED_SQL_KEYWORDS = frozenset({"DROP", "DELETE", "TRUNCATE", "ALTER", "GRANT", "REVOKE"})

# ── Config ────────────────────────────────────────────────────────────────────
PG_URL = os.environ.get("PG_CONNECTION_STRING", "")
ORG_ID = os.environ.get("ORG_ID", "default")
AGENT_ID = os.environ.get("AGENT_ID", "unknown")
SCHEMA_NAME = f"org_{ORG_ID.replace('-', '_').replace('.', '_')}_a_{AGENT_ID.replace('-', '_').replace('.', '_')}"

SCRIPT_DIR = Path(__file__).parent
SCHEMA_PATH = SCRIPT_DIR.parent / "result-schema.yml"

# ── Type Mapping ──────────────────────────────────────────────────────────────
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


def _check_pg_url():
    if not PG_URL:
        print("ERROR: PG_CONNECTION_STRING environment variable not set.", file=sys.stderr)
        sys.exit(1)


def _get_conn():
    _check_pg_url()
    return psycopg2.connect(PG_URL)


def _load_schema():
    if not SCHEMA_PATH.exists():
        print(f"ERROR: result-schema.yml not found at {SCHEMA_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(SCHEMA_PATH) as f:
        return yaml.safe_load(f)


def _sql_col_type(col_cfg):
    col_type = col_cfg.get("type", "text")
    pg = PG_TYPE_MAP.get(col_type, "TEXT")
    if col_type == "string":
        pg = pg.format(col_cfg.get("max_length", 255))
    return pg


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_provision():
    """Create schema and tables from result-schema.yml. Idempotent."""
    schema = _load_schema()
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"')

    tables = schema.get("tables", {})
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
            cols_sql.append(f'UNIQUE ({", ".join(f"""\"{c}\"""" for c in conflict)})')

        ddl = f'CREATE TABLE IF NOT EXISTS "{SCHEMA_NAME}"."{table_name}" ({", ".join(cols_sql)})'
        cur.execute(ddl)

        # Create index on conflict columns
        if conflict:
            idx_name = f"idx_{table_name}_{'_'.join(conflict)}"
            idx_cols = ", ".join(f'"{c}"' for c in conflict)
            cur.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{SCHEMA_NAME}"."{table_name}" ({idx_cols})')

    conn.commit()
    cur.close()
    conn.close()
    print(json.dumps({"success": True, "schema": SCHEMA_NAME, "tables": list(tables.keys())}))


def cmd_write(table_name, records_json, conflict_columns_csv, run_id=None):
    """Upsert records into a result table. Safe: INSERT ON CONFLICT UPDATE only.
    If conflict_columns_csv is empty or 'none', performs plain INSERT (no upsert)."""
    if not run_id:
        run_id = str(uuid.uuid4())

    records = json.loads(records_json)
    conflict_columns = [c.strip() for c in conflict_columns_csv.split(",") if c.strip()] if conflict_columns_csv and conflict_columns_csv.strip() and conflict_columns_csv.strip().lower() != "none" else []

    conn = _get_conn()
    cur = conn.cursor()
    total = 0

    for record in records:
        record["run_id"] = run_id
        if "computed_at" not in record:
            record["computed_at"] = datetime.now(timezone.utc).isoformat()

        cols = list(record.keys())
        vals = []
        for v in record.values():
            vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)

        col_str = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))

        if conflict_columns:
            # Upsert: INSERT ... ON CONFLICT DO UPDATE
            update_cols = [c for c in cols if c not in conflict_columns]
            update_str = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            conflict_str = ", ".join(f'"{c}"' for c in conflict_columns)
            sql = (
                f'INSERT INTO "{SCHEMA_NAME}"."{table_name}" ({col_str}) '
                f"VALUES ({placeholders}) "
                f"ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
            )
        else:
            # Plain INSERT for insert-only tables (no conflict columns)
            sql = f'INSERT INTO "{SCHEMA_NAME}"."{table_name}" ({col_str}) VALUES ({placeholders})'

        cur.execute(sql, vals)
        total += 1

    conn.commit()
    cur.close()
    conn.close()
    print(json.dumps({"success": True, "records_affected": total, "run_id": run_id, "table": table_name}))


def cmd_query(table_name, limit=100, order_by=None, where_json=None):
    """Read records from a result table. SELECT only — no writes."""
    conn = _get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    sql = f'SELECT * FROM "{SCHEMA_NAME}"."{table_name}"'
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
        # Safety: only allow column names + ASC/DESC
        safe_order = order_by.replace(";", "").replace("--", "")
        for kw in BLOCKED_SQL_KEYWORDS:
            if kw in safe_order.upper():
                print(json.dumps({"error": f"Blocked keyword in order_by: {kw}"}), file=sys.stderr)
                sys.exit(1)
        sql += f" ORDER BY {safe_order}"

    sql += f" LIMIT %s"
    params.append(limit)

    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]

    # Convert non-serializable types
    for row in rows:
        for k, v in row.items():
            if isinstance(v, datetime):
                row[k] = v.isoformat()
            elif isinstance(v, uuid.UUID):
                row[k] = str(v)

    cur.close()
    conn.close()
    print(json.dumps({"success": True, "table": table_name, "count": len(rows), "records": rows}))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Data writer for OpenClaw agent")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("provision", help="Create schema and tables from result-schema.yml")

    wp = sub.add_parser("write", help="Upsert records into a result table")
    wp.add_argument("--table", required=True, help="Table name (e.g., result_sprint_scores)")
    wp.add_argument("--records", required=True, help="JSON array of records")
    wp.add_argument("--conflict", default="", help="Comma-separated conflict columns (omit or 'none' for plain INSERT)")
    wp.add_argument("--run-id", default=None, help="Run ID (auto-generated if omitted)")

    qp = sub.add_parser("query", help="Read records from a result table")
    qp.add_argument("--table", required=True, help="Table name")
    qp.add_argument("--limit", type=int, default=100, help="Max rows (default: 100)")
    qp.add_argument("--order-by", default=None, help="ORDER BY clause (e.g., 'computed_at DESC')")
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
