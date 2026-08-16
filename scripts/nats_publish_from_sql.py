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
from that row's own Exchange/Symbol values plus the format as the last
token (e.g. "N.AAPL.msgpack" - see --no-subject-format-suffix to omit
it), and with --verify also proves nats_tool can receive and decode every
one of them, by spinning up a consumer and cross-checking its decoded
output against pg_zerialize's own <format>_to_jsonb decode of the same
rows (as an unordered multiset -- subjects built from arbitrary, possibly
non-unique column values can't be used to correlate individual rows back
to a source identity in the general case, so content, not position, is
what's verified).

Streaming, not materializing: --sql is wrapped in a single query that
computes each row's payload once (row_to_<fmt>) and both publishes it and
(with --verify) projects its jsonb reference from that same evaluation -
nothing about --sql is copied into a temp table first, so this scales to
a query returning millions of rows without duplicating them server-side.
How that query is run differs by mode, since only --verify actually needs
row data back in Python:

  - Without --verify: one plain execute() wrapping the whole thing in
    SELECT count(*) FROM (...) - Postgres does the counting, this script
    never touches a row. Fast and simple, but no progress feedback while
    it runs, and a failure partway through can't report how many rows had
    already published by then (only that some number of the earlier ones
    did - NATS publish is not transactional, see below).
  - With --verify: fetched row-by-row via psycopg's cursor.stream()
    (libpq single-row mode - no real server-side DECLARE CURSOR, no
    held-open transaction), since the reference list has to end up in
    Python either way for the comparison. This is also what
    --progress-every reports against.

The tradeoff either way: a bad subject-column value is only caught at the
row it occurs on (see "safety" in build_subject_expr()'s docstring below)
- rows the server already evaluated by then have already been published,
since NATS publish side effects aren't transactional/rollback-able. If
you need a hard guarantee that nothing gets published unless the whole
result set is clean, filter/validate the source data before pointing
this at it.

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
# unintended wildcard subscription match). Postgres's ~ operator
# understands \s in ARE (advanced regex) mode, its default, so this same
# pattern is pushed server-side as part of the per-row subject expression
# itself (see build_subject_expr()) rather than checked in Python.
BAD_SUBJECT_TOKEN_PATTERN = r"[\s*>]"


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
    p.add_argument("--subject-format-suffix", action=argparse.BooleanOptionalAction, default=True,
                    help="Append the format name as the last subject token, e.g. "
                         "'N.AAPL.msgpack' (default: on). Lets consumers subscribe by format "
                         "(e.g. '*.*.msgpack') when the same subject-columns are published in "
                         "more than one format across separate runs, without them colliding.")
    p.add_argument("--limit", type=int, default=None,
                    help="Cap the number of rows published (applied to --sql's result). "
                         "Strongly recommended for large tables.")
    p.add_argument("--dsn", default="",
                    help="libpq connection string/URI. Default: read standard PG* env vars, same as psql.")
    p.add_argument("--progress-every", type=int, default=10000,
                    help="Print a running count every N published rows, 0 to disable (default: "
                         "10000). Only applies with --verify: without it, publishing is a "
                         "single statement with no row-by-row visibility into progress.")
    p.add_argument("--verify", action="store_true",
                    help="After publishing, spin up a nats_tool consumer and cross-check its "
                         "decoded output against pg_zerialize's own decode of the same rows.")
    p.add_argument("--nats-tool", default=os.path.expanduser("~/nats_asio/build/bin/nats_tool"),
                    help="Path to nats_tool (used only with --verify).")
    p.add_argument("--nats-topic", default=None,
                    help="Override the --verify consumer's subscribe topic. Default is derived "
                         "from --subject-prefix/--subject-columns/--subject-format-suffix "
                         "(e.g. 2 subject-columns, no prefix, format suffix on -> '*.*.msgpack').")
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


def build_subject_expr(subject_columns, prefix, fmt, format_suffix):
    """Builds the per-row subject expression.

    Safety: each column's value is checked in-line against
    BAD_SUBJECT_TOKEN_PATTERN and, on a match, deliberately casts an
    error-describing string to integer - not a real conversion, just the
    simplest way to make Postgres raise a real, readable ERROR (aborting
    the query at that row) without a helper function or a separate
    validation pass. See the module docstring for what that means for
    rows the server already streamed past by the time it hits a bad one.
    """
    parts = []
    for col in subject_columns:
        ident = sql.Identifier(col)
        parts.append(
            sql.SQL(
                "CASE WHEN {ident} IS NULL THEN '_NULL_' "
                "WHEN {ident}::text ~ {pattern} "
                "THEN ((('bad subject value for column ' || {name} || ': ' || {ident}::text))::integer)::text "
                "ELSE {ident}::text END"
            ).format(
                ident=ident,
                pattern=sql.Literal(BAD_SUBJECT_TOKEN_PATTERN),
                name=sql.Literal(col),
            )
        )
    if format_suffix:
        parts.append(sql.Literal(fmt))
    expr = sql.SQL(" || '.' || ").join(parts)
    if prefix:
        expr = sql.SQL("{} || '.' || ").format(sql.Literal(prefix)) + expr
    return expr


def default_topic(subject_columns, prefix, fmt, format_suffix):
    tokens = ["*" for _ in subject_columns]
    if format_suffix:
        tokens.append(fmt)
    topic = ".".join(tokens)
    return f"{prefix}.{topic}" if prefix else topic


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


def build_publish_query(user_sql, limit, fmt, subject_expr, verify):
    # payload (row_to_<fmt>) is computed exactly once per row here and
    # reused below for both the jsonb reference (--verify only) and the
    # actual publish call, rather than invoking the encoder twice.
    limit_clause = sql.SQL(" LIMIT {}").format(sql.Literal(limit)) if limit else sql.SQL("")
    src = sql.SQL("SELECT *, {}(src) AS _e2e_payload FROM ({}) AS src{}").format(
        sql.Identifier(f"row_to_{fmt}"), sql.SQL(user_sql), limit_clause
    )
    publish_col = sql.SQL("nats_publish_binary({}, t._e2e_payload)").format(subject_expr)
    if verify:
        select_list = sql.SQL("{}(t._e2e_payload), {}").format(
            sql.Identifier(f"{fmt}_to_jsonb"), publish_col
        )
    else:
        select_list = publish_col
    return sql.SQL("SELECT {} FROM ({}) AS t").format(select_list, src)


def build_count_query(publish_query):
    # No row data is needed back for the plain-publish (no --verify) path -
    # just how many were published - so that path doesn't stream row-by-row
    # at all: one statement, one round trip, Postgres does the counting.
    return sql.SQL("SELECT count(*) FROM ({}) AS pub").format(publish_query)


def main():
    args = parse_args()
    fmt = validate_format(args.format)
    user_sql = validate_sql(args.sql)
    subject_columns = parse_subject_columns(args.subject_columns)

    if args.verify and not os.access(args.nats_tool, os.X_OK):
        sys.exit(f"error: --verify given but nats_tool not found/executable at {args.nats_tool} "
                 "(build ~/nats_asio first, or pass --nats-tool)")

    subject_expr = build_subject_expr(
        subject_columns, args.subject_prefix, fmt, args.subject_format_suffix
    )
    publish_query = build_publish_query(user_sql, args.limit, fmt, subject_expr, args.verify)

    conn = psycopg.connect(args.dsn, autocommit=True)

    dump_dir = tempfile.mkdtemp(prefix="nats_e2e_")
    dump_path = os.path.join(dump_dir, f"{fmt}.jsonl")
    log_path = os.path.join(dump_dir, f"{fmt}.log")
    consumer = None

    try:
        if args.verify:
            topic = args.nats_topic or default_topic(
                subject_columns, args.subject_prefix, fmt, args.subject_format_suffix
            )
            print(f"== 1. Starting nats_tool consumer (topic={topic!r}) ==")
            # Row count isn't known ahead of time in a streaming design, so
            # the consumer can't be told an exact --max_msgs; give it an
            # effectively-unbounded one and stop it explicitly once the
            # publish side is done (see below).
            with open(log_path, "w") as log_file:
                consumer = start_consumer(args.nats_tool, topic, fmt, 2**31 - 1, dump_path, log_file)
            if not wait_for_subscribed(log_path, args.connect_wait_secs):
                print(f"  WARNING: consumer did not confirm subscription within "
                      f"{args.connect_wait_secs}s -- proceeding anyway", file=sys.stderr)
            else:
                print("  consumer subscribed")

        reference = None
        published = 0
        if args.verify:
            # Row data is needed back (for the jsonb reference), so this
            # path streams row-by-row via cursor.stream() - also what gives
            # --progress-every something to report incrementally on.
            print(f"== 2. Streaming --sql and publishing as {fmt} ==")
            reference = []
            try:
                with conn.cursor() as cur:
                    for row in cur.stream(publish_query):
                        reference.append(canonical(row[0]))
                        published += 1
                        if args.progress_every and published % args.progress_every == 0:
                            print(f"  ...{published} published so far")
            except psycopg.Error as e:
                sys.exit(f"error: publish failed after {published} row(s): {e}")
        else:
            # No row data is needed back at all here - just a count - so
            # this is one statement, one round trip, no row-by-row client
            # iteration: Postgres does the counting, not this script.
            print(f"== 1. Publishing as {fmt} ==")
            try:
                with conn.cursor() as cur:
                    cur.execute(build_count_query(publish_query))
                    published = cur.fetchone()[0]
            except psycopg.Error as e:
                sys.exit(f"error: publish failed: {e}")
        print(f"  published {published} message(s)")

        if published == 0:
            sys.exit("error: --sql produced zero rows, nothing was published")

        if not args.verify:
            return

        # grub mode has no self-exit: it only auto-unsubscribes after
        # --max_msgs (nats_asio.hpp's increment_and_check()) and then keeps
        # running - the only way to stop it is SIGINT/SIGTERM (see the
        # comment at nats_tool.cpp's signal_set setup). That's also what
        # triggers the dump file's flush-on-clean-exit (its ofstream is
        # only auto-flushed every 100 messages otherwise - see
        # message_output.hpp's dump_file_writer). So: give delivery a
        # short settle window (local NATS delivery to an already-
        # subscribed consumer is sub-second), then terminate() (SIGTERM,
        # not kill()/SIGKILL) so that teardown - and the flush it does -
        # actually runs, then wait for the now-clean exit.
        print("== 3. Waiting for delivery, then stopping the consumer ==")
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

        print("== 4. Comparing ==")
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
