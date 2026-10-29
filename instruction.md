# PostgreSQL schema migration planner

You are given two JSON descriptions of a PostgreSQL schema: `schema_before.json` and `schema_after.json`. Build a planner at `/app/planner/plan.py` that writes a forward migration and a rollback script.

## Command line

```text
python /app/planner/plan.py \
  --before <before.json> \
  --after <after.json> \
  --out-dir <directory> \
  [--allow-destructive]
```

The command must create `<directory>/migration.sql` and `<directory>/rollback.sql`. Exit 0 on success. On invalid input or a refused destructive change, exit non-zero, print a clear message to stderr, and do not create partial output files.

## Schema format

The top level is an object with a `tables` list. Each table has a `name`, a `columns` list, an optional `primary_key` list, an optional `foreign_keys` list, and an optional `indexes` list. Each column has `name`, `type`, `nullable`, and an optional `default`. A default is a PostgreSQL expression stored as a string, for example `0`, `false`, `now()`, or `'active'`.

Types use PostgreSQL names such as `integer`, `bigint`, `text`, `varchar(120)`, `numeric(10,2)`, `boolean`, `date`, `uuid`, `jsonb`, and `timestamp with time zone`.

Foreign keys have a `name`, a `columns` list, a `references` object with `table` and `columns`, and an optional `on_delete` value. Indexes have a `name`, a `columns` list, and a `unique` boolean.

## Required behavior

The forward migration must transform the before schema into the after schema while preserving existing rows where the change is not destructive. It must create new tables without inline foreign keys, then add foreign keys after all tables exist, so cycles are safe. It must drop foreign keys before dropping columns or tables. It must create indexes after their columns exist.

Support these changes:

- Add a column, with or without a default.
- Drop a column.
- Widen integer types, varchar lengths, and numeric precision without reducing the integer digits.
- Rename a column when the old and new columns have the same type, nullability, default, and position.
- Change nullability, defaults, primary keys, foreign keys, and indexes.
- Add and drop tables.

Treat these as destructive and refuse them unless `--allow-destructive` is present: dropping a table, dropping a column, narrowing a type, setting `NOT NULL` on a previously nullable column, and adding a `NOT NULL` column without a default. Report every refused operation in the error message.

The rollback script must reverse the forward migration. For non-destructive changes it must restore the before schema and preserve row data. For destructive changes it must restore the schema, but losing the dropped data is acceptable.

Output must be deterministic: the same inputs must produce byte-identical migration and rollback files. Quote all identifiers. Use `BEGIN;` and `COMMIT;` around each script. The planner must use only the Python standard library.
