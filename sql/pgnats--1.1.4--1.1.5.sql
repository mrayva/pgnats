/* <begin connected objects> */
-- src/api/nats.rs:171
-- pgnats::api::nats::nats_publish_flush
CREATE  FUNCTION "nats_publish_flush"() RETURNS VOID /* anyhow :: Result < () > */
STRICT
LANGUAGE c /* Rust */
AS 'MODULE_PATHNAME', 'nats_publish_flush_wrapper';
/* </end connected objects> */
