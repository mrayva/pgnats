use std::{collections::HashMap, io::Cursor, time::Duration};

use async_nats::{
    jetstream::{
        kv::Store,
        object_store::{ObjectInfo, ObjectStore},
        Context,
    },
    Client, Request,
};

use futures::StreamExt;
use tokio::io::{AsyncReadExt, BufReader};

use crate::{
    config::{Config, NatsTlsOptions},
    utils::{extract_headers, resolve_config_path, FromBytes, ToBytes},
};

// Kept comfortably under async-nats's own default max-in-flight-acks
// semaphore capacity (5,000, jetstream::context::ContextBuilder's
// semaphore_capacity default) - draining here is what returns permits to
// that semaphore, so this bound has to be reached well before the
// semaphore itself would be exhausted, or publish_stream_async() would
// block waiting on a permit nothing is freeing. See
// NatsClient::publish_stream_async()'s doc for what happens if it isn't.
const PENDING_STREAM_ACK_LIMIT: usize = 1_000;

/// Awaits a batch of pipelined JetStream acks concurrently, reporting every
/// failure (not just the first) so a caller sees the full extent of a bad
/// batch instead of just its first row.
async fn drain_stream_acks(
    pending: Vec<async_nats::jetstream::context::PublishAckFuture>,
) -> anyhow::Result<u64> {
    let count = pending.len() as u64;

    let results =
        futures::future::join_all(pending.into_iter().map(std::future::IntoFuture::into_future))
            .await;

    let errors: Vec<String> = results
        .into_iter()
        .enumerate()
        .filter_map(|(i, r)| r.err().map(|e| format!("#{i}: {e}")))
        .collect();

    if !errors.is_empty() {
        anyhow::bail!(
            "{} of {} pipelined JetStream publishes failed to ack: {}",
            errors.len(),
            count,
            errors.join("; ")
        );
    }

    Ok(count)
}

pub struct NatsClient {
    connection: Option<Client>,
    jetstream: Option<Context>,
    cached_buckets: HashMap<String, Store>,
    cached_object_stores: HashMap<String, ObjectStore>,
    current_config: Option<Config>,
    config_fetcher: fn() -> Config,
    // Acks pipelined by publish_stream_async(), awaited (and verified) by
    // flush_stream_acks(). async_nats's own PublishAckFuture::drop() hands
    // pending acks off to a background "acker" task that silently discards
    // both the actual result and any error (see spawn_acker() in
    // async-nats's jetstream/context.rs: `timeout(..., subscription).await.ok()`
    // then unconditionally drops the permit) - that's exactly what
    // publish_stream() does today by discarding the future it gets back.
    // Keeping the future alive here instead is what makes a later flush
    // able to see a real failure rather than silently losing the row.
    pending_stream_acks: Vec<async_nats::jetstream::context::PublishAckFuture>,
}

impl NatsClient {
    pub fn new(config: Option<Config>, config_fetcher: fn() -> Config) -> Self {
        Self {
            current_config: config,
            config_fetcher,
            connection: None,
            jetstream: None,
            cached_buckets: HashMap::new(),
            cached_object_stores: HashMap::new(),
            pending_stream_acks: Vec::new(),
        }
    }

    pub async fn publish(
        &mut self,
        subject: impl ToString,
        message: impl ToBytes,
        reply: Option<impl ToString>,
        headers: Option<serde_json::Value>,
    ) -> anyhow::Result<()> {
        let subject = subject.to_string();
        let message: Vec<u8> = message.to_bytes()?;
        let conn = self.get_connection().await?;
        let headers = headers.map(extract_headers).transpose()?;

        if let Some(reply) = reply {
            let reply = reply.to_string();

            if let Some(headers) = headers {
                conn.publish_with_reply_and_headers(subject, reply, headers, message.into())
                    .await?;
            } else {
                conn.publish_with_reply(subject, reply, message.into())
                    .await?;
            }
        } else if let Some(headers) = headers {
            conn.publish_with_headers(subject, headers, message.into())
                .await?;
        } else {
            conn.publish(subject, message.into()).await?;
        }

        Ok(())
    }

    pub async fn request(
        &mut self,
        subject: impl ToString,
        message: impl ToBytes,
        timeout: Option<u64>,
    ) -> anyhow::Result<Vec<u8>> {
        let subject = subject.to_string();
        let message: Vec<u8> = message.to_bytes()?;

        let request = Request::new().payload(message.into());

        let request = if let Some(timeout) = timeout {
            request.timeout(Some(Duration::from_millis(timeout)))
        } else {
            request
        };

        let result = self
            .get_connection()
            .await?
            .send_request(subject, request)
            .await?;

        Ok(result.payload.to_vec())
    }

