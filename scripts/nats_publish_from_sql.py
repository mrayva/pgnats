#!/usr/bin/env python3
"""Publishes the result of an arbitrary SQL query to NATS, one message per
row, encoded with a chosen pg_zerialize binary format, with the subject
built from the per-row values of chosen columns. Optionally does this for
every format in one run and prints a performance comparison across them.

Unlike zerialize_e2e_test.sh/.sql (a fixed regression fixture wired into
CI, with deliberately crafted edge-case rows), this is a general-purpose
tool for ad hoc testing against any table/view/query: point it at a real
table, pick the columns that should form the subject, pick one or more
formats, and it publishes every row.

Example (matching a real NYSE trades table with mixed-case columns),
benchmarking all 7 formats against the same query in one run:

    ./nats_publish_from_sql.py \\
        --sql 'SELECT * FROM nyse_eqy_us_all_trade_20260105' \\
        --subject-columns 'Exchange,Symbol' \\
        --format all \\
        --limit 100000 \\
        --verify

That publishes each row's whole tuple, encoded per format, to a subject
built from that row's own Exchange/Symbol values plus the format as the
last token (e.g. "N.AAPL.msgpack" - see --no-subject-format-suffix to
omit it), once per requested format. With --verify, each format's run
also proves nats_tool can receive and decode every one of its messages,
by spinning up a consumer and cross-checking its decoded output against
pg_zerialize's own <format>_to_jsonb decode of the same rows (as an
unordered multiset -- subjects built from arbitrary, possibly non-unique
column values can't be used to correlate individual rows back to a
source identity in the general case, so content, not position, is what's
verified) -- and timing how long that took, for a throughput comparison
across formats.

Streaming, not materializing: --sql is wrapped in a single query that
computes each row's payload once (row_to_<fmt>) and both publishes it and
(with --verify) projects its jsonb reference from that same evaluation -
nothing about --sql is copied into a temp table first, so this scales to
a query returning millions of rows without duplicating them server-side.
How that query is run differs by mode, since only --verify actually needs
row data back in Python:

  - Without --verify: one plain execute() wrapping the whole thing in
    SELECT count(*), sum/avg/min/max(payload size) FROM (...) - Postgres
    does the counting and the payload-size aggregation, this script never
    touches a row. Fast and simple, but no progress feedback while it
    runs, and a failure partway through can't report how many rows had
    already published by then (only that some number of the earlier ones
    did - NATS publish is not transactional, see below).
  - With --verify: fetched row-by-row via psycopg's cursor.stream()
    (libpq single-row mode - no real server-side DECLARE CURSOR, no
    held-open transaction), since the reference list has to end up in
    Python either way for the comparison. This is also what
    --progress-every reports against, and where payload-size stats are
    accumulated in Python instead of via a separate query.

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

STATS_LINE_RE = re.compile(r"Stats: (\d+) events/sec")


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
                    help="Comma-separated pg_zerialize binary format(s), case-insensitive, or "
                         f"'all' for every one of: {', '.join(FORMATS)}. More than one runs "
                         "each format in turn against the same --sql and prints a performance "
                         "comparison at the end.")
    p.add_argument("--subject-prefix", default="",
                    help="Literal token(s) prepended to every subject, e.g. 'trades' -> "
                         "'trades.N.AAPL'. Omit for no prefix.")
    p.add_argument("--subject-format-suffix", action=argparse.BooleanOptionalAction, default=True,
                    help="Append the format name as the last subject token, e.g. "
                         "'N.AAPL.msgpack' (default: on). Lets consumers subscribe by format "
                         "(e.g. '*.*.msgpack'), and is what keeps multiple formats run via "
                         "--format from colliding on the same subjects.")
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
                    help="After publishing, spin up a nats_tool consumer, cross-check its "
                         "decoded output against pg_zerialize's own decode of the same rows, "
                         "and time how long full delivery took.")
    p.add_argument("--nats-tool", default=os.path.expanduser("~/nats_asio/build/bin/nats_tool"),
                    help="Path to nats_tool (used only with --verify).")
    p.add_argument("--nats-topic", default=None,
                    help="Override the --verify consumer's subscribe topic (only meaningful "
                         "with a single --format). Default is derived from --subject-prefix/"
                         "--subject-columns/--subject-format-suffix per format.")
    p.add_argument("--timeout-secs", type=int, default=60,
                    help="Max seconds to wait for --verify delivery to complete/the consumer "
                         "to finish, per format (default: 60).")
    p.add_argument("--connect-wait-secs", type=int, default=15,
                    help="Max seconds to wait for the --verify consumer to confirm it "
                         "subscribed before publishing starts, per format (default: 15).")
    p.add_argument("--keep-dump", action="store_true",
                    help="Don't delete each --verify consumer's dump file on exit; print its path.")
    p.add_argument("--metrics-json", default=None,
                    help="Write the collected per-format metrics as JSON to this path.")
    return p.parse_args()


def parse_formats(raw):
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if not tokens:
        sys.exit("error: --format must name at least one format")
    if any(t == "all" for t in tokens):
        return list(FORMATS)
    seen = set()
    formats = []
    for t in tokens:
        if t not in FORMATS:
            sys.exit(f"error: --format '{t}' is not one of: {', '.join(FORMATS)} (or 'all')")
        if t not in seen:
            seen.add(t)
            formats.append(t)
    return formats


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


# JSON's number type doesn't distinguish int from float, but pg_zerialize's
# own <fmt>_to_jsonb() and zerialize's translate<JSON> (what nats_tool's
# --json decode uses) don't always agree on which one to render a
# whole-number double as - e.g. a `double precision` column holding exactly
# 36 comes back as the bare integer "36" from <fmt>_to_jsonb() but as "36.0"
# from nats_tool. Both correctly hold the same IEEE-754 value; only the
# JSON text differs. Comparing canonical strings without normalizing this
# first reports a false mismatch for every whole-number double column -
# confirmed directly against real NYSE trade data (Trade Price). 2**53 is
# the largest magnitude a float can represent every integer up to exactly,
# so this only folds int into float where doing so can't itself lose
# precision and manufacture a *false* match.
_MAX_EXACT_FLOAT_INT = 2**53


def _normalize_numbers(obj):
    if isinstance(obj, dict):
        return {k: _normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_numbers(v) for v in obj]
    if isinstance(obj, int) and not isinstance(obj, bool) and abs(obj) <= _MAX_EXACT_FLOAT_INT:
        return float(obj)
    return obj


def canonical(obj):
    return json.dumps(_normalize_numbers(obj), sort_keys=True, ensure_ascii=False)


def start_consumer(nats_tool, topic, fmt, dump_path, log_file):
    cmd = [
        nats_tool, "grub",
        "--topic", topic,
        "--format", fmt,
        "--json",
        "--stats_interval", "1",
        # grub mode never self-exits on message count anyway (only auto-
        # unsubscribes - see the shutdown comment below), and an exact
        # count isn't known ahead of time in this streaming design, so this
        # is just a large placeholder - BUT it has to be present and > 0:
        # mode_runners.hpp's on_connected only logs "subscribed to ..." at
        # all (what wait_for_subscribed() polls for) when max_msgs > 0.
        "--max_msgs", str(2**31 - 1),
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


def wait_for_received_count(log_path, expected, timeout_secs):
    """Polls the consumer's log for cumulative "Stats: N events/sec" lines
    (summed across intervals - each line is a per-interval delta, not a
    running total) until `expected` messages have been observed or
    `timeout_secs` elapses. Returns (received_so_far, elapsed_secs).

    This is what makes the --verify timing a real measurement instead of a
    fixed sleep: without it, a small dataset would always report roughly
    "N rows / whatever the fixed wait was", dominated by that wait rather
    than actual delivery time.
    """
    start = time.monotonic()
    deadline = start + timeout_secs
    seen_lines = 0
    total = 0
    while total < expected and time.monotonic() < deadline:
        try:
            with open(log_path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        for line in lines[seen_lines:]:
            m = STATS_LINE_RE.search(line)
            if m:
                total += int(m.group(1))
        seen_lines = len(lines)
        if total >= expected:
            break
        time.sleep(0.15)
    return total, time.monotonic() - start


def build_publish_query(user_sql, limit, fmt, subject_expr, verify):
    # payload (row_to_<fmt>) is computed exactly once per row here and
    # reused below for the jsonb reference (--verify only), the payload
    # size, and the actual publish call, rather than invoking the encoder
    # more than once.
    limit_clause = sql.SQL(" LIMIT {}").format(sql.Literal(limit)) if limit else sql.SQL("")
    src = sql.SQL("SELECT *, {}(src) AS _e2e_payload FROM ({}) AS src{}").format(
        sql.Identifier(f"row_to_{fmt}"), sql.SQL(user_sql), limit_clause
    )
    cols = []
    if verify:
        cols.append(sql.SQL("{}(t._e2e_payload) AS _e2e_ref").format(sql.Identifier(f"{fmt}_to_jsonb")))
    cols.append(sql.SQL("octet_length(t._e2e_payload) AS _e2e_bytes"))
    cols.append(sql.SQL("nats_publish_binary({}, t._e2e_payload) AS _e2e_pub").format(subject_expr))
    select_list = sql.SQL(", ").join(cols)
    return sql.SQL("SELECT {} FROM ({}) AS t").format(select_list, src)


def build_count_query(publish_query):
    # No row data is needed back for the plain-publish (no --verify) path -
    # just how many were published and the payload-size distribution - so
    # that path doesn't stream row-by-row at all: one statement, one round
    # trip, Postgres does the counting and the aggregation.
    return sql.SQL(
        "SELECT count(*), sum(_e2e_bytes), avg(_e2e_bytes), min(_e2e_bytes), max(_e2e_bytes) "
        "FROM ({}) AS pub"
    ).format(publish_query)


def fmt_secs(s):
    return f"{s:.3f}s" if s is not None else "-"


def fmt_rate(rows, secs):
    if not secs or secs <= 0:
        return "-"
    return f"{rows / secs:,.0f}/s"


def fmt_bytes(n):
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:,.0f}{unit}"
        n /= 1024
    return f"{n:,.0f}TB"


def run_one_format(conn, args, fmt, subject_columns):
    """Publishes --sql once, encoded as `fmt`, optionally verifying via
    nats_tool. Never raises for an expected failure mode (bad SQL, bad
    subject data, verify mismatch, etc) - returns a dict with an "error"
    key set instead, so one format's failure doesn't stop a multi-format
    --format run from reporting the others.
    """
    metrics = {"format": fmt, "error": None}
    subject_expr = build_subject_expr(
        subject_columns, args.subject_prefix, fmt, args.subject_format_suffix
    )
    publish_query = build_publish_query(args.sql_stripped, args.limit, fmt, subject_expr, args.verify)

    dump_dir = tempfile.mkdtemp(prefix=f"nats_e2e_{fmt}_")
    dump_path = os.path.join(dump_dir, f"{fmt}.jsonl")
    log_path = os.path.join(dump_dir, f"{fmt}.log")
    consumer = None

    try:
        if args.verify:
            topic = args.nats_topic or default_topic(
                subject_columns, args.subject_prefix, fmt, args.subject_format_suffix
            )
            print(f"== [{fmt}] 1. Starting nats_tool consumer (topic={topic!r}) ==")
            with open(log_path, "w") as log_file:
                consumer = start_consumer(args.nats_tool, topic, fmt, dump_path, log_file)
            if not wait_for_subscribed(log_path, args.connect_wait_secs):
                print(f"  WARNING: consumer did not confirm subscription within "
                      f"{args.connect_wait_secs}s -- proceeding anyway", file=sys.stderr)
            else:
                print("  consumer subscribed")

        reference = None
        published = 0
        total_bytes = min_bytes = max_bytes = None
        t0 = time.perf_counter()
        if args.verify:
            print(f"== [{fmt}] 2. Streaming --sql and publishing ==")
            reference = []
            total_bytes = 0
            try:
                with conn.cursor() as cur:
                    for ref, nbytes, _pub in cur.stream(publish_query):
                        reference.append(canonical(ref))
                        total_bytes += nbytes
                        min_bytes = nbytes if min_bytes is None else min(min_bytes, nbytes)
                        max_bytes = nbytes if max_bytes is None else max(max_bytes, nbytes)
                        published += 1
                        if args.progress_every and published % args.progress_every == 0:
                            print(f"  ...{published} published so far")
            except psycopg.Error as e:
                metrics["error"] = f"publish failed after {published} row(s): {e}"
                return metrics
        else:
            print(f"== [{fmt}] 1. Publishing ==")
            try:
                with conn.cursor() as cur:
                    cur.execute(build_count_query(publish_query))
                    published, total_bytes, _avg_bytes, min_bytes, max_bytes = cur.fetchone()
            except psycopg.Error as e:
                metrics["error"] = f"publish failed: {e}"
                return metrics
        publish_secs = time.perf_counter() - t0
        print(f"  published {published} message(s) in {fmt_secs(publish_secs)} "
              f"({fmt_rate(published, publish_secs)}, {fmt_bytes(total_bytes)} total)")

        metrics.update(
            rows=published, publish_secs=publish_secs,
            total_bytes=total_bytes,
            avg_bytes=(total_bytes / published) if published else None,
            min_bytes=min_bytes, max_bytes=max_bytes,
        )

        if published == 0:
            metrics["error"] = "--sql produced zero rows, nothing was published"
            return metrics

        if not args.verify:
            return metrics

        print(f"== [{fmt}] 3. Waiting for delivery ==")
        received_by_stats, receive_secs = wait_for_received_count(
            log_path, published, args.timeout_secs
        )
        metrics["receive_secs"] = receive_secs
        if received_by_stats < published:
            print(f"  WARNING: only observed {received_by_stats}/{published} via consumer "
                  f"stats within {args.timeout_secs}s -- proceeding to compare anyway",
                  file=sys.stderr)
        else:
            print(f"  all {published} observed received in {fmt_secs(receive_secs)} "
                  f"({fmt_rate(published, receive_secs)})")

        # grub mode has no self-exit: it only auto-unsubscribes after
        # --max_msgs (not used here) and otherwise keeps running - the only
        # way to stop it is SIGINT/SIGTERM (see the comment at
        # nats_tool.cpp's signal_set setup). That's also what triggers the
        # dump file's flush-on-clean-exit (its ofstream is only
        # auto-flushed every 100 messages otherwise - see
        # message_output.hpp's dump_file_writer), so terminate() (SIGTERM,
        # not kill()/SIGKILL) rather than just killing it once satisfied.
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

        print(f"== [{fmt}] 4. Comparing ==")
        print(f"  reference: {len(reference)}, received: {len(received)}")

        exp_sorted = sorted(reference)
        act_sorted = sorted(received)
        if exp_sorted == act_sorted:
            print(f"  PASS: all {len(reference)} rows received and decoded correctly "
                  "(unordered content match)")
            metrics["verify_result"] = "PASS"
            return metrics

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
        metrics["verify_result"] = "FAIL"
        metrics["error"] = f"{sum(missing.values())} missing, {sum(extra.values())} unexpected"
        return metrics
    finally:
        if consumer and consumer.poll() is None:
            consumer.kill()
        if args.keep_dump:
            print(f"  (dump kept at {dump_dir})")
        else:
            shutil.rmtree(dump_dir, ignore_errors=True)


def print_comparison(results, verify):
    print("\n== Format comparison ==")
    headers = ["format", "rows", "avg bytes", "publish", "publish rate"]
    if verify:
        headers += ["receive", "receive rate", "verify"]
    rows = []
    for r in results:
        if r.get("error") and r.get("rows") is None:
            rows.append([r["format"], "ERROR: " + r["error"]])
            continue
        row = [
            r["format"],
            str(r["rows"]),
            fmt_bytes(r["avg_bytes"]),
            fmt_secs(r["publish_secs"]),
            fmt_rate(r["rows"], r["publish_secs"]),
        ]
        if verify:
            row += [
                fmt_secs(r.get("receive_secs")),
                fmt_rate(r["rows"], r.get("receive_secs")),
                r.get("verify_result") or ("ERROR" if r.get("error") else "-"),
            ]
        rows.append(row)

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    def fmt_row(cells):
        return "  ".join(c.ljust(widths[i]) if i < len(widths) else c for i, c in enumerate(cells))
    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in widths]))
    for row in rows:
        print(fmt_row(row))


def main():
    args = parse_args()
    formats = parse_formats(args.format)
    args.sql_stripped = validate_sql(args.sql)
    subject_columns = parse_subject_columns(args.subject_columns)

    if args.verify and not os.access(args.nats_tool, os.X_OK):
        sys.exit(f"error: --verify given but nats_tool not found/executable at {args.nats_tool} "
                 "(build ~/nats_asio first, or pass --nats-tool)")
    if args.nats_topic and len(formats) > 1:
        sys.exit("error: --nats-topic can't be used with more than one --format "
                 "(each format needs its own topic to avoid collisions)")

    conn = psycopg.connect(args.dsn, autocommit=True)

    results = []
    any_error = False
    for i, fmt in enumerate(formats):
        if len(formats) > 1:
            print(f"\n### format {i + 1}/{len(formats)}: {fmt} ###")
        r = run_one_format(conn, args, fmt, subject_columns)
        results.append(r)
        if r.get("error"):
            any_error = True
            print(f"  ERROR: {r['error']}", file=sys.stderr)

    if len(formats) > 1:
        print_comparison(results, args.verify)

    if args.metrics_json:
        with open(args.metrics_json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nMetrics written to {args.metrics_json}")

    sys.exit(1 if any_error else 0)


if __name__ == "__main__":
    main()
