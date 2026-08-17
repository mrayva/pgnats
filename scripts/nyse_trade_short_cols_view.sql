-- 2-letter-column-name view over an existing NYSE trade fixture table
-- (nyse_eqy_us_all_trade_20260102), used to measure how much of row-mode
-- publish's per-message overhead is field-name bytes vs everything else.
-- Row mode (nats_publish_from_sql.py's default, --batch-size 1) re-writes
-- every field's key string into every message, unlike batched/columnar
-- mode where keys are amortized across a whole batch - this table's real
-- column names are unusually verbose ("Trade Reporting Facility TRF
-- Timestamp" is 38 characters), making it a good worst-case test of that
-- per-row key-name cost.
--
-- Measured against 20,000 real rows, --format all, row mode, core NATS
-- (not JetStream), --verify PASS on all 7 formats both ways:
--
--   format       bytes/row (long)  bytes/row (short)  size cut  publish rate gain
--   -----------  ----------------  ------------------  --------  -----------------
--   msgpack      358B              131B                 2.73x    +26%
--   cbor         362B              131B                 2.76x    +19%
--   zera         648B              425B                 1.53x    +7%
--   flexbuffers  539B              296B                 1.82x    +15%
--   ion          399B              160B                 2.49x    +20%
--   bson         413B              183B                 2.26x    +15%
--   beve         369B              141B                 2.62x    +13%
--
-- msgpack/cbor benefited the most (no per-document key deduplication, so
-- shrinking key strings has maximal proportional impact); zera and
-- flexbuffers benefited the least (both already intern/deduplicate keys
-- internally, so a chunk of their per-row overhead lives elsewhere -
-- shape/type metadata - and doesn't scale with raw key-string length).
-- Throughput gains are real but smaller than the byte savings, matching
-- nats_publish_from_sql.py's own established finding that row-mode
-- publish throughput is dominated by the per-message publish() round
-- trip itself, not encoding cost.
--
-- Reproduce with:
--   psql -f scripts/nyse_trade_short_cols_view.sql
--   ./scripts/nats_publish_from_sql.py --sql 'SELECT * FROM nyse_trade_short_cols' \
--       --subject-columns sn --format all --limit 20000 --verify
-- (compare against the same command with --sql 'SELECT * FROM
-- nyse_eqy_us_all_trade_20260102' --subject-columns 'Sequence Number')

CREATE OR REPLACE VIEW nyse_trade_short_cols AS
SELECT
    "Time"                                    AS tm,
    "Exchange"                                AS ex,
    "Symbol"                                  AS sy,
    "Sale Condition"                          AS sc,
    "Trade Volume"                            AS tv,
    "Trade Price"                             AS tp,
    "Trade Stop Stock Indicator"              AS ts,
    "Trade Correction Indicator"              AS tc,
    "Sequence Number"                         AS sn,
    "Trade Id"                                AS ti,
    "Source of Trade"                         AS so,
    "Trade Reporting Facility"                AS tf,
    "Participant Timestamp"                   AS pt,
    "Trade Reporting Facility TRF Timestamp"  AS tr,
    "Trade Through Exempt Indicator"          AS te
FROM nyse_eqy_us_all_trade_20260102;