    /// Publishes and waits for this message's actual JetStream ack.
    ///
    /// `Context::publish()`/`publish_with_headers()` don't themselves wait
    /// for the ack - they return a `PublishAckFuture` that must be awaited
    /// a second time to get it. This used to be discarded here (`let _ =
    /// js.publish(...).await?`), which only confirmed the message was
    /// handed to the connection, not that the stream accepted it -
    /// dropping a `PublishAckFuture` hands it to async-nats's own
    /// background "acker" task (see `spawn_acker` in
    /// `async-nats::jetstream::context`), which waits for the ack (or
    /// times out) and then unconditionally discards the result either way.
    /// A failed or timed-out ack was therefore never surfaced here despite
    /// this function's own doc promising "JetStream persistence and
    /// delivery guarantees."
    pub async fn publish_stream(
        &mut self,
        subject: impl ToString,
        message: impl ToBytes,
        headers: Option<serde_json::Value>,
    ) -> anyhow::Result<()> {
        let subject = subject.to_string();
        let message: Vec<u8> = message.to_bytes()?;
        let headers = headers.map(extract_headers).transpose()?;
        let js = self.get_jetstream().await?;

        let ack = if let Some(headers) = headers {
            js.publish_with_headers(subject, headers, message.into())
                .await?
        } else {
            js.publish(subject, message.into()).await?
        };
        ack.await?;

        Ok(())
    }

    /// Like [`Self::publish_stream`], but returns as soon as the message is
    /// handed to the connection instead of waiting for this one message's
    /// JetStream ack. The ack is not discarded, though - it's queued in
    /// `pending_stream_acks` so [`Self::flush_stream_acks`] can verify it
    /// later. Callers must flush before treating the publish as durable;
    /// nothing here calls flush automatically.
    ///
    /// async-nats gates in-flight (unacked) publishes on its own semaphore,
    /// default capacity 5,000 - `Context::publish()`'s returned
    /// `PublishAckFuture` only releases its permit when it's either awaited
    /// or dropped (dropping hands it to async-nats's own background
    /// "acker" task, which awaits it on our behalf). Holding every future
    /// in `pending_stream_acks` without ever awaiting or dropping any of
    /// them - which is what this function did before this comment was
    /// added - starves that release path entirely: past the semaphore's
    /// capacity, every subsequent publish blocks forever on
    /// `acquire_owned().await`, and that block happens inside a foreign
    /// Rust future `block_on()`'d from C, which is not a point Postgres's
    /// own interrupt handling can reach - confirmed directly, not
    /// theoretical: `pg_cancel_backend`/`pg_terminate_backend` both failed
    /// to stop a backend wedged this way, and recovering it took `kill -9`
    /// on the backend process, which is a hard, unclean stop. Draining well
    /// under that 5,000 capacity here is what keeps this function from
    /// ever being able to reach that state.
    pub async fn publish_stream_async(
        &mut self,
        subject: impl ToString,
        message: impl ToBytes,
        headers: Option<serde_json::Value>,
    ) -> anyhow::Result<()> {
        let subject = subject.to_string();
        let message: Vec<u8> = message.to_bytes()?;
        let headers = headers.map(extract_headers).transpose()?;
        let js = self.get_jetstream().await?;

        let ack = if let Some(headers) = headers {
            js.publish_with_headers(subject, headers, message.into())
                .await?
        } else {
            js.publish(subject, message.into()).await?
        };

        self.queue_ack(ack).await
    }

    /// Like [`Self::put_value`], but pipelined the same way
    /// [`Self::publish_stream_async`] is - queues the ack instead of
    /// waiting for it, shares the same `pending_stream_acks` queue and
    /// [`Self::flush_stream_acks`].
    ///
    /// `kv::Store::put()` is, itself, nothing more than
    /// `self.stream.context.publish(subject, value).await?.await?` -  the
    /// exact same `Context::publish()` this uses for
    /// `publish_stream_async()`, just with the subject built from the
    /// bucket's key-value prefix instead of a caller-given subject. `Store`
    /// doesn't expose a way to get the first await's `PublishAckFuture`
    /// without also driving the second, so this rebuilds that subject
    /// itself from `Store`'s own public `prefix`/`put_prefix` fields (the
    /// same two `put()` reads) and publishes through this client's own
    /// already-held `Context` rather than `Store`'s private one - both are
    /// contexts on the same underlying connection, so this is equivalent,
    /// not a workaround.
    ///
    /// Two things `Store::put()` does that this does *not*: it doesn't
    /// validate the key (`kv::is_valid_key()` isn't reachable from outside
    /// the crate - only its regex-backed rules are lost, not enforcement:
    /// a key that's empty or has a leading/trailing `.` would build a
    /// syntactically different subject than intended instead of failing
    /// fast, so callers must pre-sanitize keys the same way this session's
    /// subject-building already does for stream subjects), and it doesn't
    /// support `use_jetstream_prefix` (JetStream domains) - both fine for
    /// the common case (this deployment included) but worth knowing if
    /// either ever changes.
    pub async fn put_value_async(
        &mut self,
        bucket: impl ToString,
        key: impl AsRef<str>,
        data: impl ToBytes,
    ) -> anyhow::Result<()> {
        let data: Vec<u8> = data.to_bytes()?;
        let subject = {
            let store = self.get_or_create_bucket(bucket).await?;
            let mut subject = String::with_capacity(store.prefix.len() + key.as_ref().len());
            subject.push_str(store.put_prefix.as_ref().unwrap_or(&store.prefix));
            subject.push_str(key.as_ref());
            subject
        };
        let js = self.get_jetstream().await?;
        let ack = js.publish(subject, data.into()).await?;

        self.queue_ack(ack).await
    }

