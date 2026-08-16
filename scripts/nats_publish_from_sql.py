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

A subject-column value containing whitespace, '.', or a NATS wildcard
character ('*'/'>') would otherwise corrupt the subject's token structure
(see build_subject_expr()'s docstring). By default (--on-bad-subject-value
normalize) those characters are collapsed to '_' and every row publishes -
real-world data commonly has this (NYSE symbols like "AAM WS", tickers
like "BRK.A"), and only the subject is affected, never the payload. Pass
--on-bad-subject-value reject for the opposite: abort the whole run at the
first bad value instead - but note that's only caught at the row it occurs
on, so rows the server already evaluated by then have already been
published, since NATS publish side effects aren't transactional/
rollback-able. If you need a hard guarantee that nothing gets published
unless the whole result set is clean, filter/validate the source data
before pointing this at it either way.

--batch-size N (opt-in, default 1 = off) groups up to N rows *within each
subject* into one columnar NATS message - {"col1":[v,v,...],"col2":[v,v,...]}
instead of a row object. Two ways to build that batch document, chosen with
--batch-encoding:

  - "native" (default): pg_zerialize's rows_to_<fmt>_columnar(anyarray) -
    deforms each row's tuple directly in C++ and writes columns straight
    into the target format, no jsonb intermediate. Requires pg_zerialize
    >= 1.12.
  - "jsonb": jsonb_build_object()+jsonb_agg() then <fmt>_from_jsonb() - the
    original implementation, kept for comparison. Column alignment across
    the batch (col1[i] and col2[i] belonging to the same source row) is
    guaranteed by giving every column's jsonb_agg() the same explicit
    ORDER BY - see build_batched_publish_query()'s docstring for why
    that's load-bearing, not cosmetic.

Where this helps and where it doesn't - measured, not assumed, and the
honest result cuts differently than a first look at the encoder-level cost
suggests: profiling zerialize's writers directly (no SQL/jsonb involved,
see the sibling investigation this tool grew out of) showed formats with a
fixed per-document cost - Ion's mandatory local symbol table, FlexBuffers'
key/value deduplication - lose that overhead almost entirely once
amortized across a batch (Ion: ~5x smaller, ~2-8x faster in that isolated
test). Real batching through this tool's original jsonb-based SQL path
told a different story: --batch-size 100 against 200k real rows cut
payload size 2.2-4.6x across every format (confirmed real, not estimated),
but *reduced* publish throughput for every format except Ion, which came
out roughly breakeven - the window function + GROUP BY +
jsonb_build_object()/jsonb_agg() machinery needed to actually build a
batch in SQL had real, substantial cost of its own, and for formats with
less fixed per-document overhead to amortize away (msgpack, cbor, beve
especially), that construction cost outweighed the encoder-level saving.

--batch-encoding native (the default) closes that gap: pg_zerialize's
native columnar encoder does the same column-array construction in C++,
via heap_deform_tuple, with no jsonb round-trip at all. Measured against
the same 200k real rows, batch=100: every format got *faster*, not just
smaller - msgpack 1.8x, ion 4.9x (2.2x vs the old jsonb-batched path),
bson 1.5x, zera 2.0x, beve 2.0x, flexbuffers 2.4x. Pass --batch-encoding
jsonb to reproduce the old (size-win-only) behavior for comparison.

--workers N (opt-in, default 1 = off) tests connection-level parallelism
alongside per-message encoding cost: splits --sql's rows across N
independent Postgres connections, each publishing its own physical
page-range share concurrently. Without --verify, each worker uses the
same one-round-trip server-side aggregation build_count_query()/
build_batched_count_query() already use for the single-connection path -
no row data shipped back to Python - since an earlier version of this
path streamed every row back for row-by-row Python-side counting even
without --verify, which is GIL-bound across worker threads and was a real
(now-fixed) cause of its scaling falling off: confirmed directly via a
controlled isolation test (server-side count-only vs Python-side row
iteration, same query, same worker counts) that this GIL bottleneck
explained most of an earlier gap. Also chased down a second real
bottleneck one layer down: pgnats's per-backend NATS connection runs on
its own thread_local Tokio runtime, which defaulted to one OS worker
thread per CPU core (25 threads/backend on a 24-core box) - with many
concurrent backends that's real oversubscription, and was measurably
causing aggregate throughput to *decline* past ~24 workers. Fixed
upstream in pgnats (src/ctx.rs, worker_threads(2) instead of the
per-core default) - see that commit for why new_current_thread() (the
seemingly obvious fix) was tried first and made things ~9x worse, not
better.

