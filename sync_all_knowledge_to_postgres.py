import json
import os
import sys
from typing import Any, Dict, List

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, execute_values


INCIDENTS_TABLE = "incidents"
FEEDBACK_TABLE = "feedback_events"
TAXONOMY_TABLE = "taxonomy"


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "aiops"),
        user=os.getenv("PGUSER", "aiops_user"),
        password=os.getenv("PGPASSWORD"),
    )


def load_json(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    return data


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        )
        """,
        (table_name,),
    )
    return cur.fetchone()[0]


def get_columns(cur, table_name: str) -> Dict[str, str]:
    cur.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    return {r[0]: r[1] for r in cur.fetchall()}


def infer_type(values: List[Any]) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "TEXT"

    if all(isinstance(v, bool) for v in vals):
        return "BOOLEAN"

    if all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return "BIGINT"

    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return "DOUBLE PRECISION"

    if all(isinstance(v, (list, dict)) for v in vals):
        return "JSONB"

    return "TEXT"


def ensure_table(cur, table_name: str, rows: List[Dict[str, Any]], pk: str = None):
    if table_exists(cur, table_name):
        return

    all_keys = sorted({k for row in rows for k in row.keys()})
    col_defs = []
    for key in all_keys:
        col_type = infer_type([row.get(key) for row in rows])
        if key == pk:
            col_defs.append(sql.SQL("{} TEXT PRIMARY KEY").format(sql.Identifier(key)))
        else:
            col_defs.append(sql.SQL("{} {}").format(sql.Identifier(key), sql.SQL(col_type)))

    stmt = sql.SQL("CREATE TABLE {} ({})").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(col_defs),
    )
    cur.execute(stmt)


def add_missing_columns(cur, table_name: str, rows: List[Dict[str, Any]]):
    existing = get_columns(cur, table_name)
    all_keys = sorted({k for row in rows for k in row.keys()})

    for key in all_keys:
        if key in existing:
            continue
        col_type = infer_type([row.get(key) for row in rows])
        cur.execute(
            sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                sql.Identifier(table_name),
                sql.Identifier(key),
                sql.SQL(col_type),
            )
        )


def normalize_row(row: Dict[str, Any], table_name: str = None) -> Dict[str, Any]:
    out = {}
    for k, v in row.items():
        if isinstance(v, str):
            v = v.strip()
            if v == "":
                v = None
        out[k] = v

    if table_name == FEEDBACK_TABLE:
        if out.get("corrected_classification") is None and out.get("corrected") is not None:
            out["corrected_classification"] = str(out["corrected"]).strip().lower().replace(" ", "_")

        if out.get("original_classification") is None and out.get("predicted") is not None:
            out["original_classification"] = str(out["predicted"]).strip().lower().replace(" ", "_")

        if out.get("stored_domain") is None and out.get("domain") is not None:
            out["stored_domain"] = str(out["domain"]).strip().lower()

        if out.get("submitted_domain") is None and out.get("domain") is not None:
            out["submitted_domain"] = str(out["domain"]).strip().lower()

        if out.get("original_domain") is None and out.get("domain") is not None:
            out["original_domain"] = str(out["domain"]).strip().lower()

        if out.get("is_correct") is None:
            oc = out.get("original_classification")
            cc = out.get("corrected_classification")
            if oc is not None and cc is not None:
                out["is_correct"] = (oc == cc)

    return out


def insert_rows(cur, table_name: str, rows: List[Dict[str, Any]]):
    if not rows:
        return

    rows = [normalize_row(r, table_name) for r in rows]
    cols = sorted({k for r in rows for k in r.keys()})

    values = []
    for row in rows:
        record = []
        for c in cols:
            v = row.get(c)
            if isinstance(v, (dict, list)):
                v = Json(v)
            record.append(v)
        values.append(tuple(record))

    stmt = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
    )
    execute_values(cur, stmt.as_string(cur.connection), values, page_size=500)


def upsert_incidents(cur, rows: List[Dict[str, Any]]):
    rows = [normalize_row(r, INCIDENTS_TABLE) for r in rows]
    add_missing_columns(cur, INCIDENTS_TABLE, rows)
    cols = sorted({k for r in rows for k in r.keys()})
    non_id = [c for c in cols if c != "id"]

    values = []
    for row in rows:
        record = []
        for c in cols:
            v = row.get(c)
            if isinstance(v, (dict, list)):
                v = Json(v)
            record.append(v)
        values.append(tuple(record))

    stmt = sql.SQL(
        """
        INSERT INTO {table} ({cols})
        VALUES %s
        ON CONFLICT (id)
        DO UPDATE SET
        {updates}
        """
    ).format(
        table=sql.Identifier(INCIDENTS_TABLE),
        cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
        updates=sql.SQL(", ").join(
            sql.SQL("{c} = COALESCE(EXCLUDED.{c}, {t}.{c})").format(
                c=sql.Identifier(c), t=sql.Identifier(INCIDENTS_TABLE)
            )
            for c in non_id
        ),
    )
    execute_values(cur, stmt.as_string(cur.connection), values, page_size=500)


def reset_and_load_taxonomy(cur, rows: List[Dict[str, Any]]):
    ensure_table(cur, TAXONOMY_TABLE, rows, pk=None)
    add_missing_columns(cur, TAXONOMY_TABLE, rows)

    cur.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(TAXONOMY_TABLE)))
    insert_rows(cur, TAXONOMY_TABLE, rows)

    # helpful indexes
    cur.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS idx_taxonomy_classification ON {} (classification)").format(
            sql.Identifier(TAXONOMY_TABLE)
        )
    )
    cur.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS idx_taxonomy_tower ON {} (tower)").format(
            sql.Identifier(TAXONOMY_TABLE)
        )
    )


def reset_and_load_feedback(cur, rows: List[Dict[str, Any]]):
    ensure_table(cur, FEEDBACK_TABLE, rows, pk=None)
    add_missing_columns(cur, FEEDBACK_TABLE, rows)

    # Preserve history with a backup, then reload full latest file
    backup_name = FEEDBACK_TABLE + "_backup_before_sync"
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(backup_name)))
    cur.execute(
        sql.SQL("CREATE TABLE {} AS TABLE {}").format(
            sql.Identifier(backup_name),
            sql.Identifier(FEEDBACK_TABLE),
        )
    )

    # Relax old constraints so mixed-schema feedback rows can load
    cur.execute(
        sql.SQL("ALTER TABLE {} ALTER COLUMN corrected_classification DROP NOT NULL").format(
            sql.Identifier(FEEDBACK_TABLE)
        )
    )

    # Optional: also relax this if your table has it constrained
    cur.execute(
        sql.SQL("ALTER TABLE {} ALTER COLUMN original_classification DROP NOT NULL").format(
            sql.Identifier(FEEDBACK_TABLE)
        )
    )

    cur.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(FEEDBACK_TABLE)))
    insert_rows(cur, FEEDBACK_TABLE, rows)

    cur.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS idx_feedback_request_id ON {} (request_id)").format(
            sql.Identifier(FEEDBACK_TABLE)
        )
    )
    cur.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS idx_feedback_corrected_classification ON {} (corrected_classification)").format(
            sql.Identifier(FEEDBACK_TABLE)
        )
    )


def print_counts(cur):
    for table in [INCIDENTS_TABLE, FEEDBACK_TABLE, TAXONOMY_TABLE]:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
        count = cur.fetchone()[0]
        print(f"{table}: {count} rows")


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else r"C:\Projects\ai\slm\data"

    incidents_path = os.path.join(base, "incidents.json")
    feedback_path = os.path.join(base, "feedback.json")
    taxonomy_path = os.path.join(base, "taxonomy.json")

    incidents = load_json(incidents_path)
    feedback = load_json(feedback_path)
    taxonomy = load_json(taxonomy_path)

    conn = get_conn()
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            ensure_table(cur, INCIDENTS_TABLE, incidents, pk="id")
            upsert_incidents(cur, incidents)

            reset_and_load_feedback(cur, feedback)
            reset_and_load_taxonomy(cur, taxonomy)

        conn.commit()

        with conn.cursor() as cur:
            print("Sync completed successfully.")
            print_counts(cur)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()