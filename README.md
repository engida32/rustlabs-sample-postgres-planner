# PostgreSQL schema migration planner task

A FrontierSWE-style evaluation task. The agent gets two JSON descriptions of a PostgreSQL schema and must produce a forward migration and a matching rollback, graded against a real PostgreSQL server.

The point of the task is ordering and safety, not SQL typing. Foreign keys are created after every table exists so cycles are safe, constraints are dropped before the columns they protect, indexes come last, and destructive changes are refused unless `--allow-destructive` is passed.

## Task contents

| Path | What it is |
|---|---|
| `instruction.md` | The prompt given to the agent: CLI contract, schema format, required behaviour |
| `task.toml` | Task metadata: frontier difficulty, 2 CPUs, 4 GB, allowlisted network, 20 hour agent budget |
| `environment/` | Container definition plus the workspace the agent starts in, including a 16-line stub planner and example schemas |
| `solution/solve.sh` | Reference implementation, written to `/app/planner/plan.py` |
| `tests/test_planner.py` | 42 tests that run the planner and then apply both scripts to a live database |
| `tests/test.sh` | Verifier entry point: fresh cluster, run the suite, write `1` or `0` to the reward file |

## How grading works

`tests/test.sh` starts a throwaway PostgreSQL cluster on a unix socket, exports `PLANNER=/app/planner/plan.py`, runs pytest against `tests/test_planner.py`, and writes the reward. A test only passes if the generated migration applies cleanly, the row data survives where the change is not destructive, and the rollback restores the before schema.

## Reproduce the grade locally

Verified on 2026-11-19: 42 passed with the reference planner.

```bash
git clone https://github.com/engida32/rustlabs-sample-postgres-planner
cd rustlabs-sample-postgres-planner

python3 -m venv venv && ./venv/bin/pip install pytest

# pull the reference planner out of solve.sh
python3 - <<'PY'
import pathlib
body = pathlib.Path("solution/solve.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
pathlib.Path("plan.py").write_text(body + "\n")
PY

export PGDATA=$PWD/pgdata PGSOCK=$PWD/pgsock
mkdir -p "$PGDATA" "$PGSOCK"
initdb -D "$PGDATA" --no-locale -E UTF8
pg_ctl -D "$PGDATA" -o "-k $PGSOCK -p 55433 -c listen_addresses=''" -w start

PGHOST="$PGSOCK" PGPORT=55433 PGDATABASE=postgres PLANNER="$PWD/plan.py" \
  ./venv/bin/python -m pytest -q tests/test_planner.py

pg_ctl -D "$PGDATA" -m immediate stop
```

`initdb` as a normal user creates a superuser named after that user, so leave `PGUSER` unset. The verifier image runs everything as `postgres` and sets `PGUSER=postgres` instead.

## What the tests check

- Add and drop columns, with and without defaults.
- Type widening for integers, varchar lengths and numeric precision, and refusal to narrow.
- Column renames detected only when type, nullability, default and position match.
- Primary key, foreign key, index and `ON DELETE` changes.
- Table creation and removal, including cycles between foreign keys.
- Refusal of every destructive operation without `--allow-destructive`, with all refusals reported at once.
- Byte-identical output for identical input across runs.
- Rollback restores schema and keeps row data for non-destructive changes.

## License

MIT, see `LICENSE`.
