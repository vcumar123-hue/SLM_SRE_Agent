import json
import os
import sys
from decimal import Decimal

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values


# =========================
# CONFIG
# =========================
TARGET_TABLE = "incidents"
JSON_FILE = "incidents.json"

# You can either set DATABASE_URL
# Example:
#   set DATABASE_URL=postgresql://postgres:postgres@localhost:5432/aiops
#
# Or set the individual vars:
#   set PGHOST=localhost
#   set PGPORT=5432
#   set PGDATABASE=aiops
#   set PGUSER=postgres
#   set PGPASSWORD=postgres


# =========================
# HELPERS
# =========================
def get_conn():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)

    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "postgres"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", "postgres"),
    )


def load_json_records(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("incidents.json must contain a top-level JSON array.")

    cleaned = []
    for row in data:
        if not isinstance(row, dict):
            continue

        # Normalize blank strings to None where useful
        normalized = {}
        for k, v in row.items():
            if isinstance(v, str):
                v = v.strip()
                if v == "":
                    v = None
            normalized[k] = v

        if not normalized.get("id"):
            raise ValueError(f"Found incident without id: {normalized}")

        cleaned.append(normalized)

    return cleaned


def infer_pg_type(values):
    """
    Infer a safe postgres type from sample JSON values.
    Keeps things conservative.
    """
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "TEXT"

    if all(isinstance(v, bool) for v in non_null):
        return "BOOLEAN"

    if all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
        return "BIGINT"

    if all(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in non_null):
        return "DOUBLE PRECISION"

    # If nested data appears later, use JSONB
    if all(isinstance(v, (dict, list)) for v in non_null):
        return "JSONB"

    return "TEXT"


def fetch_existing_columns(cur, table_name):
    cur.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def ensure_target_table(cur, table_name, sample_record):
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        )
        """,
        (table_name,),
    )
    exists = cur.fetchone()[0]

    if exists:
        return

    cols = []
    for k, v in sample_record.items():
        pg_type = infer_pg_type([v])
        if k == "id":
            cols.append(sql.SQL("{} TEXT PRIMARY KEY").format(sql.Identifier(k)))
        else:
            cols.append(
                sql.SQL("{} {}").format(sql.Identifier(k), sql.SQL(pg_type))
            )

    create_stmt = sql.SQL("CREATE TABLE {} ({})").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(cols),
    )
    cur.execute(create_stmt)


def ensure_missing_columns(cur, table_name, records):
    existing_cols = fetch_existing_columns(cur, table_name)

    all_keys = sorted({k for r in records for k in r.keys()})
    for key in all_keys:
        if key in existing_cols:
            continue

        values = [r.get(key) for r in records]
        pg_type = infer_pg_type(values)

        alter_stmt = sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
            sql.Identifier(table_name),
            sql.Identifier(key),
            sql.SQL(pg_type),
        )
        cur.execute(alter_stmt)


def ensure_unique_id(cur, table_name):
    # Ensure id exists
    cols = fetch_existing_columns(cur, table_name)
    if "id" not in cols:
        cur.execute(
            sql.SQL("ALTER TABLE {} ADD COLUMN id TEXT").format(sql.Identifier(table_name))
        )

    # Populate missing ids if any old row somehow has null id
    cur.execute(
        sql.SQL(
            """
            UPDATE {}
            SET id = 'legacy_' || md5(random()::text || clock_timestamp()::text)
            WHERE id IS NULL
            """
        ).format(sql.Identifier(table_name))
    )

    # Add PK or unique index if not already there
    cur.execute(
        """
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = %s::regclass
          AND contype = 'p'
        """,
        (table_name,),
    )
    has_pk = cur.fetchone() is not None

    if not has_pk:
        cur.execute(
            sql.SQL(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                          AND tablename = {table_name}
                          AND indexname = {index_name}
                    ) THEN
                        EXECUTE 'CREATE UNIQUE INDEX ' || quote_ident({index_name}) ||
                                ' ON ' || quote_ident({table_name}) || ' (id)';
                    END IF;
                END
                $$;
                """
            ).format(
                table_name=sql.Literal(table_name),
                index_name=sql.Literal(f"{table_name}_id_uq"),
            )
        )


def create_backup(cur, table_name):
    backup_table = f"{table_name}_backup_before_merge"
    cur.execute(
        sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(backup_table))
    )
    cur.execute(
        sql.SQL("CREATE TABLE {} AS TABLE {}").format(
            sql.Identifier(backup_table),
            sql.Identifier(table_name),
        )
    )