Real scaling, not just theoretical - measured against real NYSE rows,
msgpack: unbatched, 2M rows: 1 worker 670k rows/s, 2 workers 967k/s,
4 workers 1.47M/s, 8 workers 2.15M/s, 16 workers 2.46M/s, continuing
(post-fix) up to ~3.0M/s around 64 workers before plateauing - the
individual-row publish() round trip is the limit here, not encoding or
NATS. Combined with --batch-size (native encoding, 500), 30M rows: the
real unlock - up to ~7.3M rows/s (90 workers), since batching collapses
the number of individual publish() calls (and their per-call latency)
by ~500x while parallelism still scales the reduced call count across
connections. For scale: raw `nats bench pub` (no Postgres involved) on
this same server measured a single client at 4.56M msgs/sec (340B
messages) and 16 clients aggregating 9.1M msgs/sec / 6.2 GiB/sec at
~25KB messages (matching this pipeline's actual batched payload size) -
even the best combined result here (--workers 90 --batch-size 500) only
uses roughly 484MB/s of that, meaning NATS itself is nowhere near
saturated; Postgres-side per-call/per-row cost is still the real ceiling.
Combinable with --batch-size (each worker batches its own share the
same way the single-connection path would) and --verify (one shared
nats_tool consumer per format; each worker streams and contributes its
own slice of the decoded reference list, merged before comparing).

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
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql

FORMATS = ("msgpack", "cbor", "zera", "flexbuffers", "ion", "bson", "beve")

# A NATS subject token can't contain whitespace or the wildcard characters
# '*'/'>' (they'd change what the token means to a subscriber), and '.'
# specifically can't appear in a *column value* used to build a subject
# because '.' is this tool's own token separator - a value containing one
# silently splits into an extra token, corrupting the subject's structure
# with no error at all. Real-world data hits both: NYSE trade data has
# symbols with embedded spaces (warrant/rights tickers like "AAM WS") and,
# separately, class-of-share tickers with embedded dots (e.g. "BRK.A") are
# common enough elsewhere to matter. Postgres's ~ operator understands \s
# in ARE (advanced regex) mode, its default, so this same pattern is
# pushed server-side as part of the per-row subject expression itself (see
# build_subject_expr()) rather than checked in Python.
BAD_SUBJECT_TOKEN_PATTERN = r"[\s.*>]"

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
    p.add_argument("--on-bad-subject-value", choices=("normalize", "reject"), default="normalize",
                    help="How to handle a subject-column value containing whitespace, '.', or a "
                         "NATS wildcard character ('*'/'>'). 'normalize' (default) collapses runs "
                         "of them to a single '_' and publishes every row - real-world data "
                         "commonly has this (e.g. NYSE symbols like 'AAM WS', or class-of-share "
                         "tickers like 'BRK.A'); the payload is never affected, only the subject. "
                         "'reject' aborts the whole run at the first bad value instead (see the "
                         "module docstring for what that means for rows already published by "
                         "then).")
    p.add_argument("--limit", type=int, default=None,
                    help="Cap the number of rows published (applied to --sql's result). "
                         "Strongly recommended for large tables.")
    p.add_argument("--batch-size", type=int, default=1,
                    help="Group up to N rows into a single columnar NATS message instead of "
                         "one message per row (opt-in, default: 1 = off). Rows are batched "
                         "*within* each subject (partitioned by --subject-columns) so a batch "
                         "never mixes rows that would otherwise get different subjects - a "
                         "batch's message is {\"col1\":[v,v,...], \"col2\":[v,v,...], ...} "
                         "rather than a row object, built via jsonb_build_object()+jsonb_agg() "
                         "then <fmt>_from_jsonb(). Measured (see module docstring for numbers): "
                         "cuts payload size 2-5x across every format (see --batch-encoding for "
                         "whether it's also a throughput win).")
    p.add_argument("--batch-encoding", choices=("native", "jsonb"), default="native",
                    help="Only meaningful with --batch-size > 1. 'native' (default) uses "
                         "pg_zerialize's rows_to_<fmt>_columnar(anyarray) - deforms each row's "
                         "tuple directly in C++, no jsonb intermediate - measured as a real "
                         "throughput win (1.5-4.9x faster), not just a smaller-payload one. "
                         "'jsonb' uses the original jsonb_build_object()+jsonb_agg()+"
                         "<fmt>_from_jsonb() construction, kept for comparison - measured as a "
                         "payload-size win only (2.2-4.6x smaller) but a publish-throughput "
                         "*loss* for every format except Ion, since the GROUP BY/jsonb "
                         "construction cost outweighs the encoder-level saving. Requires "
                         "pg_zerialize >= 1.12 for 'native'.")
    p.add_argument("--workers", type=int, default=1,
                    help="Split --sql's rows across N parallel SQL connections instead of one "
                         "(opt-in, default: 1 = off), each publishing its own share "
                         "concurrently - tests whether publish throughput scales with "
                         "connection-level parallelism, not just per-message encoding cost. "
                         "Requires materializing --sql's rows into a throwaway UNLOGGED table "
                         "first (dropped when the run ends) - --sql has no guaranteed row order "
                         "on its own, so partitioning it directly across independent connections "
                         "isn't safe. Rows are then split into N roughly-equal, non-overlapping "
                         "physical page (ctid) ranges, one per worker. Combinable with "
                         "--batch-size (each worker batches its own share) and --verify (one "
                         "shared consumer per format; each worker's decoded reference rows are "
                         "merged before comparing).")
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


def build_subject_expr(subject_columns, prefix, fmt, format_suffix, on_bad_value, composite_column=None):
    """Builds the per-row subject expression.

    `composite_column`, when given, addresses each subject column as a
    field of that composite column (e.g. "(_e2e_row).\"Exchange\"")
    instead of a bare top-level column reference - used by the --workers
    parallel-publish path, whose per-worker query only has the whole
    original row available as a single composite column, not as
    individually-projected columns (see materialize_partitioned()).

    Safety, one of two modes (--on-bad-subject-value):

    - "normalize" (default): runs of whitespace/'.'/wildcard characters in
      a column value are collapsed to a single '_' (regexp_replace with
      the 'g' flag) before being used as a subject token. Lossy for the
      subject only - the published payload is always the row's real,
      untouched data - but means real-world data (e.g. NYSE symbols like
      "AAM WS" with an embedded space, or class-of-share tickers like
      "BRK.A") doesn't abort the whole run. Rare, accepted tradeoff: two
      distinct values that normalize to the same token (e.g. "AAM WS" and
      "AAM_WS", if both existed) become indistinguishable *by subject* -
      their payloads on NATS remain correct and distinct either way.
    - "reject": each value is checked in-line against
      BAD_SUBJECT_TOKEN_PATTERN and, on a match, deliberately casts an
      error-describing string to integer - not a real conversion, just the
      simplest way to make Postgres raise a real, readable ERROR (aborting
      the query at that row) without a helper function or a separate
      validation pass. See the module docstring for what that means for
      rows the server already streamed past by the time it hits a bad one.
    """
    parts = []
    for col in subject_columns:
        if composite_column:
            ident = sql.SQL("({}).{}").format(sql.Identifier(composite_column), sql.Identifier(col))
        else:
            ident = sql.Identifier(col)
        if on_bad_value == "normalize":
            parts.append(
                sql.SQL(
                    "CASE WHEN {ident} IS NULL THEN '_NULL_' "
                    "ELSE regexp_replace({ident}::text, {pattern}, '_', 'g') END"
                ).format(ident=ident, pattern=sql.Literal(BAD_SUBJECT_TOKEN_PATTERN + "+"))
            )
        else:
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


def introspect_columns(conn, user_sql):
    # --batch-size needs the full column list up front to build a
    # jsonb_build_object(col1, jsonb_agg(col1), col2, jsonb_agg(col2), ...)
    # expression - LIMIT 0 evaluates no rows, just gets column names back
    # via cursor.description. --limit isn't relevant here (and can't be
    # combined with a second LIMIT 0 in one query) - this only needs
    # column names, not a correctly-capped row count.
    query = sql.SQL("SELECT * FROM ({}) AS t0 LIMIT 0").format(sql.SQL(user_sql))
    with conn.cursor() as cur:
        cur.execute(query)
        return [d.name for d in cur.description]


def build_batched_publish_query(user_sql, limit, fmt, subject_columns, all_columns, subject_expr, batch_size, verify):
    """Groups up to `batch_size` rows *within each subject* (partitioned by
    subject_columns, so a batch never spans rows that would get different
    subjects) into one columnar document:
    {"col1": [v, v, ...], "col2": [v, v, ...], ...} instead of a row object -
    see build_subject_expr()'s and the module docstring's notes on why, and
    the numbers behind it.

    Column alignment matters here: jsonb_agg(col1 ORDER BY _e2e_seq) and
    jsonb_agg(col2 ORDER BY _e2e_seq) are two independent aggregate calls in
    the same GROUP BY, and Postgres does not otherwise guarantee they see
    rows in the same order - without an explicit, *identical* ORDER BY on
    every column's aggregate, col1[i] and col2[i] could silently end up
    belonging to two different source rows. _e2e_seq (a stable per-subject
    row_number()) is that shared order key.
    """
    # LIMIT must apply strictly before the window function below, not after
    # it (window functions are computed before LIMIT in SQL's logical
    # processing order) - an outer "... FROM (<user_sql>) AS src LIMIT n"
    # would instead run row_number() OVER the *entire*, unlimited --sql
    # result first and only take the first n of that, which for a large
    # source table means a full sort of every row before LIMIT ever gets a
    # chance to matter. Confirmed directly: this was a real, catastrophic
    # bug during testing (row_number() OVER a 115M-row table instead of a
    # 20,000-row --limit), not just a theoretical concern - fixed by
    # nesting the LIMIT one level deeper than build_publish_query's
    # (unbatched) same-looking pattern needs, since that path has no window
    # function forcing full materialization to begin with.
    limited = sql.SQL("SELECT * FROM ({}) AS src0{}").format(
        sql.SQL(user_sql), sql.SQL(" LIMIT {}").format(sql.Literal(limit)) if limit else sql.SQL("")
    )
    subject_col_list = sql.SQL(", ").join(sql.Identifier(c) for c in subject_columns)

    numbered = sql.SQL(
        "SELECT *, row_number() OVER (PARTITION BY {}) AS _e2e_seq FROM ({}) AS src"
    ).format(subject_col_list, limited)

    batched = sql.SQL(
        "SELECT *, ((_e2e_seq - 1) / {}) AS _e2e_batch_id FROM ({}) AS numbered"
    ).format(sql.Literal(batch_size), numbered)

    agg_pairs = [
        sql.SQL("{}, jsonb_agg({} ORDER BY _e2e_seq)").format(sql.Literal(col), sql.Identifier(col))
        for col in all_columns
    ]
    columnar_expr = sql.SQL("jsonb_build_object({})").format(sql.SQL(", ").join(agg_pairs))

    grouped = sql.SQL(
        "SELECT {subj}, count(*) AS _e2e_batch_rows, {from_jsonb}({columnar}) AS _e2e_payload "
        "FROM ({batched}) AS b GROUP BY {subj}, _e2e_batch_id"
    ).format(
        subj=subject_col_list,
        from_jsonb=sql.Identifier(f"{fmt}_from_jsonb"),
        columnar=columnar_expr,
        batched=batched,
    )

    cols = []
    if verify:
        cols.append(sql.SQL("{}(g._e2e_payload) AS _e2e_ref").format(sql.Identifier(f"{fmt}_to_jsonb")))
    cols.append(sql.SQL("octet_length(g._e2e_payload) AS _e2e_bytes"))
    cols.append(sql.SQL("g._e2e_batch_rows"))
    cols.append(sql.SQL("nats_publish_binary({}, g._e2e_payload) AS _e2e_pub").format(subject_expr))
    select_list = sql.SQL(", ").join(cols)
    return sql.SQL("SELECT {} FROM ({}) AS g").format(select_list, grouped)


def build_native_columnar_publish_query(user_sql, limit, fmt, subject_columns, subject_expr, batch_size, verify):
    """Same grouping/partitioning as build_batched_publish_query() (batches
    within each subject, never spanning rows that'd get different
    subjects), but builds the batch document via pg_zerialize's native
    rows_to_<fmt>_columnar(anyarray) instead of jsonb_build_object()+
    jsonb_agg()+<fmt>_from_jsonb() - no jsonb intermediate, no per-column
    aggregate calls, and (unlike the jsonb path) no need to know the
    column list up front, since it aggregates whole rows, not per-column
    jsonb_agg()s.

    Column alignment falls out for free here: array_agg(_e2e_row ORDER BY
    _e2e_seq) is a single aggregate over the whole row, not N independent
    per-column aggregates, so there's no possibility of two columns
    disagreeing on row order the way jsonb_agg(col1)/jsonb_agg(col2) could
    without matching explicit ORDER BYs.

    `src` (the FROM-item alias) is referenced bare in the SELECT list to
    get each row's whole-row composite value - valid whether the
    underlying FROM-item is a real table or an arbitrary derived subquery,
    exactly like `SELECT t FROM some_table t` yields t's whole-row value.
    """
    limited = sql.SQL("SELECT * FROM ({}) AS src0{}").format(
        sql.SQL(user_sql), sql.SQL(" LIMIT {}").format(sql.Literal(limit)) if limit else sql.SQL("")
    )
    subject_col_list = sql.SQL(", ").join(sql.Identifier(c) for c in subject_columns)

    numbered = sql.SQL(
        "SELECT *, src AS _e2e_row, row_number() OVER (PARTITION BY {}) AS _e2e_seq FROM ({}) AS src"
    ).format(subject_col_list, limited)

    batched = sql.SQL(
        "SELECT *, ((_e2e_seq - 1) / {}) AS _e2e_batch_id FROM ({}) AS numbered"
    ).format(sql.Literal(batch_size), numbered)

    grouped = sql.SQL(
        "SELECT {subj}, count(*) AS _e2e_batch_rows, "
        "{rows_to_fmt_columnar}(array_agg(_e2e_row ORDER BY _e2e_seq)) AS _e2e_payload "
        "FROM ({batched}) AS b GROUP BY {subj}, _e2e_batch_id"
    ).format(
        subj=subject_col_list,
        rows_to_fmt_columnar=sql.Identifier(f"rows_to_{fmt}_columnar"),
        batched=batched,
    )

    cols = []
    if verify:
        cols.append(sql.SQL("{}(g._e2e_payload) AS _e2e_ref").format(sql.Identifier(f"{fmt}_to_jsonb")))
    cols.append(sql.SQL("octet_length(g._e2e_payload) AS _e2e_bytes"))
    cols.append(sql.SQL("g._e2e_batch_rows"))
    cols.append(sql.SQL("nats_publish_binary({}, g._e2e_payload) AS _e2e_pub").format(subject_expr))
    select_list = sql.SQL(", ").join(cols)
    return sql.SQL("SELECT {} FROM ({}) AS g").format(select_list, grouped)


def build_batched_count_query(publish_query):
    # Same reasoning as build_count_query(), extended with sum(_e2e_batch_rows)
    # since count(*) here counts *batches* (messages), not rows.
    return sql.SQL(
        "SELECT count(*), sum(_e2e_batch_rows), sum(_e2e_bytes), min(_e2e_bytes), max(_e2e_bytes) "
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


def materialize_partitioned(conn, user_sql, limit, table_name):
    """Materializes --sql (capped at `limit` rows) into a throwaway UNLOGGED
    table, an exact 1:1 flat copy of --sql's own columns (same names,
    same types) - deliberately *not* wrapped into a single composite
    column: CREATE TABLE AS can't store a column of pseudo-type "record"
    (confirmed directly - "column has pseudo-type record" - since record
    has no fixed on-disk representation outside one query's execution),
    which a bare `src AS whole_row` projection produces whenever src is a
    derived subquery rather than a real table scan.

    Needed at all (rather than partitioning --sql directly) so --workers'
    independently-opened connections can each claim a disjoint, gap-free,
    overlap-free slice of the same row set: --sql has no guaranteed row
    order on its own (the same issue build_native_columnar_publish_query's
    docstring notes for LIMIT), so two separate executions of the same
    unordered query aren't guaranteed to agree on which rows go where.
    Partitioning is done by physical ctid page range afterwards (see
    ctid_page_bounds()) rather than an injected row-id column, so the
    materialized table's columns exactly match --sql's, with nothing to
    strip back out before serializing. UNLOGGED (skips WAL) since this is
    a disposable benchmark artifact, dropped by the caller when the run
    ends.
    """
    limit_clause = sql.SQL(" LIMIT {}").format(sql.Literal(limit)) if limit else sql.SQL("")
    query = sql.SQL(
        "CREATE UNLOGGED TABLE {table} AS SELECT * FROM ({user_sql}) AS s0{limit}"
    ).format(table=sql.Identifier(table_name), user_sql=sql.SQL(user_sql), limit=limit_clause)
    with conn.cursor() as cur:
        cur.execute(query)
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table_name)))
        return cur.fetchone()[0]


