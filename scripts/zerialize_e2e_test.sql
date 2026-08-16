-- Fixture for scripts/zerialize_e2e_test.sh: a representative "arbitrary
-- table" with a wide type spread (ints of every width, floats including
-- +/-Infinity and NaN, bool, text with quotes/backslashes/unicode, jsonb,
-- bytea, timestamp, numeric, a text array, and a composite column), plus
-- edge cases (an all-NULL row, empty string/array/bytea).
--
-- Row 3 (+/-Infinity) and row 5 (NaN) are deliberate regression coverage,
-- not filler: this exact fixture caught a real bug on its first run --
-- every pg_zerialize format failed to decode row 3 end to end, because
-- zerialize::translate<JSON> (what nats_tool's --format decode uses)
-- threw for the *entire* message on any non-finite double, not just that
-- field. Fixed upstream in zerialize (json.hpp's double_()); NaN
-- specifically wasn't covered by that original repro, so row 5 exists to
-- make sure it doesn't regress independently of +/-Infinity.
--
-- Not meant to be exhaustive on its own -- pg_zerialize's own regression
-- suite already covers that. This just needs enough variety that a
-- publish/transport/decode bug would actually show up in at least one row.

DROP TABLE IF EXISTS pgnats_zerialize_demo CASCADE;
DROP TYPE IF EXISTS pgnats_zerialize_demo_addr CASCADE;

CREATE TYPE pgnats_zerialize_demo_addr AS (
    city text,
    zip  text
);

CREATE TABLE pgnats_zerialize_demo (
    id      integer PRIMARY KEY,
    i2      smallint,
    i4      integer,
    i8      bigint,
    f4      real,
    f8      double precision,
    flag    boolean,
    label   text,
    meta    jsonb,
    blob    bytea,
    ts      timestamp,
    amount  numeric,
    tags    text[],
    address pgnats_zerialize_demo_addr
);

-- Explicit edge-case rows.
INSERT INTO pgnats_zerialize_demo VALUES
(1, (-32768)::smallint, (-2147483648)::integer, '-9223372036854775808'::bigint,
 1.5::real, 2.5::double precision, true, 'hello "world" \ backslash',
 '{"k":1,"nested":[1,2,3]}'::jsonb, decode('DEADBEEF', 'hex'),
 '2000-01-01 00:00:00'::timestamp, 1234.5678, ARRAY['a', 'b'],
 ROW('Springfield', '00000')::pgnats_zerialize_demo_addr),
(2, 0::smallint, 0::integer, 0::bigint, 0.0::real, 0.0::double precision, false, '',
 'null'::jsonb, ''::bytea, '1970-01-01 00:00:00'::timestamp, 0,
 ARRAY[]::text[], NULL),
(3, 32767::smallint, 2147483647::integer, '9223372036854775807'::bigint,
 'Infinity'::real, '-Infinity'::double precision, true, 'unicode: 日本語 🎉 Ω',
 '{"a":[1,2,{"b":true}]}'::jsonb, decode('00FF10', 'hex'),
 '2038-01-19 03:14:07'::timestamp, -999.99, ARRAY['x', 'y', 'z'],
 ROW('Tokyo', '100-0001')::pgnats_zerialize_demo_addr),
(4, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
(5, 1::smallint, 1::integer, 1::bigint,
 'NaN'::real, 'NaN'::double precision, false, 'nan row',
 '{"note":"nan floats"}'::jsonb, decode('CAFE', 'hex'),
 '2024-06-15 12:00:00'::timestamp, 3.14159, ARRAY['n', 'a', 'n'],
 ROW('NaNCity', '99999')::pgnats_zerialize_demo_addr);

-- Bulk-generated rows, deterministic so re-runs are reproducible.
INSERT INTO pgnats_zerialize_demo
SELECT
    g,
    (g % 32000)::smallint,
    g * 1000,
    g::bigint * 1000000,
    g::real * 1.5,
    g::double precision * 2.25,
    (g % 2 = 0),
    'row-' || g,
    jsonb_build_object('id', g, 'sq', g * g),
    decode(md5(g::text), 'hex'),
    ('2020-01-01'::date + (g || ' days')::interval)::timestamp,
    (g * 1.1)::numeric,
    ARRAY['tag' || g, 'tag' || (g + 1)],
    ROW('City' || g, lpad(g::text, 5, '0'))::pgnats_zerialize_demo_addr
FROM generate_series(6, 30) g;
