import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLANNER = Path(os.environ.get("PLANNER", "/app/planner/plan.py"))

PSQL = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def q(name):
    return '"' + str(name).replace('"', '""') + '"'


def psql(sql, db="postgres", check=True):
    proc = run(PSQL + ["-d", db, "-c", sql])
    if check and proc.returncode != 0:
        raise AssertionError(f"psql failed: {proc.stderr}\nSQL: {sql}")
    return proc


def query_json(sql, db):
    proc = psql(sql, db)
    text = proc.stdout.strip()
    return json.loads(text) if text else None


def apply_sql(path, db):
    proc = run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", db, "-f", str(path)])
    if proc.returncode != 0:
        raise AssertionError(f"SQL failed: {proc.stderr}\n{proc.stdout}")
    return proc


def fresh_db(name):
    run(["dropdb", "--if-exists", name])
    proc = run(["createdb", name])
    assert proc.returncode == 0, proc.stderr
    return name


def drop_db(name):
    run(["dropdb", "--if-exists", name])


def column_sql(column):
    text = f"{q(column['name'])} {column['type']}"
    if column.get("default") is not None:
        text += f" DEFAULT {column['default']}"
    if not column.get("nullable", True):
        text += " NOT NULL"
    return text


def create_schema(schema, db):
    tables = schema["tables"]
    for table in tables:
        columns = ", ".join(column_sql(column) for column in table["columns"])
        primary_key = table.get("primary_key")
        if primary_key:
            columns += ", PRIMARY KEY (" + ", ".join(q(name) for name in primary_key) + ")"
        psql(f"CREATE TABLE {q(table['name'])} ({columns})", db)

    for table in tables:
        for foreign_key in table.get("foreign_keys", []):
            columns = ", ".join(q(name) for name in foreign_key["columns"])
            ref = foreign_key["references"]
            ref_columns = ", ".join(q(name) for name in ref["columns"])
            text = (
                f"ALTER TABLE {q(table['name'])} ADD CONSTRAINT {q(foreign_key['name'])} "
                f"FOREIGN KEY ({columns}) REFERENCES {q(ref['table'])} ({ref_columns})"
            )
            if foreign_key.get("on_delete"):
                text += f" ON DELETE {foreign_key['on_delete']}"
            psql(text, db)

    for table in tables:
        for index in table.get("indexes", []):
            unique = "UNIQUE " if index.get("unique") else ""
            columns = ", ".join(q(name) for name in index["columns"])
            psql(
                f"CREATE {unique}INDEX {q(index['name'])} ON {q(table['name'])} ({columns})",
                db,
            )


def columns(db, table):
    sql = f"""
        SELECT coalesce(json_agg(t), '[]'::json)
        FROM (
            SELECT column_name, data_type, character_maximum_length, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = {lit(table)}
            ORDER BY ordinal_position
        ) t
    """
    rows = query_json(sql, db)
    return {
        row["column_name"]: (
            row["data_type"],
            row["character_maximum_length"],
            row["is_nullable"],
        )
        for row in rows
    }


def rows(db, sql):
    return query_json(
        f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t",
        db,
    )


def foreign_key_count(db):
    result = query_json(
        """
        SELECT coalesce(json_agg(t), '[]'::json)
        FROM (
            SELECT conname
            FROM pg_constraint
            WHERE contype = 'f' AND connamespace = 'public'::regnamespace
        ) t
        """,
        db,
    )
    return len(result)


def table_exists(db, table):
    result = query_json(
        "SELECT coalesce(json_agg(t), '[]'::json) FROM ("
        f"SELECT to_regclass({lit('public.' + table)}) IS NOT NULL AS present"
        ") t",
        db,
    )
    return result[0]["present"]


def column_defaults(db, table):
    sql = f"""
        SELECT coalesce(json_agg(t), '[]'::json)
        FROM (
            SELECT column_name, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = {lit(table)}
        ) t
    """
    return {
        row["column_name"]: row["column_default"]
        for row in query_json(sql, db)
    }


