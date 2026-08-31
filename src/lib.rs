//! # PGNats
//!
//! Provides seamless integration between PostgreSQL and NATS messaging system,
//! enabling:
//! Provides one-way integration from PostgreSQL to NATS, supporting:
//! - Message publishing to core NATS subjects from SQL
//! - Subscriptions to NATS subjects that invoke PostgreSQL functions on incoming messages
//! - JetStream persistent message streams
//! - Key-Value storage operations from SQL
//! - Object Store operations (uploading, downloading, deleting files) from SQL
//! - Works on Postgres Cluster
//!
//! ### Feature Flags
//!
//! This extension uses a set of feature flags to enable optional functionality while keeping dependencies manageable. By default, **all features are enabled**, providing full integration with NATS and support for HTTP-based communication with [Patroni](https://github.com/zalando/patroni).
//!
//! Below is a list of available feature flags and what they enable:
//! - **kv** - Enables integration with the NATS key-value store. Useful for distributed configuration and metadata storage.
//! - **object_store** - Enables support for the NATS object store, allowing storage and retrieval of large binary blobs.
//! - **sub** - Enables NATS subscription handling.
//!
//! If you need a minimal setup, you can disable default features and enable only the ones required.

::pgrx::pg_module_magic!();

// Scoped to this crate's own Rust-side heap traffic only (String/Vec/etc.
// allocated by pgnats's own code, via Rust's #[global_allocator] hook) -
// does not touch Postgres's own palloc/memory-context allocator, which is
// a separate system entirely. A profiled run of the async publish path
// (perf record -g, 12M rows) showed ~11% of total CPU time in glibc's
// allocator internals (_int_malloc/_int_free_chunk/realloc/
// malloc_consolidate/...) - consistent with many small, short-lived
// per-row allocations (subject Strings, payload Vecs, ack-tracking
// structures) that a general-purpose allocator like ptmalloc pays real
// per-call overhead for. mimalloc is designed for exactly this pattern
// (thread-local free lists, low per-call overhead) - the same tradeoff
// nats_asio already made for its own build (NATS_ASIO_USE_MIMALLOC).
// Initializes once per backend process, before that backend's own
// worker threads (pgnats's thread_local Tokio runtime) are spun up, so
// this doesn't interact with Postgres's fork() model any differently
// than any other per-backend global state already does.
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

mod pg_tests;

mod init;
mod log;
mod utils;

pub mod api;

#[doc(hidden)]
#[cfg(feature = "sub")]
pub mod bgw;

#[doc(hidden)]
pub mod config;

#[doc(hidden)]
pub mod nats_client;

#[doc(hidden)]
pub mod constants;

#[doc(hidden)]
pub mod ctx;

/// This module is required by `cargo pgrx test` invocations.
/// It must be visible at the root of your extension crate.
#[cfg(test)]
pub mod pg_test {
    // Without this, the test cluster's postmaster reliably fails to start:
    // "cannot allocate memory in static TLS block" loading pgnats.so.
    // Root cause: this crate's #[global_allocator] (mimalloc, see lib.rs)
    // emits large initial-exec-model thread-locals for its per-thread heap
    // state - glibc's dynamic loader only reserves a small "static TLS
    // surplus" for libraries dlopen()'d after process startup (exactly how
    // Postgres loads shared_preload_libraries/extensions), and mimalloc's
    // block doesn't fit. Confirmed via a minimal manual repro (bare initdb
    // + shared_preload_libraries='pgnats' + pg_ctl start, no pgrx/cargo
    // involved at all) - fails identically, and setting this exact
    // GLIBC_TUNABLES fixes it. This must be set here (before `run_test()`
    // spawns pg_ctl as a child of this process, which inherits it) rather
    // than as a one-off environment tweak, since it's a property of this
    // crate's own choice of global allocator, not of any particular
    // machine or CI runner.
    pub fn setup(_options: Vec<&str>) {
        std::env::set_var("GLIBC_TUNABLES", "glibc.rtld.optional_static_tls=4096");
    }

    #[must_use]
    pub fn postgresql_conf_options() -> Vec<&'static str> {
        vec![
            "shared_preload_libraries='pgnats'",
            "max_worker_processes = 32",
        ]
    }
}