    async fn queue_ack(
        &mut self,
        ack: async_nats::jetstream::context::PublishAckFuture,
    ) -> anyhow::Result<()> {
        self.pending_stream_acks.push(ack);

        if self.pending_stream_acks.len() >= PENDING_STREAM_ACK_LIMIT {
            let pending = std::mem::take(&mut self.pending_stream_acks);
            drain_stream_acks(pending).await?;
        }

        Ok(())
    }

    /// Awaits every ack queued by [`Self::publish_stream_async`]/
    /// [`Self::put_value_async`] on this connection since the last flush
    /// (whether that was an explicit call here or an automatic drain once
    /// `PENDING_STREAM_ACK_LIMIT` was reached), concurrently. Returns the
    /// number flushed by *this* call - not a running total, and not
    /// necessarily every publish since the backend connected, since an
    /// automatic drain may already have flushed and cleared earlier ones.
    pub async fn flush_stream_acks(&mut self) -> anyhow::Result<u64> {
        let pending = std::mem::take(&mut self.pending_stream_acks);
        drain_stream_acks(pending).await
    }

    pub async fn invalidate_connection(&mut self) {
        let connection = { self.connection.take() };

        {
            self.cached_buckets.clear();
            let _ = self.jetstream.take();
            let _ = self.current_config.take();
        }

        if let Some(conn) = connection {
            let _ = conn.drain().await;
        }
    }

    pub async fn check_and_invalidate_connection(&mut self, new_config: Config) {
        let (changed, new_config) = {
            let config = &self.current_config;

            let changed = config.as_ref().map(|c| &c.nats_opt) != Some(&new_config.nats_opt);

            (changed, new_config)
        };

        if changed {
            self.invalidate_connection().await;

            self.current_config = Some(new_config);
        }
    }

    pub async fn put_value(
        &mut self,
        bucket: impl ToString,
        key: impl AsRef<str>,
        data: impl ToBytes,
    ) -> anyhow::Result<u64> {
        let bucket = self.get_or_create_bucket(bucket).await?;
        let data: Vec<u8> = data.to_bytes()?;
        let version = bucket.put(key, data.into()).await?;

        Ok(version)
    }

    pub async fn get_value<T: FromBytes>(
        &mut self,
        bucket: impl ToString,
        key: impl Into<String>,
    ) -> anyhow::Result<Option<T>> {
        let bucket = self.get_or_create_bucket(bucket).await?;

        bucket
            .get(key)
            .await?
            .map(|d| d.to_vec())
            .map(T::from_bytes)
            .transpose()
    }

    pub async fn delete_value(
        &mut self,
        bucket: impl ToString,
        key: impl AsRef<str>,
    ) -> anyhow::Result<()> {
        let bucket = self.get_or_create_bucket(bucket).await?;
        bucket.delete(key).await?;

        Ok(())
    }

    pub async fn get_server_info(&mut self) -> anyhow::Result<async_nats::ServerInfo> {
        let connection = self.get_connection().await?;
        Ok(connection.server_info())
    }

    pub async fn get_file(
        &mut self,
        store: impl ToString,
        name: impl AsRef<str> + Send,
    ) -> anyhow::Result<Vec<u8>> {
        let store = self.get_or_create_object_store(store).await?;
        let mut file = store.get(name).await?;

        let mut content = Vec::with_capacity(file.info().size);
        let _ = file.read_to_end(&mut content).await?;

        Ok(content)
    }