def column_type(db, table, column):
    sql = f"""
        SELECT coalesce(json_agg(t), '[]'::json)
        FROM (
            SELECT format_type(a.atttypid, a.atttypmod) AS type
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = {lit(table)}
              AND a.attname = {lit(column)}
              AND a.attnum > 0
              AND NOT a.attisdropped
        ) t
    """
    result = query_json(sql, db)
    return result[0]["type"] if result else None


def primary_key_columns(db, table):
    sql = f"""
        SELECT coalesce(json_agg(t), '[]'::json)
        FROM (
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class r ON r.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = r.oid AND a.attnum = k.attnum
            WHERE c.contype = 'p'
              AND n.nspname = 'public'
              AND r.relname = {lit(table)}
            ORDER BY k.ord
        ) t
    """
    return [row["attname"] for row in query_json(sql, db)]


def index_names(db, table):
    sql = f"""
        SELECT coalesce(json_agg(t), '[]'::json)
        FROM (
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = {lit(table)}
        ) t
    """
    return {row["indexname"] for row in query_json(sql, db)}


def foreign_key_delete_action(db, name):
    sql = f"""
        SELECT coalesce(json_agg(t), '[]'::json)
        FROM (
            SELECT confdeltype
            FROM pg_constraint
            WHERE conname = {lit(name)}
              AND connamespace = 'public'::regnamespace
        ) t
    """
    result = query_json(sql, db)
    return result[0]["confdeltype"] if result else None


def run_planner(before, after, outdir, allow=False):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    before_path = outdir / "before.json"
    after_path = outdir / "after.json"
    before_path.write_text(json.dumps(before), encoding="utf-8")
    after_path.write_text(json.dumps(after), encoding="utf-8")
    command = [
        sys.executable,
        str(PLANNER),
        "--before",
        str(before_path),
        "--after",
        str(after_path),
        "--out-dir",
        str(outdir),
    ]
    if allow:
        command.append("--allow-destructive")
    return run(command)


def basic_before():
    return {
        "tables": [
            {
                "name": "users",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "email", "type": "varchar(120)", "nullable": False},
                ],
                "primary_key": ["id"],
                "indexes": [
                    {"name": "users_email_key", "columns": ["email"], "unique": True}
                ],
            }
        ]
    }


def basic_after():
    return {
        "tables": [
            {
                "name": "users",
                "columns": [
                    {"name": "id", "type": "bigint", "nullable": False},
                    {"name": "email", "type": "varchar(160)", "nullable": False},
                    {"name": "display_name", "type": "text", "nullable": True},
                ],
                "primary_key": ["id"],
                "indexes": [
                    {"name": "users_email_key", "columns": ["email"], "unique": True}
                ],
            }
        ]
    }


def test_widen_add_and_rollback_preserves_rows(tmp_path):
    db = fresh_db("t_basic")
    try:
        before = basic_before()
        after = basic_after()
        create_schema(before, db)
        psql(
            "INSERT INTO users (id, email) VALUES (1, 'a@example.com'), (2, 'b@example.com')",
            db,
        )

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        cols = columns(db, "users")
        assert cols["id"][0] == "bigint"
        assert cols["email"][0] == "character varying"
        assert cols["email"][1] == 160
        assert cols["display_name"][0] == "text"
        assert rows(db, "SELECT id, email, display_name FROM users ORDER BY id") == [
            {"id": 1, "email": "a@example.com", "display_name": None},
            {"id": 2, "email": "b@example.com", "display_name": None},
        ]

        apply_sql(out / "rollback.sql", db)
        cols = columns(db, "users")
        assert cols["id"][0] == "integer"
        assert cols["email"][1] == 120
        assert "display_name" not in cols
        assert rows(db, "SELECT id, email FROM users ORDER BY id") == [
            {"id": 1, "email": "a@example.com"},
            {"id": 2, "email": "b@example.com"},
        ]
    finally:
        drop_db(db)