def ctid_page_bounds(conn, table_name, workers):
    """Splits the materialized table's block range into `workers` roughly
    equal, non-overlapping, gap-free page ranges - pg_relation_size() is
    exact immediately after CREATE TABLE AS (no ANALYZE/stats needed,
    unlike relpages). Returns a list of (lo_page, hi_page_or_None) tuples,
    one per worker, in worker-id order; the last worker's upper bound is
    None (open-ended) so it also catches anything at/after the final
    computed boundary.
    """
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT pg_relation_size({t}) / current_setting('block_size')::bigint").format(
                t=sql.Literal(table_name)
            )
        )
        pages = cur.fetchone()[0] or 0
    pages = max(pages, 1)
    bounds = []
    for i in range(workers):
        lo = (i * pages) // workers
        hi = ((i + 1) * pages) // workers
        bounds.append((lo, hi if i < workers - 1 else None))
    return bounds


def build_worker_source_sql(conn, table_name, page_lo, page_hi):
    """Renders (to text, via psycopg's own identifier/literal quoting) the
    ctid-bounded slice of the materialized table that one worker owns -
    "SELECT * FROM <table> t WHERE t.ctid >= ... [AND t.ctid < ...]" -
    ready to plug into build_publish_query()/build_batched_publish_query()/
    build_native_columnar_publish_query() as their `user_sql` argument,
    exactly the same shape as the top-level --sql string those already
    accept. Rendering to text once up front (rather than composing
    sql.SQL(user_sql) around a Composed object) keeps the per-worker query
    construction identical to the single-connection path's, so batching
    logic doesn't need a parallel-aware variant.
    """
    ctid_filter = sql.SQL("t.ctid >= {lo}::text::tid").format(lo=sql.Literal(f"({page_lo},0)"))
    if page_hi is not None:
        ctid_filter = sql.SQL("{lo} AND t.ctid < {hi}::text::tid").format(
            lo=ctid_filter, hi=sql.Literal(f"({page_hi},0)")
        )
    composed = sql.SQL("SELECT * FROM {table} t WHERE {filter}").format(
        table=sql.Identifier(table_name), filter=ctid_filter
    )
    return composed.as_string(conn)