def create_staging_table(cur, stage_table, records):
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(stage_table)))

    all_keys = sorted({k for r in records for k in r.keys()})
    col_defs = []
    for key in all_keys:
        values = [r.get(key) for r in records]
        pg_type = infer_pg_type(values)
        col_defs.append(
            sql.SQL("{} {}").format(sql.Identifier(key), sql.SQL(pg_type))
        )

    cur.execute(
        sql.SQL("CREATE TEMP TABLE {} ({})").format(
            sql.Identifier(stage_table),
            sql.SQL(", ").join(col_defs),
        )
    )

    rows = []
    for r in records:
        row = []
        for key in all_keys:
            val = r.get(key)
            if isinstance(val, (dict, list)):
                val = json.dumps(val)
            row.append(val)
        rows.append(tuple(row))

    insert_stmt = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
        sql.Identifier(stage_table),
        sql.SQL(", ").join(map(sql.Identifier, all_keys)),
    )

    execute_values(cur, insert_stmt.as_string(cur.connection), rows, page_size=500)
    return all_keys


def upsert_from_stage(cur, target_table, stage_table, columns):
    non_id_cols = [c for c in columns if c != "id"]
    if not non_id_cols:
        return

    insert_stmt = sql.SQL(
        """
        INSERT INTO {target} ({cols})
        SELECT {cols}
        FROM {stage}
        ON CONFLICT (id)
        DO UPDATE SET
        {updates}
        """
    ).format(
        target=sql.Identifier(target_table),
        stage=sql.Identifier(stage_table),
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        updates=sql.SQL(", ").join(
            sql.SQL("{col} = COALESCE(EXCLUDED.{col}, {target}.{col})").format(
                col=sql.Identifier(c),
                target=sql.Identifier(target_table),
            )
            for c in non_id_cols
        ),
    )
    cur.execute(insert_stmt)


def normalize_taxonomy_fields(cur, table_name):
    # Normalize common taxonomy-like fields across ALL existing rows
    taxonomy_cols = [
        "domain",
        "classification",
        "service_scope",
        "environment",
        "severity",
        "source",
    ]
    existing_cols = fetch_existing_columns(cur, table_name)

    for col in taxonomy_cols:
        if col in existing_cols:
            cur.execute(
                sql.SQL(
                    """
                    UPDATE {}
                    SET {} = lower(trim({}))
                    WHERE {} IS NOT NULL
                    """
                ).format(
                    sql.Identifier(table_name),
                    sql.Identifier(col),
                    sql.Identifier(col),
                    sql.Identifier(col),
                )
            )

    # Confidence default
    if "confidence" in existing_cols:
        cur.execute(
            sql.SQL(
                """
                UPDATE {}
                SET confidence = 0.0
                WHERE confidence IS NULL
                """
            ).format(sql.Identifier(table_name))
        )


def main():
    json_path = sys.argv[1] if len(sys.argv) > 1 else JSON_FILE
    records = load_json_records(json_path)

    if not records:
        print("No records found in JSON. Nothing to do.")
        return

    conn = get_conn()
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # 1) Ensure target table exists
            ensure_target_table(cur, TARGET_TABLE, records[0])

            # 2) Ensure schema supports latest JSON fields
            ensure_missing_columns(cur, TARGET_TABLE, records)

            # 3) Ensure id uniqueness for upsert
            ensure_unique_id(cur, TARGET_TABLE)

            # 4) Backup current live data before merge
            create_backup(cur, TARGET_TABLE)

            # 5) Stage latest incidents.json
            stage_table = "incidents_stage_latest"
            cols = create_staging_table(cur, stage_table, records)

            # 6) Upsert: update matching ids, insert new ids
            upsert_from_stage(cur, TARGET_TABLE, stage_table, cols)

            # 7) Normalize taxonomy-like fields across all existing rows
            normalize_taxonomy_fields(cur, TARGET_TABLE)

            # 8) Optional: created/updated audit columns if they exist
            existing_cols = fetch_existing_columns(cur, TARGET_TABLE)
            if "updated_at" in existing_cols:
                cur.execute(
                    sql.SQL(
                        """
                        UPDATE {}
                        SET updated_at = NOW()
                        """
                    ).format(sql.Identifier(TARGET_TABLE))
                )

        conn.commit()
        print("Merge completed successfully.")
        print(f"Backup table created: {TARGET_TABLE}_backup_before_merge")
        print("Existing data preserved, latest JSON schema incorporated, taxonomy fields normalized.")

    except Exception as e:
        conn.rollback()
        print("Merge failed. Transaction rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()