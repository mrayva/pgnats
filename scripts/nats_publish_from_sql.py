#!/usr/bin/env python3
"""Publishes the result of an arbitrary SQL query to NATS, one message per
row, encoded with a chosen pg_zerialize binary format, with the subject
built from the per-row values of chosen columns.

Unlike zerialize_e2e_test.sh/.sql (a fixed regression fixture wired into
CI, with deliberately crafted edge-case rows), this is a general-purpose
tool for ad hoc testing against any table/view/query: point it at a real
table, pick the columns that should form the subject, pick a format, and
it publishes every row.

Example (matching a real NYSE trades table with mixed-case columns):

    ./nats_publish_from_sql.py \\
        --sql 'SELECT * FROM nyse_eqy_us_all_trade_20260105' \\
        --subject-columns 'Exchange,Symbol' \\
        --format msgpack \\
        --limit 1000 \\
        --verify

That publishes each row's whole tuple, msgpack-encoded, to a subject built
from that row's own Exchange and Symbol values (e.g. "N.AAPL"), and with
--verify also proves nats_tool can receive and decode every one of them by
spinning up a consumer, cross-checking its decoded output against
pg_zerialize's own <format>_to_jsonb decode of the same rows (as an
unordered multiset -- subjects built from arbitrary, possibly non-unique
column values can't be used to correlate individual rows back to a source
identity in the general case, so content, not position, is what's
verified).

Requires: psycopg (v3) - already available in this environment. Connects
using standard libpq environment variables (PGHOST/PGPORT/PGUSER/
PGPASSWORD/PGDATABASE/...), same as psql; override with --dsn if needed.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter

import psycopg
from psycopg import sql

FORMATS = ("msgpack", "cbor", "zera", "flexbuffers", "ion", "bson", "beve")

# NATS subject tokens can't contain whitespace or the wildcard characters
# '*'/'>' - a column value containing one of those would silently corrupt
# the subject's token structure downstream (e.g. turn a value into an
# unintended wildcard subscription match). Caught here, at publish time,
# rather than discovered later as a confusing "wrong number of messages
# received" mismatch during --verify.
BAD_SUBJECT_TOKEN_RE = re.compile(r"[\s*>]")


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--sql", required=True,
                    help="Arbitrary SELECT statement producing the row set to publish "
                         "(e.g. 'SELECT * FROM my_table WHERE ...').")
    p.add_argument("--subject-columns", required=True,
                    help="Comma-separated column names (must be present in --sql's result). "
                         "Each row's subject is these columns' own values, dot-joined, "
                         "in the given order (e.g. 'Exchange,Symbol' -> subject 'N.AAPL').")
    p.add_argument("--format", required=True,
                    help=f"pg_zerialize binary format, case-insensitive. One of: {', '.join(FORMATS)}")
    p.add_argument("--subject-prefix", default="",
                    help="Literal token(s) prepended to every subject, e.g. 'trades' -> "
                         "'trades.N.AAPL'. Omit for no prefix.")
    p.add_argument("--limit", type=int, default=None,
                    help="Cap the number of rows published (applied to --sql's result). "
                         "Strongly recommended for large tables.")
    p.add_argument("--dsn", default="",
                    help="libpq connection string/URI. Default: read standard PG* env vars, same as psql.")
    p.add_argument("--verify", action="store_true",
                    help="After publishing, spin up a nats_tool consumer and cross-check its "
                         "decoded output against pg_zerialize's own decode of the same rows.")
    p.add_argument("--nats-tool", default=os.path.expanduser("~/nats_asio/build/bin/nats_tool"),
                    help="Path to nats_tool (used only with --verify).")
    p.add_argument("--nats-topic", default=None,
                    help="Override the --verify consumer's subscribe topic. Default: "
                         "<subject-prefix.><one '*' per subject column, dot-joined> "
                         "(e.g. 2 subject-columns, no prefix -> '*.*').")
    p.add_argument("--timeout-secs", type=int, default=60,
                    help="Max seconds to wait for the --verify consumer to finish (default: 60).")
    p.add_argument("--connect-wait-secs", type=int, default=15,
                    help="Max seconds to wait for the --verify consumer to confirm it "
                         "subscribed before publishing starts (default: 15).")
    p.add_argument("--keep-dump", action="store_true",
                    help="Don't delete the --verify consumer's dump file on exit; print its path.")
    return p.parse_args()


def validate_format(raw):
    fmt = raw.strip().lower()
    if fmt not in FORMATS:
        sys.exit(f"error: --format '{raw}' is not one of: {', '.join(FORMATS)}")
    return fmt


def validate_sql(raw_sql):
    stripped = raw_sql.strip().rstrip(";").strip()
    if not stripped:
        sys.exit("error: --sql is empty")
    head = stripped[:32].lstrip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        sys.exit("error: --sql must be a SELECT (or WITH ... SELECT) statement, "
                 f"got: {stripped[:40]!r}...")
    return stripped


def parse_subject_columns(raw):
    cols = [c.strip() for c in raw.split(",")]
    cols = [c for c in cols if c]
    if not cols:
        sys.exit("error: --subject-columns must name at least one column")
    return cols


def build_subject_expr(subject_columns, prefix):
    # COALESCE so a NULL column value produces the literal token "_NULL_"
    # in the subject instead of making the whole subject expression NULL
    # (concatenation with NULL is NULL in SQL, which nats_publish_binary
    # would reject outright rather than silently drop -- fail loud either
    # way, but this keeps the failure specific to genuinely bad data, not
    # every row that happens to have a NULL in a subject column).
    parts = [
        sql.SQL("COALESCE({}::text, '_NULL_')").format(sql.Identifier(c))
        for c in subject_columns
    ]
    expr = sql.SQL(" || '.' || ").join(parts)
    if prefix:
        expr = sql.SQL("{} || '.' || ").format(sql.Literal(prefix)) + expr
    return expr


def default_topic(subject_columns, prefix):
    wildcard = ".".join("*" for _ in subject_columns)
    return f"{prefix}.{wildcard}" if prefix else wildcard


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def start_consumer(nats_tool, topic, fmt, max_msgs, dump_path, log_file):
    cmd = [
        nats_tool, "grub",
        "--topic", topic,
        "--format", fmt,
        "--json",
        "--max_msgs", str(max_msgs),
        "--dump", dump_path,
    ]
    return subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)


def wait_for_subscribed(log_path, timeout_secs):
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        try:
            with open(log_path) as f:
                if "subscribed to" in f.read():
                    return True
        except FileNotFoundError:
            pass
        time.sleep(0.2)
    return False


def main():
    args = parse_args()
    fmt = validate_format(args.format)
    user_sql = validate_sql(args.sql)
    subject_columns = parse_subject_columns(args.subject_columns)

    if args.verify and not os.access(args.nats_tool, os.X_OK):
        sys.exit(f"error: --verify given but nats_tool not found/executable at {args.nats_tool} "
                 "(build ~/nats_asio first, or pass --nats-tool)")

    conn = psycopg.connect(args.dsn, autocommit=True)
    cur = conn.cursor()

    print("== 1. Materializing --sql result ==")
    limit_clause = sql.SQL(" LIMIT {}").format(sql.Literal(args.limit)) if args.limit else sql.SQL("")
    cur.execute(
        sql.SQL("CREATE TEMP TABLE _e2e_src AS {}{}").format(sql.SQL(user_sql), limit_clause)
    )
    cur.execute("SELECT count(*) FROM _e2e_src")
    row_count = cur.fetchone()[0]
    print(f"  {row_count} row(s)")
    if row_count == 0:
        sys.exit("error: --sql produced zero rows, nothing to publish")

    for col in subject_columns:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = '_e2e_src' AND column_name = %s",
            (col,),
        )
        if cur.fetchone() is None:
            sys.exit(f"error: subject column '{col}' is not present in --sql's result columns")

    # Postgres's ~ operator understands \s in ARE (advanced regex) mode, its
    # default, so the same pattern the Python side uses to describe this
    # check can be pushed server-side as a single bounded query instead of
    # pulling every subject-column value across to check them in Python.
    col_checks = sql.SQL(" OR ").join(
        sql.SQL("{}::text ~ {}").format(sql.Identifier(c), sql.Literal(BAD_SUBJECT_TOKEN_RE.pattern))
        for c in subject_columns
    )
    select_cols = sql.SQL(", ").join(sql.Identifier(c) for c in subject_columns)
    cur.execute(sql.SQL("SELECT {} FROM _e2e_src WHERE {} LIMIT 5").format(select_cols, col_checks))
    bad_rows = cur.fetchall()
    if bad_rows:
        sys.exit(f"error: subject column(s) {subject_columns} contain values with whitespace or "
                 f"NATS wildcard characters ('*'/'>'), which would corrupt the subject "
                 f"structure, e.g.: {bad_rows!r}")

    subject_expr = build_subject_expr(subject_columns, args.subject_prefix)

    reference = None
    if args.verify:
        print("== 2. Computing reference (pg_zerialize's own decode) ==")
        ref_query = sql.SQL("SELECT {}({}(_e2e_src)) FROM _e2e_src").format(
            sql.Identifier(f"{fmt}_to_jsonb"), sql.Identifier(f"row_to_{fmt}")
        )
        cur.execute(ref_query)
        reference = [canonical(row[0]) for row in cur.fetchall()]
        print(f"  {len(reference)} reference row(s)")

    dump_dir = tempfile.mkdtemp(prefix="nats_e2e_")
    dump_path = os.path.join(dump_dir, f"{fmt}.jsonl")
    log_path = os.path.join(dump_dir, f"{fmt}.log")
    consumer = None

    try:
        if args.verify:
            topic = args.nats_topic or default_topic(subject_columns, args.subject_prefix)
            print(f"== 3. Starting nats_tool consumer (topic={topic!r}) ==")
            with open(log_path, "w") as log_file:
                consumer = start_consumer(args.nats_tool, topic, fmt, row_count, dump_path, log_file)
            if not wait_for_subscribed(log_path, args.connect_wait_secs):
                print(f"  WARNING: consumer did not confirm subscription within "
                      f"{args.connect_wait_secs}s -- proceeding anyway", file=sys.stderr)
            else:
                print("  consumer subscribed")

        print(f"== {'4' if args.verify else '2'}. Publishing {row_count} row(s) as {fmt} ==")
        pub_query = sql.SQL("SELECT nats_publish_binary({}, {}(_e2e_src)) FROM _e2e_src").format(
            subject_expr, sql.Identifier(f"row_to_{fmt}")
        )
        try:
            cur.execute(pub_query)
        except psycopg.Error as e:
            sys.exit(f"error: publish failed: {e}")
        print(f"  published {row_count} message(s)")

        if not args.verify:
            return

        # grub mode has no self-exit on --max_msgs: it only auto-unsubscribes
        # after N messages (nats_asio.hpp's increment_and_check()) and then
        # keeps running - the only way to stop it is SIGINT/SIGTERM (see the
        # comment at nats_tool.cpp's signal_set setup). That's also what
        # triggers the dump file's flush-on-clean-exit (its ofstream is only
        # auto-flushed every 100 messages otherwise - see message_output.hpp's
        # dump_file_writer). So: give delivery a short settle window (local
        # NATS delivery to an already-subscribed consumer is sub-second),
        # then terminate() (SIGTERM, not kill()/SIGKILL) so that teardown -
        # and the flush it does - actually runs, then wait for the now-clean
        # exit.
        print("== 5. Waiting for delivery, then stopping the consumer ==")
        time.sleep(min(args.timeout_secs, 3))
        consumer.terminate()
        try:
            consumer.wait(timeout=args.timeout_secs)
        except subprocess.TimeoutExpired:
            consumer.kill()
            consumer.wait(timeout=5)
            print(f"  WARNING: consumer did not exit cleanly within {args.timeout_secs}s "
                  "after SIGTERM (force-killed) -- its dump file may be incomplete", file=sys.stderr)

        received = []
        if os.path.exists(dump_path):
            with open(dump_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        received.append(canonical(json.loads(line)))

        print("== 6. Comparing ==")
        print(f"  reference: {len(reference)}, received: {len(received)}")

        exp_sorted = sorted(reference)
        act_sorted = sorted(received)
        if exp_sorted == act_sorted:
            print(f"  PASS: all {len(reference)} rows received and decoded correctly "
                  "(unordered content match)")
            sys.exit(0)

        # Multiset diff for a useful report even when counts match but content differs.
        exp_counts = Counter(reference)
        act_counts = Counter(received)
        missing = exp_counts - act_counts
        extra = act_counts - exp_counts
        print(f"  FAIL: {sum(missing.values())} row(s) missing/mismatched, "
              f"{sum(extra.values())} unexpected row(s) received")
        for i, s in enumerate(list(missing.elements())[:3]):
            print(f"    missing[{i}]: {s[:300]}")
        for i, s in enumerate(list(extra.elements())[:3]):
            print(f"    extra[{i}]:   {s[:300]}")
        sys.exit(1)
    finally:
        if consumer and consumer.poll() is None:
            consumer.kill()
        if args.keep_dump:
            print(f"(dump kept at {dump_dir})")
        else:
            shutil.rmtree(dump_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