def build_worker_publish_query(conn, args, fmt, subject_columns, subject_expr, all_columns,
                                table_name, page_lo, page_hi, batching, verify):
    worker_sql = build_worker_source_sql(conn, table_name, page_lo, page_hi)
    if batching and args.batch_encoding == "native":
        return build_native_columnar_publish_query(
            worker_sql, None, fmt, subject_columns, subject_expr, args.batch_size, verify
        )
    if batching:
        return build_batched_publish_query(
            worker_sql, None, fmt, subject_columns, all_columns, subject_expr, args.batch_size, verify
        )
    return build_publish_query(worker_sql, None, fmt, subject_expr, verify)


def _unpack_worker_row(row, batching, verify):
    """The worker query's column shape varies with (batching, verify) -
    (ref?, bytes, batch_rows?, _pub) - see build_publish_query()/
    build_batched_publish_query()/build_native_columnar_publish_query()'s
    own `cols` construction, which this mirrors positionally rather than
    by name (psycopg rows are plain tuples).
    """
    idx = 0
    ref = None
    if verify:
        ref = row[idx]
        idx += 1
    nbytes = row[idx]
    idx += 1
    batch_rows = row[idx] if batching else 1
    return ref, nbytes, batch_rows


def run_parallel_worker(dsn, query, batching, verify, worker_id):
    """Runs in its own thread with its own connection (opened here, not
    shared) - the real parallelism is N separate Postgres backend
    processes doing the encode+publish work concurrently; the Python
    thread mostly just blocks on that connection's socket.

    Without --verify, no row data is needed back in Python at all - just
    counts/byte totals - so this uses the same one-round-trip server-side
    aggregation as the single-connection path's build_count_query()/
    build_batched_count_query() (see run_one_format()), rather than
    streaming every row back to be counted in Python. This isn't just a
    tidiness win: confirmed directly that shipping every row back to
    Python for row-by-row counting is GIL-bound across worker threads (a
    controlled isolation test - server-side count-only vs Python-side
    row iteration, same query, same worker counts - showed the row-
    iteration version topping out at ~2.7x aggregate throughput at 16
    workers while the count-only version reached ~4.6x), and was the
    dominant cause of --workers' earlier sub-linear scaling - a bug in
    this benchmark tool's own measurement path, not a real Postgres/NATS
    limitation. With --verify, the decoded reference rows genuinely have
    to reach Python (there's no way around that), so this still streams
    (cursor.stream()) row-by-row in that case, same as the
    single-connection path.
    """
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        t0 = time.perf_counter()
        reference = [] if verify else None
        with conn.cursor() as cur:
            if verify:
                rows = 0
                batches = 0
                total_bytes = 0
                for row in cur.stream(query):
                    ref, nbytes, batch_rows = _unpack_worker_row(row, batching, verify)
                    reference.append(canonical(ref))
                    total_bytes += nbytes
                    rows += batch_rows
                    batches += 1
            else:
                count_query = build_batched_count_query(query) if batching else build_count_query(query)
                cur.execute(count_query)
                if batching:
                    batches, rows, total_bytes, _min_b, _max_b = cur.fetchone()
                    rows = int(rows) if rows is not None else 0
                else:
                    rows, total_bytes, _avg_b, _min_b, _max_b = cur.fetchone()
                    rows = rows or 0
                    batches = rows
                total_bytes = int(total_bytes) if total_bytes is not None else 0
        return {"worker_id": worker_id, "rows": rows, "batches": batches, "total_bytes": total_bytes,
                "elapsed": time.perf_counter() - t0, "reference": reference}
    finally:
        conn.close()


