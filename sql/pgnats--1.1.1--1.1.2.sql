/* <begin connected objects> */
-- src/api/nats.rs:246
-- pgnats::api::nats::nats_put_binary_async
CREATE  FUNCTION "nats_put_binary_async"(
	"bucket" TEXT, /* String */
	"key" TEXT, /* & str */
	"data" bytea /* Vec < u8 > */
) RETURNS VOID /* anyhow :: Result < () > */
STRICT
LANGUAGE c /* Rust */
AS 'MODULE_PATHNAME', 'nats_put_binary_async_wrapper';
/* </end connected objects> */
