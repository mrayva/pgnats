/* <begin connected objects> */
-- src/api/nats.rs:127
-- pgnats::api::nats::nats_publish_binary_stream_async
CREATE  FUNCTION "nats_publish_binary_stream_async"(
	"subject" TEXT, /* & str */
	"payload" bytea, /* Vec < u8 > */
	"headers" jsonb DEFAULT NULL /* Option < pgrx :: JsonB > */
) RETURNS VOID /* anyhow :: Result < () > */
LANGUAGE c /* Rust */
AS 'MODULE_PATHNAME', 'nats_publish_binary_stream_async_wrapper';
/* </end connected objects> */

/* <begin connected objects> */
-- src/api/nats.rs:161
-- pgnats::api::nats::nats_publish_stream_flush
CREATE  FUNCTION "nats_publish_stream_flush"() RETURNS bigint /* anyhow :: Result < i64 > */
STRICT
LANGUAGE c /* Rust */
AS 'MODULE_PATHNAME', 'nats_publish_stream_flush_wrapper';
/* </end connected objects> */
