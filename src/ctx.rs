use std::cell::RefCell;

use crate::{config::fetch_config, constants::FDW_EXTENSION_NAME, nats_client::NatsClient};

thread_local! {
    pub static CTX: RefCell<Context> = RefCell::new(create_context());
}

pub struct Context {
    pub nats_connection: NatsClient,
    pub rt: tokio::runtime::Runtime,
}

// The extension is useless without tokio runtime. It has to panic if the runtime cannot be initialized.
// pgrx will handle the panic properly.
//
// worker_threads(2), not the default (one thread per CPU core): this
// runtime is thread_local per Postgres backend (CTX above), and
// new_multi_thread()'s default spun up 25 OS threads per backend on this
// 24-core dev box (confirmed via /proc/<pid>/task) - with many concurrent
// backends each paying that cost, real oversubscription (e.g. 16 backends
// x 25 threads = 400 OS threads competing for 24 cores), measurably
// hurting aggregate publish throughput under connection-level
// parallelism. Switching to new_current_thread() (0 background threads)
// was tried first and made things far worse, not better - single-
// connection throughput dropped ~9x - because nothing then drives the
// async-nats client's write/flush loop between our synchronous
// block_on() calls, so each publish() effectively pays a full
// synchronous flush. A small fixed worker_threads(2) keeps that
// background-thread-driven reactor behavior (so throughput is unaffected
// at low concurrency) while still cutting the oversubscription drastically
// at high connection counts.
#[allow(clippy::expect_used)]
fn create_context() -> Context {
    Context {
        nats_connection: NatsClient::new(None, || fetch_config(FDW_EXTENSION_NAME)),
        rt: tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .expect("Failed to initialize Tokio runtime"),
    }
}
