# CHANGELOG

## [1.1.2] - 2026-08-17

### Added (New Features)

* `nats_put_binary_async(bucket, key, data)`: pipelined KV put, mirroring
  1.1.1's `nats_publish_binary_stream_async()` - queues the ack instead
  of waiting for it, sharing the same `pending_stream_acks` queue,
  `PENDING_STREAM_ACK_LIMIT` auto-drain, and `nats_publish_stream_flush()`
  as the stream-publish async path (no separate flush function needed;
  `kv::Store::put()` turned out to be nothing more than
  `context.publish(subject, value).await?.await?` under the hood - the
  same mechanism, just with the subject built from the bucket's
  key-value prefix). Not durable until `nats_publish_stream_flush()` is
  called. Doesn't validate the key or support JetStream domains the way
  `nats_put_binary()` does - pre-sanitize keys.

  Measured: 500,000 real rows, single connection, fully acked -
  194,932 rows/s, vs ~2,467 rows/s per connection for the existing
  synchronous `nats_put_binary()` (~2,467/s single-connection derived
  from a 16-connection/41,505 rows/s aggregate measurement) - roughly
  **79x** on one connection, and past native `nats bench kv put`'s own
  16-client aggregate ceiling (41,382 msgs/s), which has no async/
  pipelined put mode to compare against directly.

## [1.1.1] - 2026-08-17

### Fixed

* `nats_publish_binary_stream()` / `nats_publish_json_stream()` /
  `nats_publish_jsonb_stream()` / `nats_publish_text_stream()` now actually
  wait for and check the message's JetStream ack. Previously, the
  `PublishAckFuture` returned by `async_nats::jetstream::Context::publish()`
  was discarded (`let _ = js.publish(...).await?`) - that only confirmed
  the message was handed to the connection, not that the stream accepted
  it. Dropping a `PublishAckFuture` hands it to async-nats's own background
  "acker" task, which waits for the ack (or times out) and then
  unconditionally discards the result either way - so a failed or
  timed-out ack was never surfaced to the caller, despite these
  functions' own docs promising "JetStream persistence and delivery
  guarantees." A publish that genuinely fails now raises a real error
  and aborts the calling transaction, instead of silently succeeding.

### Added (New Features)

* `nats_publish_binary_stream_async(subject, payload, headers)`: pipelined
  JetStream publish. Returns as soon as the message is handed to the
  connection (gated only by async-nats's own max-in-flight-acks semaphore,
  default 5,000) instead of blocking on that one message's ack the way
  `nats_publish_binary_stream()` does - useful when the per-message round
  trip is the bottleneck for high-throughput row-mode publishing. The ack
  is queued on the backend connection, not discarded.
* `nats_publish_stream_flush()`: awaits every ack queued by
  `nats_publish_binary_stream_async()` on the current backend connection,
  concurrently, and raises an error naming every publish that failed to
  ack (not just the first). **A publish made with
  `nats_publish_binary_stream_async()` is not durable until this is
  called** - nothing flushes it automatically.

## [1.1.0] - 2025-12-15

### Changed (Breaking Changes)

* Subscription Table Refactoring: The `pgnats.subscriptions` table structure was fundamentally changed to improve reliability and internal processing:

  * The `callback` column (TEXT) was removed.

  * The `fn_oid` column (OID) was added to store the PostgreSQL function's Object ID.

> [!WARNING] 
> Impact: During the upgrade, all existing subscriptions are migrated by resolving the function name to its OID. Subscriptions referencing non-existent functions will be dropped during migration.

* Changed `pgnats_version()` signature: The function providing version information has been updated to return a detailed table of build metadata instead of a single text string.

  * Old Signature: `pgnats_version() RETURNS TEXT`

  * New Signature: `pgnats_version() RETURNS TABLE (version TEXT, commit_date TEXT, short_commit TEXT, branch TEXT, last_tag TEXT)`

### Added (New Features)

* When a subscribed PostgreSQL function is dropped, this event trigger automatically removes the corresponding entry from the `pgnats.subscriptions` table, preventing background worker errors.