def test_safe_rename_preserves_data(tmp_path):
    db = fresh_db("t_rename")
    try:
        before = {
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "full_name", "type": "text", "nullable": False},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        after = {
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "name", "type": "text", "nullable": False},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        create_schema(before, db)
        psql("INSERT INTO people (id, full_name) VALUES (1, 'Ada')", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        cols = columns(db, "people")
        assert "name" in cols
        assert "full_name" not in cols
        assert rows(db, "SELECT id, name FROM people ORDER BY id") == [
            {"id": 1, "name": "Ada"}
        ]

        apply_sql(out / "rollback.sql", db)
        cols = columns(db, "people")
        assert "full_name" in cols
        assert "name" not in cols
        assert rows(db, "SELECT id, full_name FROM people ORDER BY id") == [
            {"id": 1, "full_name": "Ada"}
        ]
    finally:
        drop_db(db)


def test_foreign_key_cycle_and_rollback(tmp_path):
    db = fresh_db("t_fk")
    try:
        before = {
            "tables": [
                {
                    "name": "a",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "b_id", "type": "integer", "nullable": True},
                    ],
                    "primary_key": ["id"],
                },
                {
                    "name": "b",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "a_id", "type": "integer", "nullable": True},
                    ],
                    "primary_key": ["id"],
                },
            ]
        }
        after = {
            "tables": [
                {
                    "name": "a",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "b_id", "type": "integer", "nullable": True},
                        {"name": "label", "type": "text", "nullable": True},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {
                            "name": "a_b_fk",
                            "columns": ["b_id"],
                            "references": {"table": "b", "columns": ["id"]},
                        }
                    ],
                },
                {
                    "name": "b",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "a_id", "type": "integer", "nullable": True},
                        {"name": "label", "type": "text", "nullable": True},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {
                            "name": "b_a_fk",
                            "columns": ["a_id"],
                            "references": {"table": "a", "columns": ["id"]},
                        }
                    ],
                },
            ]
        }
        create_schema(before, db)
        psql("INSERT INTO a (id) VALUES (1); INSERT INTO b (id) VALUES (1)", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        assert foreign_key_count(db) == 2
        psql("UPDATE a SET b_id = 1; UPDATE b SET a_id = 1", db)
        assert rows(db, "SELECT id, b_id, label FROM a") == [
            {"id": 1, "b_id": 1, "label": None}
        ]

        apply_sql(out / "rollback.sql", db)
        assert foreign_key_count(db) == 0
        assert "label" not in columns(db, "a")
        assert "label" not in columns(db, "b")
        assert rows(db, "SELECT id, b_id FROM a") == [{"id": 1, "b_id": 1}]
    finally:
        drop_db(db)


def _destructive_case(tmp_path, before, after, seed, verify):
    db = fresh_db("t_destructive")
    try:
        create_schema(before, db)
        if seed:
            psql(seed, db)

        blocked = tmp_path / "blocked"
        proc = run_planner(before, after, blocked)
        assert proc.returncode != 0
        assert "destructive" in (proc.stderr + proc.stdout).lower()
        assert not (blocked / "migration.sql").exists()
        assert not (blocked / "rollback.sql").exists()

        allowed = tmp_path / "allowed"
        proc = run_planner(before, after, allowed, allow=True)
        assert proc.returncode == 0, proc.stderr
        apply_sql(allowed / "migration.sql", db)
        verify(db)
    finally:
        drop_db(db)


def test_destructive_drop_column_requires_flag(tmp_path):
    before = {
        "tables": [
            {
                "name": "items",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "legacy", "type": "text", "nullable": True},
                    {"name": "price", "type": "integer", "nullable": False},
                ],
                "primary_key": ["id"],
            }
        ]
    }
    after = {
        "tables": [
            {
                "name": "items",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "price", "type": "integer", "nullable": False},
                ],
                "primary_key": ["id"],
            }
        ]
    }

    def verify(db):
        assert "legacy" not in columns(db, "items")
        assert rows(db, "SELECT id, price FROM items") == [{"id": 1, "price": 10}]

    _destructive_case(
        tmp_path,
        before,
        after,
        "INSERT INTO items (id, legacy, price) VALUES (1, 'old', 10)",
        verify,
    )


