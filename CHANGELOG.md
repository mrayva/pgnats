# CHANGELOG

## [1.1.5] - 2026-08-23

### Added

* `nats_publish_flush()` - blocks until every plain (non-JetStream) publish
  made on this backend connection *up to that point* has been written to
  the socket. Root-caused via a real `nats_sidecar` benchmark stall:
  `async-nats`'s `Client::publish()` only awaits handing the message to its
  internal command channel, not writing it to the wire - under a high call
  rate (one `nats_publish_binary`/etc. call per row from a tight Postgres
  loop) the connection's write/flush loop can fall behind, and a
  "successfully" published message (`Ok(())`) can sit unsent in an internal
  buffer with nothing before this release able to detect it. Same class of
  gap the JetStream path already had a fix for (`nats_publish_stream_flush`)
  - core NATS still has no publish ack, so this confirms the client actually
  wrote the message, not that the server received it.
  `nats_publish_binary`/`nats_publish_text`/`nats_publish_json`/
  `nats_publish_jsonb` are unchanged by default - this is opt-in.

  **Measured, not just asserted: this does not fully close the original
  stall on its own.** Reproduced directly against the real stress condition
  that surfaced it (a heavy 32-`nats_sidecar`-instance teardown immediately
  followed by a high-burst single-consumer publish): a single flush call
  after a 200k-row batch still stalled short in 2 of 5 trials, no better
  than 1 of 5 with no flush at all - the loss can accumulate *during* a
  large in-flight burst, not just linger unflushed after the batch ends.
  Only flushing after *every single publish* reliably prevented the stall
  in reproduction (0 of 3 trials), at a real, severe cost: ~50-80x slower
  (200k rows, 0.25-0.4s unflushed vs 20s flushed every row). A coarser
  middle ground (flush every 500 rows, ~free at 0.43s for 200k rows) still
  stalled in 1 of 3 trials - not a safe default granularity either. Shipped
  anyway because it's a real, correctly-implemented primitive useful on its
  own terms for ordinary (non-pathological-burst) durability checkpoints
  (e.g. before a commit) - see its own doc comment
  (`src/api/nats.rs::nats_publish_flush`) for the full measured picture
  before relying on it as a complete fix for this specific extreme load
  shape.

## [1.1.4] - 2026-08-17

### Changed

* Raised the async publish/put paths' stop-and-wait batch size
  (`PENDING_STREAM_ACK_LIMIT`) from 1,000 to 3,000. `join_all()`-ing a
  batch of pending acks has a fixed cost that's amortized over more
  messages with a bigger batch. Measured, 2M real rows, single
  connection: 1,000 -> 210,637 rows/s, 2,000 -> 224,921, 3,000 ->
  230,443-235,183, 4,800 -> 233,754 (flat past 3,000, not worth trading
  away headroom under async-nats's 5,000 in-flight-ack semaphore for
  no further gain). Same measured gain on both the stream and KV async
  paths, since they share this batching mechanism.

  Also tried and rejected (not shipped, not in this diff): a true
  sliding window - await only the single oldest ack once the window
  fills, instead of the whole batch - on the theory that it would avoid
  blocking on a batch's own tail latency. Measured *worse* (~179,500
  rows/s, reproduced twice): the per-await overhead this stack pays
  on nearly every message in that design outweighs the bubble it
  was meant to remove. Batch size is the lever that works here, not
  batch *granularity*.

## [1.1.3] - 2026-08-17

### Changed

* pgnats's own Rust-side heap allocations (subject `String`s, payload
  `Vec<u8>`s, ack-tracking structures - not Postgres's `palloc`, which is
  untouched) now go through `mimalloc` instead of the system allocator.
  A `perf record -g` profile of the 1.1.1 async publish path (12M rows)
  showed ~11% of total CPU time in glibc allocator internals
  (`_int_malloc`/`_int_free_chunk`/`realloc`/`malloc_consolidate`/...) -
  consistent with the many small, short-lived per-row allocations the
  async publish/put paths make. Same tradeoff nats_asio already makes for
  its own build (`NATS_ASIO_USE_MIMALLOC`).

  Measured, single connection, real rows, both fully acked: stream async
  publish 207,900 -> 216,163 rows/s (+4%), KV async put 194,932 ->
  217,014 rows/s (**+11%**) - roughly tracking the allocator's measured
  share of the profile.

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
