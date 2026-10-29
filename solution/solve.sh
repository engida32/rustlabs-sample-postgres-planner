#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/planner
cat > /app/planner/plan.py <<'PY'
#!/usr/bin/env python3
"""Reference PostgreSQL schema migration planner."""

import argparse
import json
import re
import sys

TYPE_ALIASES = {
    "int": "integer",
    "int4": "integer",
    "serial": "integer",
    "int8": "bigint",
    "bigserial": "bigint",
    "int2": "smallint",
    "smallserial": "smallint",
    "varchar": "character varying",
    "char": "character",
    "bool": "boolean",
    "float4": "real",
    "float8": "double precision",
    "timestamptz": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
}

TYPE_RE = re.compile(r"^[a-z_]+(?: [a-z_]+)*(?:\([0-9, ]+\))?(?:\[\])?$")
WIDEN = {
    "smallint": ["integer", "bigint", "numeric"],
    "integer": ["bigint", "numeric"],
    "bigint": ["numeric"],
    "real": ["double precision", "numeric"],
    "double precision": ["numeric"],
    "character varying": ["text"],
    "character": ["text"],
    "date": ["timestamp without time zone", "timestamp with time zone"],
    "timestamp without time zone": ["timestamp with time zone"],
}


class PlanError(Exception):
    pass


def fail(message):
    raise PlanError(message)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def norm_type(value):
    if not isinstance(value, str):
        fail("column type must be a string")
    text = value.strip().lower()
    match = re.fullmatch(r"([a-z_ ]+?)(?:\(([^)]*)\))?(\[\])?", text)
    if not match:
        fail("invalid column type: %r" % value)
    base = " ".join(match.group(1).split())
    base = TYPE_ALIASES.get(base, base)
    args = match.group(2)
    suffix = match.group(3) or ""
    if args is not None:
        args = ",".join(part.strip() for part in args.split(","))
        parts = args.split(",")
        if base in {"character varying", "character"}:
            if len(parts) != 1 or not parts[0].isdigit() or int(parts[0]) <= 0:
                fail("invalid column type: %r" % value)
        elif base == "numeric":
            if len(parts) not in {1, 2} or not all(part.isdigit() for part in parts):
                fail("invalid column type: %r" % value)
            precision = int(parts[0])
            scale = int(parts[1]) if len(parts) == 2 else 0
            if precision <= 0 or scale < 0 or scale > precision:
                fail("invalid column type: %r" % value)
        result = "%s(%s)%s" % (base, args, suffix)
    else:
        result = base + suffix
    if not TYPE_RE.fullmatch(result):
        fail("invalid column type: %r" % value)
    return result


def norm_default(value):
    if value is None:
        return None
    if not isinstance(value, str):
        fail("column default must be a string or null")
    text = value.strip()
    if not text:
        return None
    if ";" in text or "--" in text or "/*" in text:
        fail("column default contains disallowed punctuation")
    return text


def norm_ident(value, what):
    if not isinstance(value, str) or not value or "\x00" in value:
        fail("invalid %s: %r" % (what, value))
    if len(value.encode("utf-8")) > 63:
        fail("%s is longer than 63 bytes: %r" % (what, value))
    return value


def as_list(value, what):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        fail("%s must be a list of strings" % what)
    return value