def test_destructive_drop_table_requires_flag(tmp_path):
    before = {
        "tables": [
            {
                "name": "keep",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "value", "type": "text", "nullable": True},
                ],
                "primary_key": ["id"],
            },
            {
                "name": "gone",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                ],
                "primary_key": ["id"],
            },
        ]
    }
    after = {"tables": [before["tables"][0]]}

    def verify(db):
        assert not table_exists(db, "gone")
        assert rows(db, "SELECT id, value FROM keep") == [{"id": 1, "value": "kept"}]

    _destructive_case(
        tmp_path,
        before,
        after,
        "INSERT INTO keep (id, value) VALUES (1, 'kept')",
        verify,
    )


def test_destructive_narrow_type_requires_flag(tmp_path):
    before = {
        "tables": [
            {
                "name": "docs",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "code", "type": "varchar(160)", "nullable": False},
                ],
                "primary_key": ["id"],
            }
        ]
    }
    after = {
        "tables": [
            {
                "name": "docs",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "code", "type": "varchar(80)", "nullable": False},
                ],
                "primary_key": ["id"],
            }
        ]
    }

    def verify(db):
        assert columns(db, "docs")["code"][1] == 80

    _destructive_case(
        tmp_path,
        before,
        after,
        "INSERT INTO docs (id, code) VALUES (1, 'abc')",
        verify,
    )


def test_destructive_set_not_null_requires_flag(tmp_path):
    before = {
        "tables": [
            {
                "name": "notes",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "body", "type": "text", "nullable": True},
                ],
                "primary_key": ["id"],
            }
        ]
    }
    after = {
        "tables": [
            {
                "name": "notes",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "body", "type": "text", "nullable": False},
                ],
                "primary_key": ["id"],
            }
        ]
    }

    def verify(db):
        assert columns(db, "notes")["body"][2] == "NO"

    _destructive_case(
        tmp_path,
        before,
        after,
        "INSERT INTO notes (id, body) VALUES (1, 'x')",
        verify,
    )


def test_destructive_add_not_null_without_default_requires_flag(tmp_path):
    before = {
        "tables": [
            {
                "name": "empty",
                "columns": [{"name": "id", "type": "integer", "nullable": False}],
                "primary_key": ["id"],
            }
        ]
    }
    after = {
        "tables": [
            {
                "name": "empty",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "label", "type": "text", "nullable": False},
                ],
                "primary_key": ["id"],
            }
        ]
    }

    def verify(db):
        assert columns(db, "empty")["label"][2] == "NO"

    _destructive_case(tmp_path, before, after, None, verify)


def test_deterministic_output(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    first = run_planner(basic_before(), basic_after(), out_a)
    second = run_planner(basic_before(), basic_after(), out_b)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (out_a / "migration.sql").read_bytes() == (out_b / "migration.sql").read_bytes()
    assert (out_a / "rollback.sql").read_bytes() == (out_b / "rollback.sql").read_bytes()


def test_noop_is_applyable(tmp_path):
    db = fresh_db("t_noop")
    try:
        before = basic_before()
        create_schema(before, db)
        psql("INSERT INTO users (id, email) VALUES (1, 'a@example.com')", db)
        out = tmp_path / "out"
        proc = run_planner(before, before, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)
        apply_sql(out / "rollback.sql", db)
        assert rows(db, "SELECT id, email FROM users") == [
            {"id": 1, "email": "a@example.com"}
        ]
    finally:
        drop_db(db)


def type_schema(type_name):
    return {
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "v", "type": type_name, "nullable": True},
                ],
                "primary_key": ["id"],
            }
        ]
    }