    pub async fn put_file(
        &mut self,
        store: impl ToString,
        name: impl AsRef<str>,
        content: Vec<u8>,
    ) -> anyhow::Result<()> {
        let store = self.get_or_create_object_store(store).await?;
        let mut reader = BufReader::new(Cursor::new(content));
        let _ = store.put(name.as_ref(), &mut reader).await?;

        Ok(())
    }

    pub async fn delete_file(
        &mut self,
        store: impl ToString,
        name: impl AsRef<str>,
    ) -> anyhow::Result<()> {
        let store = self.get_or_create_object_store(store).await?;
        store.delete(name).await.map_err(|e| e.into())
    }

    pub async fn get_file_info(
        &mut self,
        store: impl ToString,
        name: impl AsRef<str>,
    ) -> anyhow::Result<ObjectInfo> {
        let store = self.get_or_create_object_store(store).await?;
        store.info(name).await.map_err(|e| e.into())
    }

    pub async fn get_file_list(&mut self, store: impl ToString) -> anyhow::Result<Vec<ObjectInfo>> {
        let store = self.get_or_create_object_store(store).await?;
        let mut vec = vec![];
        let mut list = store.list().await?;

        while let Some(object) = list.next().await {
            vec.push(object?);
        }

        Ok(vec)
    }
}

impl NatsClient {
    #[allow(clippy::expect_used)]
    async fn get_connection(&mut self) -> anyhow::Result<&Client> {
        if self.connection.is_none() {
            self.initialize_connection().await?;
        }

        Ok(self
            .connection
            .as_ref()
            .expect("unreachable, must be initialized"))
    }

    #[allow(clippy::expect_used)]
    async fn get_jetstream(&mut self) -> anyhow::Result<&Context> {
        if self.connection.is_none() {
            self.initialize_connection().await?;
        }

        Ok(self
            .jetstream
            .as_ref()
            .expect("unreachable, must be initialized"))
    }

    #[allow(clippy::expect_used)]
    async fn get_or_create_bucket(&mut self, bucket: impl ToString) -> anyhow::Result<&Store> {
        let bucket = bucket.to_string();

        if !self.cached_buckets.contains_key(&bucket) {
            let new_store = {
                let jetstream = self.get_jetstream().await?;

                if let Ok(store) = jetstream.get_key_value(&bucket).await {
                    store
                } else {
                    jetstream
                        .create_key_value(async_nats::jetstream::kv::Config {
                            bucket: bucket.clone(),
                            ..Default::default()
                        })
                        .await?
                }
            };

            let _ = self.cached_buckets.insert(bucket.clone(), new_store);
        }

        Ok(self
            .cached_buckets
            .get(&bucket)
            .expect("unreachable, must be initialized"))
    }

    #[allow(clippy::expect_used)]
    async fn get_or_create_object_store(
        &mut self,
        store: impl ToString,
    ) -> anyhow::Result<&ObjectStore> {
        let bucket = store.to_string();

        if !self.cached_object_stores.contains_key(&bucket) {
            let new_store = {
                let jetstream = self.get_jetstream().await?;

                if let Ok(store) = jetstream.get_object_store(&bucket).await {
                    store
                } else {
                    jetstream
                        .create_object_store(async_nats::jetstream::object_store::Config {
                            bucket: bucket.clone(),
                            ..Default::default()
                        })
                        .await?
                }
            };

            let _ = self.cached_object_stores.insert(bucket.clone(), new_store);
        }

        Ok(self
            .cached_object_stores
            .get(&bucket)
            .expect("unreachable, must be initialized"))
    }

    async fn initialize_connection(&mut self) -> anyhow::Result<()> {
        let config = self.current_config.get_or_insert_with(self.config_fetcher);

        let mut opts = async_nats::ConnectOptions::new().client_capacity(config.nats_opt.capacity);

        if let Some(tls) = &config.nats_opt.tls {
            match tls {
                NatsTlsOptions::Tls { ca } => {
                    opts = opts
                        .require_tls(true)
                        .add_root_certificates(resolve_config_path(ca)?)
                }
                NatsTlsOptions::MutualTls { ca, cert, key } => {
                    opts = opts
                        .require_tls(true)
                        .add_root_certificates(resolve_config_path(ca)?)
                        .add_client_certificate(
                            resolve_config_path(cert)?,
                            resolve_config_path(key)?,
                        );
                }
            }
        }

        let connection = opts
            .connect(format!(
                "{0}:{1}",
                config.nats_opt.host, config.nats_opt.port
            ))
            .await
            .inspect_err(|_| {
                self.current_config = None;
            })?;

        let mut jetstream = async_nats::jetstream::new(connection.clone());
        jetstream.set_timeout(std::time::Duration::from_secs(5));

        self.connection = Some(connection);
        self.jetstream = Some(jetstream);

        Ok(())
    }
}
