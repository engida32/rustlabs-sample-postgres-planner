#!/usr/bin/env bash
set -u

REWARD_DIR=/logs/verifier
mkdir -p "$REWARD_DIR"
printf '0\n' > "$REWARD_DIR/reward.txt"

PGDATA=/tmp/verifier-pgdata
PGSOCKET=/tmp/verifier-pgsocket
PGPORT=55433
PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
export PATH="$PGBIN:$PATH"

rm -rf "$PGDATA" "$PGSOCKET"
mkdir -p "$PGDATA" "$PGSOCKET"
chown -R postgres:postgres "$PGDATA" "$PGSOCKET"

cleanup() {
    runuser -u postgres -- "$PGBIN/pg_ctl" -D "$PGDATA" -m immediate stop >/tmp/pg-stop.log 2>&1 || true
}
trap cleanup EXIT

runuser -u postgres -- "$PGBIN/initdb" -D "$PGDATA" --no-locale -E UTF8 >/tmp/initdb.log 2>&1
if [ $? -ne 0 ]; then
    cat /tmp/initdb.log >&2
    exit 0
fi

runuser -u postgres -- "$PGBIN/pg_ctl" -D "$PGDATA" -o "-k $PGSOCKET -p $PGPORT -c listen_addresses=''" -w start >/tmp/pg-start.log 2>&1
if [ $? -ne 0 ]; then
    cat /tmp/pg-start.log >&2
    exit 0
fi

export PGHOST="$PGSOCKET"
export PGPORT="$PGPORT"
export PGUSER=postgres
export PGDATABASE=postgres
export PLANNER=/app/planner/plan.py

python3 -m pytest -q /tests/test_planner.py
rc=$?

if [ "$rc" -eq 0 ]; then
    printf '1\n' > "$REWARD_DIR/reward.txt"
else
    printf '0\n' > "$REWARD_DIR/reward.txt"
fi

exit 0