def run_one_format_parallel(conn, args, fmt, subject_columns, all_columns):
    """--workers > 1 path: materializes --sql once (see
    materialize_partitioned()), then fans out N independent connections
    each publishing its own disjoint physical-page-range share
    concurrently, timed as one wall-clock window (not summed per-worker
    time) so the result reflects real elapsed-time throughput, the way a
    caller waiting on the whole run would experience it. Combinable with
    --batch-size (each worker runs the same batched/columnar query the
    single-connection path would, just restricted to its own slice) and
    --verify (one shared nats_tool consumer per format, same as the
    single-connection path; each worker streams and contributes its own
    slice of the decoded reference list, merged before comparing).
    """
    metrics = {"format": fmt, "error": None, "workers": args.workers}
    batching = args.batch_size > 1
    table_name = f"nats_bench_parallel_{uuid.uuid4().hex[:12]}"
    subject_expr = build_subject_expr(
        subject_columns, args.subject_prefix, fmt, args.subject_format_suffix, args.on_bad_subject_value
    )

    dump_dir = dump_path = log_path = None
    consumer = None
    try:
        print(f"== [{fmt}] 1. Materializing --sql's rows ({args.workers} workers) ==")
        total_rows = materialize_partitioned(conn, args.sql_stripped, args.limit, table_name)
        bounds = ctid_page_bounds(conn, table_name, args.workers)
        print(f"  {total_rows} row(s) materialized, partitioning by physical page range across "
              f"{args.workers} worker(s)")

        step = 2
        if args.verify:
            dump_dir = tempfile.mkdtemp(prefix=f"nats_e2e_{fmt}_")
            dump_path = os.path.join(dump_dir, f"{fmt}.jsonl")
            log_path = os.path.join(dump_dir, f"{fmt}.log")
            topic = args.nats_topic or default_topic(
                subject_columns, args.subject_prefix, fmt, args.subject_format_suffix
            )
            print(f"== [{fmt}] {step}. Starting nats_tool consumer (topic={topic!r}) ==")
            step += 1
            with open(log_path, "w") as log_file:
                consumer = start_consumer(args.nats_tool, topic, fmt, dump_path, log_file)
            if not wait_for_subscribed(log_path, args.connect_wait_secs):
                print(f"  WARNING: consumer did not confirm subscription within "
                      f"{args.connect_wait_secs}s -- proceeding anyway", file=sys.stderr)
            else:
                print("  consumer subscribed")

        encoding_note = f", {args.batch_encoding} batch encoding" if batching else ""
        print(f"== [{fmt}] {step}. Publishing ({args.workers} parallel connections{encoding_note}) ==")
        step += 1
        worker_queries = [
            build_worker_publish_query(conn, args, fmt, subject_columns, subject_expr, all_columns,
                                        table_name, lo, hi, batching, args.verify)
            for lo, hi in bounds
        ]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [
                ex.submit(run_parallel_worker, args.dsn, q, batching, args.verify, i)
                for i, q in enumerate(worker_queries)
            ]
            worker_results = [f.result() for f in futures]
        wall_secs = time.perf_counter() - t0

        for w in sorted(worker_results, key=lambda w: w["worker_id"]):
            print(f"  worker {w['worker_id']}: {w['rows']} row(s) in {fmt_secs(w['elapsed'])} "
                  f"({fmt_rate(w['rows'], w['elapsed'])})")

        published = sum(w["rows"] for w in worker_results)
        batches = sum(w["batches"] for w in worker_results)
        total_bytes = sum(w["total_bytes"] for w in worker_results)
        batch_note = f" as {batches} batch(es), {args.batch_encoding} encoding" if batching else ""
        print(f"  published {published} row(s){batch_note} across {args.workers} worker(s) in "
              f"{fmt_secs(wall_secs)} ({fmt_rate(published, wall_secs)} aggregate, {fmt_bytes(total_bytes)} total)")

        metrics.update(
            rows=published, batches=batches, publish_secs=wall_secs,
            total_bytes=total_bytes,
            avg_bytes=(total_bytes / published) if published else None,
            batch_encoding=(args.batch_encoding if batching else None),
        )
        if published != total_rows:
            metrics["error"] = (f"expected {total_rows} row(s) across all workers, published "
                                 f"{published} (partitioning bug or a worker failed)")
            return metrics
        if published == 0:
            metrics["error"] = "--sql produced zero rows, nothing was published"
            return metrics

        if not args.verify:
            return metrics

        print(f"== [{fmt}] {step}. Waiting for delivery ==")
        step += 1
        # nats_tool's own stats count messages (batches), not rows.
        received_by_stats, receive_secs = wait_for_received_count(log_path, batches, args.timeout_secs)
        metrics["receive_secs"] = receive_secs
        if received_by_stats < batches:
            print(f"  WARNING: only observed {received_by_stats}/{batches} via consumer "
                  f"stats within {args.timeout_secs}s -- proceeding to compare anyway",
                  file=sys.stderr)
        else:
            print(f"  all {batches} batch(es) observed received in {fmt_secs(receive_secs)} "
                  f"({fmt_rate(published, receive_secs)})")

        # Same shutdown reasoning as the single-connection path: grub mode
        # never self-exits, and SIGTERM (not kill()) is what triggers the
        # dump file's flush-on-clean-exit.
        consumer.terminate()
        try:
            consumer.wait(timeout=args.timeout_secs)
        except subprocess.TimeoutExpired:
            consumer.kill()
            consumer.wait(timeout=5)
            print(f"  WARNING: consumer did not exit cleanly within {args.timeout_secs}s "
                  "after SIGTERM (force-killed) -- its dump file may be incomplete", file=sys.stderr)

        reference = []
        for w in worker_results:
            reference.extend(w["reference"])
        received = []
        if os.path.exists(dump_path):
            with open(dump_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        received.append(canonical(json.loads(line)))

        unit = "batch(es)" if batching else "row(s)"
        print(f"== [{fmt}] {step}. Comparing ==")
        print(f"  reference: {len(reference)} {unit}, received: {len(received)} {unit}")

        exp_sorted = sorted(reference)
        act_sorted = sorted(received)
        if exp_sorted == act_sorted:
            print(f"  PASS: all {len(reference)} {unit} ({published} row(s)) received and "
                  "decoded correctly (unordered content match)")
            metrics["verify_result"] = "PASS"
            return metrics

        exp_counts = Counter(reference)
        act_counts = Counter(received)
        missing = exp_counts - act_counts
        extra = act_counts - exp_counts
        print(f"  FAIL: {sum(missing.values())} {unit} missing/mismatched, "
              f"{sum(extra.values())} unexpected {unit} received")
        for i, s in enumerate(list(missing.elements())[:3]):
            print(f"    missing[{i}]: {s[:300]}")
        for i, s in enumerate(list(extra.elements())[:3]):
            print(f"    extra[{i}]:   {s[:300]}")
        metrics["verify_result"] = "FAIL"
        metrics["error"] = f"{sum(missing.values())} missing, {sum(extra.values())} unexpected"
        return metrics
    except psycopg.Error as e:
        metrics["error"] = f"publish failed: {e}"
        return metrics
    finally:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table_name)))
        if consumer and consumer.poll() is None:
            consumer.kill()
        if dump_dir:
            if args.keep_dump:
                print(f"  (dump kept at {dump_dir})")
            else:
                shutil.rmtree(dump_dir, ignore_errors=True)


