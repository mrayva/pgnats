#!/usr/bin/env python3
"""Compares nats_tool's decoded output for each zerialize format against a
reference JSONL (produced by pg_zerialize's own msgpack_to_jsonb) after a
full publish/NATS-transport/decode round trip.

Usage: compare_e2e_results.py <reference.jsonl> <format>=<out.jsonl> [...]

Structural JSON comparison, not string comparison: formats/tooling are free
to reorder object keys or reformat whitespace without that being a real
mismatch, matching the comparison approach used throughout this session's
own pg_zerialize/nats_asio digest-based verification.

Exits 0 if every format received exactly the expected row count with no
content mismatches; exits 1 and prints a per-format breakdown otherwise.
"""

import json
import re
import sys

SUBJECT_RE = re.compile(r"^pgtest\.([a-z]+)\.(\d+)$")


def load_reference(path):
    expected = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            expected[obj["row_id"]] = obj["expected"]
    return expected


def load_actual(path, fmt):
    """Returns {row_id: payload} for lines whose subject matches this format."""
    actual = {}
    duplicates = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            m = SUBJECT_RE.match(obj["subject"])
            if not m or m.group(1) != fmt:
                print(f"  WARNING: unexpected subject '{obj['subject']}' in {path}")
                continue
            row_id = int(m.group(2))
            if row_id in actual:
                duplicates.append(row_id)
            actual[row_id] = obj["payload"]
    return actual, duplicates


def compare_format(fmt, out_path, expected):
    print(f"--- {fmt} ---")
    actual, duplicates = load_actual(out_path, fmt)

    ok = True
    if duplicates:
        ok = False
        print(f"  FAIL: {len(duplicates)} duplicate row_id(s) received: {sorted(set(duplicates))}")

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        ok = False
        print(f"  FAIL: missing {len(missing)} row(s): {missing}")
    if extra:
        ok = False
        print(f"  FAIL: {len(extra)} unexpected row(s): {extra}")

    mismatches = []
    for row_id in sorted(set(expected) & set(actual)):
        if actual[row_id] != expected[row_id]:
            mismatches.append(row_id)

    if mismatches:
        ok = False
        print(f"  FAIL: {len(mismatches)} content mismatch(es): {mismatches}")
        for row_id in mismatches[:3]:
            print(f"    row {row_id}:")
            print(f"      expected: {json.dumps(expected[row_id], sort_keys=True)}")
            print(f"      actual:   {json.dumps(actual[row_id], sort_keys=True)}")
        if len(mismatches) > 3:
            print(f"    ... and {len(mismatches) - 3} more")

    if ok:
        print(f"  PASS: {len(actual)}/{len(expected)} rows, all match")
    return ok


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)

    reference_path = sys.argv[1]
    format_args = sys.argv[2:]

    expected = load_reference(reference_path)
    print(f"Reference: {len(expected)} rows from {reference_path}\n")

    all_ok = True
    for arg in format_args:
        fmt, _, out_path = arg.partition("=")
        if not out_path:
            print(f"Bad argument (expected fmt=path): {arg}")
            sys.exit(2)
        if not compare_format(fmt, out_path, expected):
            all_ok = False
        print()

    if all_ok:
        print("RESULT: all formats PASS")
        sys.exit(0)
    else:
        print("RESULT: FAILURE (see above)")
        sys.exit(1)


if __name__ == "__main__":
    main()