@pytest.mark.parametrize(
    "old_type,new_type,seed,expected_type,expected_value",
    [
        ("varchar(120)", "varchar(160)", "'abc'", "character varying(160)", "abc"),
        ("varchar(120)", "text", "'abc'", "text", "abc"),
        ("numeric(10,2)", "numeric(12,2)", "42.50", "numeric(12,2)", 42.5),
        ("numeric(10,2)", "numeric(12,3)", "42.50", "numeric(12,3)", 42.5),
        ("numeric(10)", "numeric(12,2)", "42", "numeric(12,2)", 42.0),
        ("integer", "bigint", "42", "bigint", 42),
        ("integer", "numeric(12,2)", "42", "numeric(12,2)", 42.0),
        ("integer", "numeric", "42", "numeric", 42),
        ("bigint", "numeric", "42", "numeric", 42),
    ],
)
def test_type_widening_allowed(
    tmp_path, old_type, new_type, seed, expected_type, expected_value
):
    db = fresh_db("t_type_ok")
    try:
        before = type_schema(old_type)
        after = type_schema(new_type)
        create_schema(before, db)
        psql(f"INSERT INTO t (id, v) VALUES (1, {seed})", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        assert column_type(db, "t", "v") == expected_type
        assert rows(db, "SELECT v FROM t") == [{"v": expected_value}]
    finally:
        drop_db(db)


@pytest.mark.parametrize(
    "old_type,new_type",
    [
        ("varchar", "varchar(120)"),
        ("numeric", "numeric(12,2)"),
        ("numeric(10,2)", "numeric(10,3)"),
        ("integer", "numeric(10,2)"),
        ("bigint", "numeric(18,0)"),
        ("varchar(160)", "varchar(120)"),
        ("numeric(12,2)", "numeric(10,2)"),
        ("bigint", "integer"),
    ],
)
def test_type_narrowing_refused(tmp_path, old_type, new_type):
    out = tmp_path / "out"
    proc = run_planner(type_schema(old_type), type_schema(new_type), out)
    assert proc.returncode != 0
    assert "narrow type" in (proc.stderr + proc.stdout).lower()
    assert not (out / "migration.sql").exists()
    assert not (out / "rollback.sql").exists()


@pytest.mark.parametrize(
    "bad_type",
    [
        "varchar(0)",
        "varchar(10,2)",
        "numeric(0)",
        "numeric(10,11)",
        "numeric(10,2,3)",
    ],
)
def test_invalid_type_writes_no_scripts(tmp_path, bad_type):
    out = tmp_path / "out"
    proc = run_planner(type_schema("text"), type_schema(bad_type), out)
    assert proc.returncode != 0
    assert "invalid column type" in (proc.stderr + proc.stdout).lower()
    assert not (out / "migration.sql").exists()
    assert not (out / "rollback.sql").exists()


def test_default_and_nullability_rollback(tmp_path):
    db = fresh_db("t_defaults")
    try:
        before = {
            "tables": [
                {
                    "name": "notes",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {
                            "name": "body",
                            "type": "text",
                            "nullable": False,
                            "default": "'draft'",
                        },
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        after = {
            "tables": [
                {
                    "name": "notes",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "body", "type": "text", "nullable": True},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        create_schema(before, db)
        psql("INSERT INTO notes (id, body) VALUES (1, 'kept')", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        assert column_defaults(db, "notes")["body"] is None
        assert columns(db, "notes")["body"][2] == "YES"
        assert rows(db, "SELECT id, body FROM notes") == [{"id": 1, "body": "kept"}]

        apply_sql(out / "rollback.sql", db)
        assert column_defaults(db, "notes")["body"] is not None
        assert columns(db, "notes")["body"][2] == "NO"
        assert rows(db, "SELECT id, body FROM notes") == [{"id": 1, "body": "kept"}]
    finally:
        drop_db(db)


def test_primary_key_change_and_rollback(tmp_path):
    db = fresh_db("t_pk")
    try:
        before = {
            "tables": [
                {
                    "name": "codes",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "code", "type": "text", "nullable": False},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        after = {
            "tables": [
                {
                    "name": "codes",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "code", "type": "text", "nullable": False},
                    ],
                    "primary_key": ["code"],
                }
            ]
        }
        create_schema(before, db)
        psql("INSERT INTO codes (id, code) VALUES (1, 'a'), (2, 'b')", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        assert primary_key_columns(db, "codes") == ["code"]
        assert rows(db, "SELECT id, code FROM codes ORDER BY id") == [
            {"id": 1, "code": "a"},
            {"id": 2, "code": "b"},
        ]

        apply_sql(out / "rollback.sql", db)
        assert primary_key_columns(db, "codes") == ["id"]
        assert rows(db, "SELECT id, code FROM codes ORDER BY id") == [
            {"id": 1, "code": "a"},
            {"id": 2, "code": "b"},
        ]
    finally:
        drop_db(db)


def test_table_index_and_default_changes_rollback(tmp_path):
    db = fresh_db("t_objects")
    try:
        before = {
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "email", "type": "varchar(120)", "nullable": False},
                        {
                            "name": "status",
                            "type": "text",
                            "nullable": False,
                            "default": "'active'",
                        },
                    ],
                    "primary_key": ["id"],
                    "indexes": [
                        {
                            "name": "accounts_email_key",
                            "columns": ["email"],
                            "unique": True,
                        }
                    ],
                },
                {
                    "name": "orders",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "account_id", "type": "integer", "nullable": False},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {
                            "name": "orders_account_fk",
                            "columns": ["account_id"],
                            "references": {"table": "accounts", "columns": ["id"]},
                            "on_delete": "CASCADE",
                        }
                    ],
                },
            ]
        }
        after = {
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "email", "type": "varchar(160)", "nullable": False},
                        {
                            "name": "status",
                            "type": "text",
                            "nullable": False,
                            "default": "'enabled'",
                        },
                        {"name": "display_name", "type": "text", "nullable": True},
                    ],
                    "primary_key": ["id"],
                    "indexes": [
                        {
                            "name": "accounts_status_idx",
                            "columns": ["status"],
                            "unique": False,
                        }
                    ],
                },
                {
                    "name": "orders",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "account_id", "type": "integer", "nullable": False},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {
                            "name": "orders_account_fk",
                            "columns": ["account_id"],
                            "references": {"table": "accounts", "columns": ["id"]},
                            "on_delete": "CASCADE",
                        }
                    ],
                    "indexes": [
                        {
                            "name": "orders_account_idx",
                            "columns": ["account_id"],
                            "unique": False,
                        }
                    ],
                },
                {
                    "name": "audit",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "account_id", "type": "integer", "nullable": False},
                        {"name": "note", "type": "text", "nullable": True},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {
                            "name": "audit_account_fk",
                            "columns": ["account_id"],
                            "references": {"table": "accounts", "columns": ["id"]},
                            "on_delete": "CASCADE",
                        }
                    ],
                    "indexes": [
                        {
                            "name": "audit_account_idx",
                            "columns": ["account_id"],
                            "unique": False,
                        }
                    ],
                },
            ]
        }
        create_schema(before, db)
        psql(
            "INSERT INTO accounts (id, email, status) VALUES "
            "(1, 'a@example.com', 'active')",
            db,
        )
        psql("INSERT INTO orders (id, account_id) VALUES (10, 1)", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        assert column_type(db, "accounts", "email") == "character varying(160)"
        assert "enabled" in column_defaults(db, "accounts")["status"]
        assert "display_name" in columns(db, "accounts")
        assert "accounts_status_idx" in index_names(db, "accounts")
        assert "accounts_email_key" not in index_names(db, "accounts")
        assert "orders_account_idx" in index_names(db, "orders")
        assert table_exists(db, "audit")
        assert "audit_account_idx" in index_names(db, "audit")
        assert foreign_key_count(db) == 2
        assert rows(
            db,
            "SELECT id, email, status, display_name FROM accounts",
        ) == [
            {
                "id": 1,
                "email": "a@example.com",
                "status": "active",
                "display_name": None,
            }
        ]
        assert rows(db, "SELECT id, account_id FROM orders") == [
            {"id": 10, "account_id": 1}
        ]

        apply_sql(out / "rollback.sql", db)
        assert column_type(db, "accounts", "email") == "character varying(120)"
        assert "active" in column_defaults(db, "accounts")["status"]
        assert "display_name" not in columns(db, "accounts")
        assert "accounts_email_key" in index_names(db, "accounts")
        assert "accounts_status_idx" not in index_names(db, "accounts")
        assert "orders_account_idx" not in index_names(db, "orders")
        assert not table_exists(db, "audit")
        assert foreign_key_count(db) == 1
        assert rows(db, "SELECT id, email, status FROM accounts") == [
            {"id": 1, "email": "a@example.com", "status": "active"}
        ]
        assert rows(db, "SELECT id, account_id FROM orders") == [
            {"id": 10, "account_id": 1}
        ]
    finally:
        drop_db(db)


def test_foreign_key_on_delete_change_rollback(tmp_path):
    db = fresh_db("t_fk_action")
    try:
        before = {
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                    ],
                    "primary_key": ["id"],
                },
                {
                    "name": "orders",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "account_id", "type": "integer", "nullable": False},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {
                            "name": "orders_account_fk",
                            "columns": ["account_id"],
                            "references": {"table": "accounts", "columns": ["id"]},
                            "on_delete": "CASCADE",
                        }
                    ],
                },
            ]
        }
        after = {
            "tables": [
                before["tables"][0],
                {
                    "name": "orders",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "account_id", "type": "integer", "nullable": False},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {
                            "name": "orders_account_fk",
                            "columns": ["account_id"],
                            "references": {"table": "accounts", "columns": ["id"]},
                            "on_delete": "RESTRICT",
                        }
                    ],
                },
            ]
        }
        create_schema(before, db)
        psql("INSERT INTO accounts (id) VALUES (1)", db)
        psql("INSERT INTO orders (id, account_id) VALUES (10, 1)", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        assert foreign_key_delete_action(db, "orders_account_fk") == "r"

        apply_sql(out / "rollback.sql", db)
        assert foreign_key_delete_action(db, "orders_account_fk") == "c"
    finally:
        drop_db(db)


def test_destructive_drop_column_rollback_restores_schema(tmp_path):
    db = fresh_db("t_drop_col_rb")
    try:
        before = {
            "tables": [
                {
                    "name": "items",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "legacy", "type": "text", "nullable": True},
                        {"name": "price", "type": "integer", "nullable": False},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        after = {
            "tables": [
                {
                    "name": "items",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "price", "type": "integer", "nullable": False},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        create_schema(before, db)
        psql("INSERT INTO items (id, legacy, price) VALUES (1, 'old', 10)", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out, allow=True)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        assert "legacy" not in columns(db, "items")
        assert rows(db, "SELECT id, price FROM items") == [{"id": 1, "price": 10}]

        apply_sql(out / "rollback.sql", db)
        assert "legacy" in columns(db, "items")
        assert rows(db, "SELECT id, legacy, price FROM items") == [
            {"id": 1, "legacy": None, "price": 10}
        ]
    finally:
        drop_db(db)


def test_destructive_drop_table_rollback_restores_schema(tmp_path):
    db = fresh_db("t_drop_tbl_rb")
    try:
        before = {
            "tables": [
                {
                    "name": "keep",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "value", "type": "text", "nullable": True},
                    ],
                    "primary_key": ["id"],
                },
                {
                    "name": "gone",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                    ],
                    "primary_key": ["id"],
                },
            ]
        }
        after = {"tables": [before["tables"][0]]}
        create_schema(before, db)
        psql("INSERT INTO keep (id, value) VALUES (1, 'kept')", db)
        psql("INSERT INTO gone (id) VALUES (2)", db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out, allow=True)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        assert not table_exists(db, "gone")
        assert rows(db, "SELECT id, value FROM keep") == [{"id": 1, "value": "kept"}]

        apply_sql(out / "rollback.sql", db)
        assert table_exists(db, "gone")
        assert rows(db, "SELECT id FROM gone") == []
        assert rows(db, "SELECT id, value FROM keep") == [{"id": 1, "value": "kept"}]
    finally:
        drop_db(db)


def test_multiple_destructive_refusals_are_reported(tmp_path):
    before = {
        "tables": [
            {
                "name": "keep",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "legacy", "type": "text", "nullable": True},
                ],
                "primary_key": ["id"],
            },
            {
                "name": "gone",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                ],
                "primary_key": ["id"],
            },
        ]
    }
    after = {
        "tables": [
            {
                "name": "keep",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                ],
                "primary_key": ["id"],
            }
        ]
    }
    out = tmp_path / "out"
    proc = run_planner(before, after, out)
    assert proc.returncode != 0
    message = (proc.stderr + proc.stdout).lower()
    assert 'drop table "gone"' in message
    assert 'drop column "keep"."legacy"' in message
    assert not (out / "migration.sql").exists()
    assert not (out / "rollback.sql").exists()


