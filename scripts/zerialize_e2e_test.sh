#!/usr/bin/env bash
# End-to-end test: Postgres rows -> pg_zerialize binary formats -> pgnats
# (NATS publish from SQL) -> nats_tool (NATS consume + zerialize decode) ->
# compared back against pg_zerialize's own decode of the same rows.
#
# Proves the full chain doesn't lose or corrupt anything for any of the
# seven pg_zerialize/zerialize wire formats, and that nats_asio's
# zerialize-based decoder agrees with pg_zerialize's own decoder for the
# same bytes.
#
# Prerequisites (all already true on this box as of when this was written,
# checked directly rather than assumed -- adjust the env vars below if not):
#   - A NATS server reachable at $NATS_HOST:$NATS_PORT (nats-server -js).
#   - The `pgnats` extension CREATE EXTENSION'd in $PGDATABASE, loaded via
#     shared_preload_libraries, with a foreign server (any name) pointing
#     at that NATS server -- see pgnats's README "Configuration" section.
#   - The `pg_zerialize` extension CREATE EXTENSION'd (any version; this
#     script runs `ALTER EXTENSION pg_zerialize UPDATE;` itself, idempotent
#     if already current) in the same database.
#   - nats_tool built at $NATS_TOOL (defaults to the sibling ~/nats_asio
#     checkout's release build).
#
# Usage: ./zerialize_e2e_test.sh [table_name]
#   table_name defaults to pgnats_zerialize_demo, created fresh by
#   zerialize_e2e_test.sql. Pass an existing table name to run against
#   that instead (skips the fixture-creation step) -- the table just needs
#   a column usable as a stable integer row identifier named `id`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$SCRIPT_DIR/out"

: "${PGDATABASE:=postgres}"
: "${NATS_TOOL:=$HOME/nats_asio/build/bin/nats_tool}"
: "${SUBJECT_PREFIX:=pgtest}"
: "${GRUB_TIMEOUT_SECS:=30}"
: "${CONNECT_WAIT_SECS:=10}"

TABLE_NAME="${1:-pgnats_zerialize_demo}"
FORMATS=(msgpack cbor zera flexbuffers ion bson beve)

if [[ ! -x "$NATS_TOOL" ]]; then
    echo "nats_tool not found/executable at $NATS_TOOL (build ~/nats_asio first, or set NATS_TOOL)" >&2
    exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

GRUB_PIDS=()
cleanup() {
    for pid in "${GRUB_PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

echo "== 1. Fixture setup =="
if [[ "$TABLE_NAME" == "pgnats_zerialize_demo" ]]; then
    psql -v ON_ERROR_STOP=1 -q -c "ALTER EXTENSION pg_zerialize UPDATE;"
    psql -v ON_ERROR_STOP=1 -q -f "$SCRIPT_DIR/zerialize_e2e_test.sql"
else
    psql -v ON_ERROR_STOP=1 -q -c "ALTER EXTENSION pg_zerialize UPDATE;"
    echo "Using existing table $TABLE_NAME (skipping fixture creation)"
fi

ROW_COUNT="$(psql -v ON_ERROR_STOP=1 -tAc "SELECT count(*) FROM $TABLE_NAME;")"
echo "Table $TABLE_NAME: $ROW_COUNT rows"

echo
echo "== 2. Reference values (pg_zerialize's own msgpack_to_jsonb decode) =="
psql -v ON_ERROR_STOP=1 -tAc "
    SELECT jsonb_build_object('row_id', id, 'expected', msgpack_to_jsonb(row_to_msgpack(t)))::text
    FROM $TABLE_NAME t
    ORDER BY id;
" > "$OUT_DIR/reference.jsonl"
echo "Wrote $(wc -l < "$OUT_DIR/reference.jsonl") reference rows to $OUT_DIR/reference.jsonl"

echo
echo "== 3. Starting $((${#FORMATS[@]})) nats_tool consumers =="
for fmt in "${FORMATS[@]}"; do
    timeout "${GRUB_TIMEOUT_SECS}s" "$NATS_TOOL" grub \
        --topic "${SUBJECT_PREFIX}.${fmt}.>" \
        --format "$fmt" \
        --json \
        --timestamp \
        --max_msgs "$ROW_COUNT" \
        --dump "$OUT_DIR/${fmt}.jsonl" \
        > "$OUT_DIR/${fmt}.log" 2>&1 &
    GRUB_PIDS+=("$!")
done

echo "Waiting up to ${CONNECT_WAIT_SECS}s for all consumers to subscribe..."
deadline=$((SECONDS + CONNECT_WAIT_SECS))
while true; do
    subscribed=$(grep -l "subscribed to" "$OUT_DIR"/*.log 2>/dev/null | wc -l || true)
    if [[ "$subscribed" -eq "${#FORMATS[@]}" ]]; then
        echo "All ${#FORMATS[@]} consumers subscribed."
        break
    fi
    if [[ "$SECONDS" -ge "$deadline" ]]; then
        echo "Only $subscribed/${#FORMATS[@]} consumers subscribed after ${CONNECT_WAIT_SECS}s -- proceeding anyway (some formats will likely show missing rows below)." >&2
        break
    fi
    sleep 0.2
done

echo
echo "== 4. Publishing $ROW_COUNT rows x ${#FORMATS[@]} formats via pgnats =="
for fmt in "${FORMATS[@]}"; do
    psql -v ON_ERROR_STOP=1 -q -c "
        SELECT nats_publish_binary('${SUBJECT_PREFIX}.${fmt}.' || id, row_to_${fmt}(t))
        FROM $TABLE_NAME t;
    " > /dev/null
    echo "  published: $fmt"
done

echo
echo "== 5. Waiting for consumers to finish =="
for pid in "${GRUB_PIDS[@]}"; do
    wait "$pid" || true
done
GRUB_PIDS=()

echo
echo "== 6. Comparing =="
compare_args=()
for fmt in "${FORMATS[@]}"; do
    compare_args+=("${fmt}=${OUT_DIR}/${fmt}.jsonl")
done
python3 "$SCRIPT_DIR/compare_e2e_results.py" "$OUT_DIR/reference.jsonl" "${compare_args[@]}"