def run_one_format(conn, args, fmt, subject_columns, all_columns):
    """Publishes --sql once, encoded as `fmt`, optionally verifying via
    nats_tool. Never raises for an expected failure mode (bad SQL, bad
    subject data, verify mismatch, etc) - returns a dict with an "error"
    key set instead, so one format's failure doesn't stop a multi-format
    --format run from reporting the others.

    `all_columns` is only used when --batch-size > 1 (introspected once in
    main(), not per format - the column list doesn't depend on fmt).
    """
    if args.workers > 1:
        return run_one_format_parallel(conn, args, fmt, subject_columns, all_columns)

    metrics = {"format": fmt, "error": None}
    batching = args.batch_size > 1
    subject_expr = build_subject_expr(
        subject_columns, args.subject_prefix, fmt, args.subject_format_suffix, args.on_bad_subject_value
    )
    if batching and args.batch_encoding == "native":
        publish_query = build_native_columnar_publish_query(
            args.sql_stripped, args.limit, fmt, subject_columns,
            subject_expr, args.batch_size, args.verify
        )
    elif batching:
        publish_query = build_batched_publish_query(
            args.sql_stripped, args.limit, fmt, subject_columns, all_columns,
            subject_expr, args.batch_size, args.verify
        )
    else:
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
        published = 0   # rows
        batches = 0      # NATS messages - == published unless batching
        total_bytes = min_bytes = max_bytes = None
        t0 = time.perf_counter()
        if args.verify:
            print(f"== [{fmt}] 2. Streaming --sql and publishing ==")
            reference = []
            total_bytes = 0
            try:
                with conn.cursor() as cur:
                    if batching:
                        for ref, nbytes, batch_rows, _pub in cur.stream(publish_query):
                            reference.append(canonical(ref))
                            total_bytes += nbytes
                            min_bytes = nbytes if min_bytes is None else min(min_bytes, nbytes)
                            max_bytes = nbytes if max_bytes is None else max(max_bytes, nbytes)
                            prev = published
                            published += batch_rows
                            batches += 1
                            if args.progress_every and published // args.progress_every > prev // args.progress_every:
                                print(f"  ...{published} rows published so far ({batches} batches)")
                    else:
                        for ref, nbytes, _pub in cur.stream(publish_query):
                            reference.append(canonical(ref))
                            total_bytes += nbytes
                            min_bytes = nbytes if min_bytes is None else min(min_bytes, nbytes)
                            max_bytes = nbytes if max_bytes is None else max(max_bytes, nbytes)
                            published += 1
                            batches += 1
                            if args.progress_every and published % args.progress_every == 0:
                                print(f"  ...{published} published so far")
            except psycopg.Error as e:
                metrics["error"] = f"publish failed after {published} row(s): {e}"
                return metrics
        else:
            print(f"== [{fmt}] 1. Publishing ==")
            try:
                with conn.cursor() as cur:
                    if batching:
                        cur.execute(build_batched_count_query(publish_query))
                        batches, published, total_bytes, min_bytes, max_bytes = cur.fetchone()
                        # sum(bigint) comes back as numeric (Decimal in
                        # psycopg), unlike count(*)'s bigint (plain int) -
                        # normalize both to int for downstream arithmetic.
                        published = int(published) if published is not None else 0
                        total_bytes = int(total_bytes) if total_bytes is not None else None
                    else:
                        cur.execute(build_count_query(publish_query))
                        published, total_bytes, _avg_bytes, min_bytes, max_bytes = cur.fetchone()
                        batches = published
            except psycopg.Error as e:
                metrics["error"] = f"publish failed: {e}"
                return metrics
        publish_secs = time.perf_counter() - t0
        batch_note = f" as {batches} batch(es), {args.batch_encoding} encoding" if batching else ""
        print(f"  published {published} row(s){batch_note} in {fmt_secs(publish_secs)} "
              f"({fmt_rate(published, publish_secs)}, {fmt_bytes(total_bytes)} total)")

        metrics.update(
            rows=published, batches=batches, publish_secs=publish_secs,
            total_bytes=total_bytes,
            avg_bytes=(total_bytes / published) if published else None,
            min_bytes=min_bytes, max_bytes=max_bytes,
            batch_encoding=(args.batch_encoding if batching else None),
        )

        if published == 0:
            metrics["error"] = "--sql produced zero rows, nothing was published"
            return metrics

        if not args.verify:
            return metrics

        print(f"== [{fmt}] 3. Waiting for delivery ==")
        # nats_tool's own stats count messages (batches), not rows.
        received_by_stats, receive_secs = wait_for_received_count(
            log_path, batches, args.timeout_secs
        )
        metrics["receive_secs"] = receive_secs
        if received_by_stats < batches:
            print(f"  WARNING: only observed {received_by_stats}/{batches} via consumer "
                  f"stats within {args.timeout_secs}s -- proceeding to compare anyway",
                  file=sys.stderr)
        else:
            print(f"  all {batches} batch(es) observed received in {fmt_secs(receive_secs)} "
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

        unit = "batch(es)" if batching else "row(s)"
        print(f"== [{fmt}] 4. Comparing ==")
        print(f"  reference: {len(reference)} {unit}, received: {len(received)} {unit}")

        exp_sorted = sorted(reference)
        act_sorted = sorted(received)
        if exp_sorted == act_sorted:
            print(f"  PASS: all {len(reference)} {unit} ({published} row(s)) received and "
                  "decoded correctly (unordered content match)")
            metrics["verify_result"] = "PASS"
            return metrics

        exp_counts = Counter(reference)
        act_counts = Counter(received)
        missing = exp_counts - act_counts
        extra = act_counts - exp_counts
        print(f"  FAIL: {sum(missing.values())} {unit} missing/mismatched, "
              f"{sum(extra.values())} unexpected {unit} received")
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


def print_comparison(results, verify, batching):
    print("\n== Format comparison ==")
    headers = ["format", "rows"]
    if batching:
        headers += ["batches"]
    headers += ["avg bytes/row", "publish", "publish rate"]
    if verify:
        headers += ["receive", "receive rate", "verify"]
    rows = []
    for r in results:
        if r.get("error") and r.get("rows") is None:
            rows.append([r["format"], "ERROR: " + r["error"]])
            continue
        row = [r["format"], str(r["rows"])]
        if batching:
            row += [str(r.get("batches", "-"))]
        row += [
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
    if args.batch_size < 1:
        sys.exit("error: --batch-size must be >= 1")
    if args.workers < 1:
        sys.exit("error: --workers must be >= 1")

    conn = psycopg.connect(args.dsn, autocommit=True)

    all_columns = None
    if args.batch_size > 1 and args.batch_encoding == "jsonb":
        all_columns = introspect_columns(conn, args.sql_stripped)
        missing = [c for c in subject_columns if c not in all_columns]
        if missing:
            sys.exit(f"error: --subject-columns {missing} not found in --sql's result columns "
                     f"{all_columns}")

    if len(formats) > 1:
        # Whichever format runs first pays three one-time costs that have
        # nothing to do with that format's encoder: (1) pulling --sql's rows
        # into Postgres's shared_buffers/the OS page cache from disk, (2)
        # this backend's first-ever call into pg_zerialize.so (dlopen/
        # relocation/static-init, charged once per process), and (3) - the
        # biggest of the three, confirmed directly at ~6.5ms for the first
        # nats_publish_text()/nats_publish_binary() call in a fresh session
        # vs ~0.1ms after - pgnats lazily establishing its NATS connection
        # on first use. All three were conflated at first as "msgpack is
        # slower than the others" purely because it happened to run first in
        # one comparison; rerunning the same comparison with a different
        # format listed first shifted the "slowest" result to whichever one
        # moved to the front, proving none of it was intrinsic to any one
        # format. Warming all three here - one plain data read, one
        # throwaway pg_zerialize call (msgpack chosen arbitrarily; every
        # format lives in the same .so, so any one call loads it for all of
        # them), one throwaway nats_publish_text() to a disposable subject -
        # means every format's timing starts from the same warm state, not
        # whichever position it happened to run in.
        print("Warming cache, pg_zerialize.so, and the NATS connection (before timing any format)...")
        limit_clause = sql.SQL(" LIMIT {}").format(sql.Literal(args.limit)) if args.limit else sql.SQL("")
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT count(*) FROM ({}) AS t{}").format(
                sql.SQL(args.sql_stripped), limit_clause
            ))
            cur.fetchone()
            cur.execute(sql.SQL("SELECT octet_length(row_to_msgpack(t)) FROM ({}) AS t LIMIT 1").format(
                sql.SQL(args.sql_stripped)
            ))
            cur.fetchone()
            cur.execute("SELECT nats_publish_text('_e2e_warmup.discard', 'warmup')")
            cur.fetchone()

    results = []
    any_error = False
    for i, fmt in enumerate(formats):
        if len(formats) > 1:
            print(f"\n### format {i + 1}/{len(formats)}: {fmt} ###")
        r = run_one_format(conn, args, fmt, subject_columns, all_columns)
        results.append(r)
        if r.get("error"):
            any_error = True
            print(f"  ERROR: {r['error']}", file=sys.stderr)

    if len(formats) > 1:
        print_comparison(results, args.verify, args.batch_size > 1)

    if args.metrics_json:
        with open(args.metrics_json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nMetrics written to {args.metrics_json}")

    sys.exit(1 if any_error else 0)


if __name__ == "__main__":
    main()