def test_quoted_identifier_rename_preserves_data(tmp_path):
    db = fresh_db("t_quoted")
    try:
        before = {
            "tables": [
                {
                    "name": "order",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "user name", "type": "text", "nullable": False},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        after = {
            "tables": [
                {
                    "name": "order",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False},
                        {"name": "full name", "type": "text", "nullable": False},
                    ],
                    "primary_key": ["id"],
                }
            ]
        }
        create_schema(before, db)
        psql('INSERT INTO "order" (id, "user name") VALUES (1, \'Ada\')', db)

        out = tmp_path / "out"
        proc = run_planner(before, after, out)
        assert proc.returncode == 0, proc.stderr
        apply_sql(out / "migration.sql", db)

        cols = columns(db, "order")
        assert "full name" in cols
        assert "user name" not in cols
        assert rows(db, 'SELECT id, "full name" FROM "order"') == [
            {"id": 1, "full name": "Ada"}
        ]

        apply_sql(out / "rollback.sql", db)
        cols = columns(db, "order")
        assert "user name" in cols
        assert "full name" not in cols
        assert rows(db, 'SELECT id, "user name" FROM "order"') == [
            {"id": 1, "user name": "Ada"}
        ]
    finally:
        drop_db(db)


def test_invalid_foreign_key_writes_no_scripts(tmp_path):
    before = basic_before()
    after = basic_after()
    after["tables"][0]["foreign_keys"] = [
        {
            "name": "users_missing_fk",
            "columns": ["missing"],
            "references": {"table": "users", "columns": ["id"]},
        }
    ]
    out = tmp_path / "out"
    proc = run_planner(before, after, out)
    assert proc.returncode != 0
    assert not (out / "migration.sql").exists()
    assert not (out / "rollback.sql").exists()


def test_invalid_input_writes_no_scripts(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    before_path = out / "before.json"
    after_path = out / "after.json"
    before_path.write_text("{ this is not json", encoding="utf-8")
    after_path.write_text(json.dumps(basic_after()), encoding="utf-8")
    proc = run(
        [
            sys.executable,
            str(PLANNER),
            "--before",
            str(before_path),
            "--after",
            str(after_path),
            "--out-dir",
            str(out),
        ]
    )
    assert proc.returncode != 0
    assert not (out / "migration.sql").exists()
    assert not (out / "rollback.sql").exists()