def load_schema(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        fail("cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict) or not isinstance(data.get("tables"), list):
        fail("schema must be an object with a tables list")
    tables = {}
    for raw_table in data["tables"]:
        if not isinstance(raw_table, dict):
            fail("each table must be an object")
        name = norm_ident(raw_table.get("name"), "table name")
        if name in tables:
            fail("duplicate table: %s" % name)
        raw_columns = raw_table.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            fail("table %s must have at least one column" % name)
        columns = []
        seen = set()
        for raw_column in raw_columns:
            if not isinstance(raw_column, dict):
                fail("each column must be an object")
            column_name = norm_ident(raw_column.get("name"), "column name")
            if column_name in seen:
                fail("duplicate column %s.%s" % (name, column_name))
            seen.add(column_name)
            nullable = raw_column.get("nullable", True)
            if not isinstance(nullable, bool):
                fail("nullable must be a boolean for %s.%s" % (name, column_name))
            columns.append({
                "name": column_name,
                "type": norm_type(raw_column.get("type")),
                "nullable": nullable,
                "default": norm_default(raw_column.get("default")),
            })
        primary_key = raw_table.get("primary_key")
        if primary_key is not None:
            primary_key = as_list(primary_key, "primary_key")
            if not primary_key:
                fail("primary_key must not be empty for %s" % name)
            for column_name in primary_key:
                if column_name not in seen:
                    fail("primary key column %s.%s does not exist" % (name, column_name))
        foreign_keys = []
        fk_names = set()
        for raw_fk in raw_table.get("foreign_keys", []):
            if not isinstance(raw_fk, dict):
                fail("each foreign key must be an object")
            fk_name = norm_ident(raw_fk.get("name"), "foreign key name")
            if fk_name in fk_names:
                fail("duplicate foreign key %s.%s" % (name, fk_name))
            fk_names.add(fk_name)
            fk_columns = as_list(raw_fk.get("columns"), "foreign key columns")
            if not fk_columns:
                fail("foreign key %s.%s has no columns" % (name, fk_name))
            for column_name in fk_columns:
                if column_name not in seen:
                    fail("foreign key column %s.%s does not exist" % (name, column_name))
            refs = raw_fk.get("references")
            if not isinstance(refs, dict):
                fail("foreign key %s.%s has no references object" % (name, fk_name))
            ref_table = norm_ident(refs.get("table"), "referenced table name")
            ref_columns = as_list(refs.get("columns"), "referenced columns")
            if not ref_columns:
                fail("foreign key %s.%s has no referenced columns" % (name, fk_name))
            on_delete = raw_fk.get("on_delete")
            if on_delete is not None:
                on_delete = str(on_delete).strip().upper()
                if on_delete not in {"CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"}:
                    fail("invalid on_delete for %s.%s" % (name, fk_name))
            foreign_keys.append({
                "name": fk_name,
                "columns": fk_columns,
                "ref_table": ref_table,
                "ref_columns": ref_columns,
                "on_delete": on_delete,
            })
        indexes = []
        index_names = set()
        for raw_index in raw_table.get("indexes", []):
            if not isinstance(raw_index, dict):
                fail("each index must be an object")
            index_name = norm_ident(raw_index.get("name"), "index name")
            if index_name in index_names:
                fail("duplicate index %s.%s" % (name, index_name))
            index_names.add(index_name)
            index_columns = as_list(raw_index.get("columns"), "index columns")
            if not index_columns:
                fail("index %s.%s has no columns" % (name, index_name))
            for column_name in index_columns:
                if column_name not in seen:
                    fail("index column %s.%s does not exist" % (name, column_name))
            unique = raw_index.get("unique", False)
            if not isinstance(unique, bool):
                fail("index unique must be a boolean for %s.%s" % (name, index_name))
            indexes.append({"name": index_name, "columns": index_columns, "unique": unique})
        tables[name] = {
            "name": name,
            "columns": columns,
            "primary_key": primary_key,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
        }
    for table in tables.values():
        column_names = {column["name"] for column in table["columns"]}
        for fk in table["foreign_keys"]:
            if fk["ref_table"] not in tables:
                fail("foreign key %s.%s references missing table %s" % (table["name"], fk["name"], fk["ref_table"]))
            ref_column_names = {column["name"] for column in tables[fk["ref_table"]]["columns"]}
            for column_name in fk["ref_columns"]:
                if column_name not in ref_column_names:
                    fail("foreign key %s.%s references missing column %s.%s" % (
                        table["name"], fk["name"], fk["ref_table"], column_name))
            for column_name in fk["columns"]:
                if column_name not in column_names:
                    fail("foreign key %s.%s uses missing column %s" % (table["name"], fk["name"], column_name))
    return tables


def type_parts(type_name):
    match = re.fullmatch(r"([a-z_ ]+?)(?:\(([^)]*)\))?(\[\])?", type_name)
    if not match:
        return type_name, None, ""
    return match.group(1).strip(), match.group(2), match.group(3) or ""


def int_args(args):
    return [int(part.strip()) for part in args.split(",")]


def is_widening(old_type, new_type):
    old_base, old_args, old_suffix = type_parts(old_type)
    new_base, new_args, new_suffix = type_parts(new_type)
    if old_suffix != new_suffix:
        return False
    if old_base == new_base:
        if old_base in {"character varying", "character"}:
            old_len = None
            if old_args is not None:
                old_len = int_args(old_args)[0]
            elif old_base == "character":
                old_len = 1
            new_len = None
            if new_args is not None:
                new_len = int_args(new_args)[0]
            elif new_base == "character":
                new_len = 1
            if old_len is None:
                return new_len is None
            if new_len is None:
                return True
            return new_len >= old_len
        if old_base == "numeric":
            if old_args is None:
                return new_args is None
            if new_args is None:
                return True
            old_nums = int_args(old_args)
            new_nums = int_args(new_args)
            old_precision = old_nums[0]
            old_scale = old_nums[1] if len(old_nums) == 2 else 0
            new_precision = new_nums[0]
            new_scale = new_nums[1] if len(new_nums) == 2 else 0
            return (
                new_scale >= old_scale
                and (new_precision - new_scale) >= (old_precision - old_scale)
            )
        return False
    if new_base not in WIDEN.get(old_base, []):
        return False
    if new_base == "numeric" and new_args is not None:
        old_digits = {"smallint": 5, "integer": 10, "bigint": 19}.get(old_base)
        if old_digits is None:
            return False
        new_nums = int_args(new_args)
        precision = new_nums[0]
        scale = new_nums[1] if len(new_nums) == 2 else 0
        return precision - scale >= old_digits
    return True


def column_signature(column):
    return (column["type"], column["nullable"], column["default"])


def index_key(index):
    return (index["name"], tuple(index["columns"]), index["unique"])


def fk_key(fk):
    return (
        fk["name"],
        tuple(fk["columns"]),
        fk["ref_table"],
        tuple(fk["ref_columns"]),
        fk["on_delete"],
    )


def build_body(before, after, allow_destructive, rollback=False):
    statements = []
    refusals = []
    common_tables = sorted(set(before) & set(after))
    dropped_tables = sorted(set(before) - set(after))
    new_tables = sorted(set(after) - set(before))

    # Drop every foreign key first. This makes table, column, and primary-key
    # changes independent of dependency order, including cycles.
    for table_name in sorted(before):
        for fk in sorted(before[table_name]["foreign_keys"], key=lambda item: item["name"]):
            statements.append(
                "ALTER TABLE %s DROP CONSTRAINT %s" % (quote(table_name), quote(fk["name"]))
            )

    # Drop managed indexes before columns change. Indexes on dropped tables
    # disappear with the table.
    for table_name in common_tables:
        for index in sorted(before[table_name]["indexes"], key=lambda item: item["name"]):
            statements.append("DROP INDEX %s" % quote(index["name"]))

    # Drop primary keys that are removed or changed.
    for table_name in common_tables:
        old_pk = before[table_name]["primary_key"]
        new_pk = after[table_name]["primary_key"]
        if old_pk is not None and old_pk != new_pk:
            statements.append(
                "ALTER TABLE %s DROP CONSTRAINT %s" % (quote(table_name), quote(table_name + "_pkey"))
            )

    for table_name in dropped_tables:
        if allow_destructive or rollback:
            statements.append("DROP TABLE %s" % quote(table_name))
        else:
            refusals.append('drop table "%s"' % table_name)

    for table_name in common_tables:
        old_columns = {column["name"]: column for column in before[table_name]["columns"]}
        new_columns = {column["name"]: column for column in after[table_name]["columns"]}
        old_order = [column["name"] for column in before[table_name]["columns"]]
        new_order = [column["name"] for column in after[table_name]["columns"]]
        old_position = {name: index for index, name in enumerate(old_order)}
        new_position = {name: index for index, name in enumerate(new_order)}
        removed = [name for name in old_order if name not in new_columns]
        added = [name for name in new_order if name not in old_columns]
        renames = {}
        used_old = set()
        used_new = set()
        for old_name in removed:
            for new_name in added:
                if old_name in used_old or new_name in used_new:
                    continue
                old_column = old_columns[old_name]
                new_column = new_columns[new_name]
                if old_position[old_name] == new_position[new_name] and column_signature(old_column) == column_signature(new_column):
                    renames[old_name] = new_name
                    used_old.add(old_name)
                    used_new.add(new_name)
                    break
        for old_name in removed:
            if old_name not in renames:
                if allow_destructive or rollback:
                    statements.append(
                        "ALTER TABLE %s DROP COLUMN %s" % (quote(table_name), quote(old_name))
                    )
                else:
                    refusals.append('drop column "%s"."%s"' % (table_name, old_name))
        for old_name, new_name in sorted(renames.items()):
            statements.append(
                "ALTER TABLE %s RENAME COLUMN %s TO %s" % (
                    quote(table_name), quote(old_name), quote(new_name))
            )
        for new_name in added:
            if new_name in used_new:
                continue
            column = new_columns[new_name]
            default = column["default"]
            if not column["nullable"] and default is None and not (allow_destructive or rollback):
                refusals.append('add NOT NULL column "%s"."%s" without default' % (table_name, new_name))
                continue
            pieces = ["ALTER TABLE %s ADD COLUMN %s %s" % (
                quote(table_name), quote(new_name), column["type"])]
            if default is not None:
                pieces.append("DEFAULT %s" % default)
            if not column["nullable"]:
                pieces.append("NOT NULL")
            statements.append(" ".join(pieces))
        for new_name in new_order:
            if new_name not in old_columns and new_name not in used_new:
                continue
            old_name = next((name for name, target in renames.items() if target == new_name), new_name)
            old_column = old_columns.get(old_name)
            new_column = new_columns[new_name]
            if old_column is None:
                continue
            if old_column is not None:
                if old_column["type"] != new_column["type"]:
                    if is_widening(old_column["type"], new_column["type"]) or allow_destructive or rollback:
                        statements.append(
                            "ALTER TABLE %s ALTER COLUMN %s TYPE %s USING %s::%s" % (
                                quote(table_name), quote(new_name), new_column["type"],
                                quote(new_name), new_column["type"]))
                    else:
                        refusals.append('narrow type "%s"."%s" from %s to %s' % (
                            table_name, new_name, old_column["type"], new_column["type"]))
                if old_column["default"] != new_column["default"]:
                    if new_column["default"] is None:
                        statements.append("ALTER TABLE %s ALTER COLUMN %s DROP DEFAULT" % (
                            quote(table_name), quote(new_name)))
                    else:
                        statements.append("ALTER TABLE %s ALTER COLUMN %s SET DEFAULT %s" % (
                            quote(table_name), quote(new_name), new_column["default"]))
                if old_column["nullable"] and not new_column["nullable"]:
                    if allow_destructive or rollback:
                        if new_column["default"] is not None:
                            statements.append(
                                "UPDATE %s SET %s = %s WHERE %s IS NULL" % (
                                    quote(table_name), quote(new_name), new_column["default"], quote(new_name)))
                        statements.append("ALTER TABLE %s ALTER COLUMN %s SET NOT NULL" % (
                            quote(table_name), quote(new_name)))
                    else:
                        refusals.append('set NOT NULL on "%s"."%s"' % (table_name, new_name))
                elif not old_column["nullable"] and new_column["nullable"]:
                    statements.append("ALTER TABLE %s ALTER COLUMN %s DROP NOT NULL" % (
                        quote(table_name), quote(new_name)))

    for table_name in common_tables:
        new_pk = after[table_name]["primary_key"]
        old_pk = before[table_name]["primary_key"]
        if new_pk is not None and old_pk != new_pk:
            statements.append(
                "ALTER TABLE %s ADD CONSTRAINT %s PRIMARY KEY (%s)" % (
                    quote(table_name), quote(table_name + "_pkey"),
                    ", ".join(quote(column) for column in new_pk)))

    for table_name in new_tables:
        table = after[table_name]
        pieces = []
        for column in table["columns"]:
            piece = "%s %s" % (quote(column["name"]), column["type"])
            if column["default"] is not None:
                piece += " DEFAULT %s" % column["default"]
            if not column["nullable"]:
                piece += " NOT NULL"
            pieces.append(piece)
        if table["primary_key"] is not None:
            pieces.append("CONSTRAINT %s PRIMARY KEY (%s)" % (
                quote(table_name + "_pkey"),
                ", ".join(quote(column) for column in table["primary_key"])))
        statements.append("CREATE TABLE %s (%s)" % (quote(table_name), ", ".join(pieces)))

    for table_name in sorted(after):
        for fk in sorted(after[table_name]["foreign_keys"], key=lambda item: item["name"]):
            sql = "ALTER TABLE %s ADD CONSTRAINT %s FOREIGN KEY (%s) REFERENCES %s (%s)" % (
                quote(table_name),
                quote(fk["name"]),
                ", ".join(quote(column) for column in fk["columns"]),
                quote(fk["ref_table"]),
                ", ".join(quote(column) for column in fk["ref_columns"]),
            )
            if fk["on_delete"] is not None:
                sql += " ON DELETE %s" % fk["on_delete"]
            statements.append(sql)

    for table_name in sorted(after):
        for index in sorted(after[table_name]["indexes"], key=lambda item: item["name"]):
            statements.append(
                "CREATE %sINDEX %s ON %s (%s)" % (
                    "UNIQUE " if index["unique"] else "",
                    quote(index["name"]),
                    quote(table_name),
                    ", ".join(quote(column) for column in index["columns"]),
                )
            )

    return statements, refusals


def write_script(path, statements):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("BEGIN;\n")
        for statement in statements:
            handle.write(statement.rstrip(";") + ";\n")
        handle.write("COMMIT;\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--allow-destructive", action="store_true")
    args = parser.parse_args()
    try:
        before = load_schema(args.before)
        after = load_schema(args.after)
        forward, refusals = build_body(before, after, args.allow_destructive)
        if refusals and not args.allow_destructive:
            for refusal in sorted(refusals):
                print("refusing destructive change: %s" % refusal, file=sys.stderr)
            return 2
        rollback, _ = build_body(after, before, True, rollback=True)
        import os
        os.makedirs(args.out_dir, exist_ok=True)
        write_script(os.path.join(args.out_dir, "migration.sql"), forward)
        write_script(os.path.join(args.out_dir, "rollback.sql"), rollback)
        return 0
    except PlanError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
PY

chmod +x /app/planner/plan.py
