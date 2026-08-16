# 📡 pgnats - PostgreSQL extension for NATS messaging

> **This Fork:** Updated for PostgreSQL 18 with complete build fixes, comprehensive documentation, and production deployment guides.
> - **Status:** ✅ Production Ready - All 31 tests passing
> - **Version:** 1.1.0
> - **Upstream:** [luxms/pgnats](https://github.com/luxms/pgnats)
> - **This Fork:** [mrayva/pgnats](https://github.com/mrayva/pgnats)

## 🚀 What's New in This Fork

- ✅ **PostgreSQL 18 Support** - Fixed all build errors for PG18
- ✅ **Complete Documentation** - 6 comprehensive guides covering everything
- ✅ **Background Workers** - Verified working with <5ms latency
- ✅ **Production Ready** - Deployment guides for Docker, K8s, and bare metal
- ✅ **All Tests Passing** - 31/31 tests verified and documented
- ✅ **Build Automation** - Simple `./build.sh` wrapper script

## 📚 Documentation (NEW)

**Quick Links:**
- **[QUICKSTART.md](QUICKSTART.md)** - Get started in 5 minutes ⚡
- **[BUILD_FIX_GUIDE.md](BUILD_FIX_GUIDE.md)** - Complete build instructions & troubleshooting
- **[TEST_RESULTS.md](TEST_RESULTS.md)** - All tests passing with examples
- **[BACKGROUND_WORKERS_GUIDE.md](BACKGROUND_WORKERS_GUIDE.md)** - Automatic message processing
- **[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)** - Docker, K8s, CI/CD, and production
- **[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)** - Current status & what's working

## 🎯 Quick Deploy

```bash
# Clone this fork
git clone https://github.com/mrayva/pgnats.git
cd pgnats

# Build (now with automated script)
./build.sh build --release

# Install
sudo chown -R $USER:$USER /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
./build.sh pgrx install --release
sudo chown -R root:root /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/

# Enable in PostgreSQL
psql -U postgres -d yourdb -c "CREATE EXTENSION pgnats;"
```

---

## Original README

Provides seamless integration between PostgreSQL and NATS messaging system,
enabling:

- Message publishing to core NATS subjects from SQL
- Subscriptions to NATS subjects that invoke PostgreSQL functions on incoming messages
- JetStream persistent message streams
- Key-Value storage operations from SQL
- Object Store operations (uploading, downloading, deleting files) from SQL
- Works on Postgres Cluster

## ⚙️ Install

See [INSTALL.md](INSTALL.md) for instructions on how to install required system dependencies.

## 🛠️ PostgreSQL Configure options

You can fine tune PostgreSQL build options:

```
cargo pgrx init --configure-flag='--without-icu'
```

## 📦 Build package

```sh
cargo pgrx package --pg-config <PATH TO PG_CONFIG> [--out-dir <THE DIRECTORY TO OUTPUT THE PACKAGE>]
```

### 🔧 Selecting Features

By default, all features (`kv`, `object_store`, `sub`) are enabled.
If you prefer a smaller build or want to customize the functionality, you can selectively enable features like so:

```sh
cargo pgrx package --no-default-features --features kv
```

This will include only the `kv` feature and exclude `object_store` and `sub`.

For example:

* `--features "kv"` – enables only the NATS key-value store.
* `--features "sub"` – enables subscriptions and HTTP integration with Patroni.
* `--features "object_store"` – enables binary object storage support.

You can combine them as needed:

```sh
cargo pgrx package --no-default-features --features kv sub
```

## 🧪 Tests

> [!WARNING]
> Before starting the test, NATS-Server should be started on a local host with port 4222.

> [!WARNING]
> You need docker installed for integration testing.

**Run all tests**
```sh
cargo pgrx test
```

### 🔁 Zerialize End-to-End Test

`scripts/zerialize_e2e_test.sh` publishes real Postgres rows through pgnats to
NATS in every [pg_zerialize](https://github.com/mrayva/pg_zerialize) binary
wire format (msgpack, cbor, zera, flexbuffers, ion, bson, beve), consumes
them back with [nats_asio](https://github.com/mrayva/nats_asio)'s `nats_tool`
(a decoder independent of pg_zerialize's own), and structurally compares the
result against pg_zerialize's own decode of the same rows -- proving the
full publish/transport/decode chain doesn't lose or corrupt anything, for
every format.

```sh
./scripts/zerialize_e2e_test.sh
```

Requires a running NATS server, pgnats and pg_zerialize both installed with
the foreign server configured (see Configuration above), and `nats_tool`
built (set `NATS_TOOL=<path>` if it isn't at the default
`~/nats_asio/build/bin/nats_tool`). Runs automatically in CI on every push
via `.github/workflows/zerialize-e2e.yml`.

### 🔎 Ad Hoc: Publish Any Query to NATS

`scripts/nats_publish_from_sql.py` is a general-purpose companion to the
fixed end-to-end test above: instead of a hardcoded fixture, point it at
any SELECT statement, choose which result columns build the per-row NATS
subject, and choose the pg_zerialize wire format. Not wired into CI (it's
for ad hoc testing against real tables), and requires `psycopg` (v3).

```sh
./scripts/nats_publish_from_sql.py \
    --sql 'SELECT * FROM nyse_eqy_us_all_trade_20260105' \
    --subject-columns 'Exchange,Symbol' \
    --format msgpack \
    --limit 1000 \
    --verify
```

Publishes each row's whole tuple, msgpack-encoded, to a subject built from
that row's own `Exchange`/`Symbol` values (e.g. `N.AAPL`). `--verify` spins
up an `nats_tool` consumer and cross-checks its decoded output against
pg_zerialize's own decode of the same rows, as an unordered multiset
(subject values built from arbitrary columns aren't necessarily unique per
row, so content -- not subject -- is what's compared). Run with `--help`
for the full option list (`--subject-prefix`, `--nats-topic`, `--dsn`,
timeouts, etc).

## 🦀 Minimum supported Rust version

- `Rust 1.82.0`

## 📚 Documentation

To view the documentation, run:

```sh
cargo doc --open
```

The exported PostgreSQL API is implemented in the `api` module.

## 📘 Usage

### ⚠️ Prerequisites

**Important:** pgnats must be loaded at PostgreSQL startup. Add it to `postgresql.conf`:

```conf
shared_preload_libraries = 'pgnats'
```

Then restart PostgreSQL:

```bash
sudo systemctl restart postgresql
```

Without this, you'll get `ERROR: PgLwLock was not initialized` when creating the server.

### ⚙️ Configuration

To configure the NATS connection, you need to create a Foreign Server:

```sql
CREATE SERVER nats_fdw_server FOREIGN DATA WRAPPER pgnats_fdw OPTIONS (
    --  IP/hostname of the NATS message server (default: 127.0.0.1)
    host 'localhost',

    -- TCP port for NATS connections (default: 4222)
    port '4222',

    -- Internal command buffer size in messages (default: 128)
    capacity '128',

    -- Path to the CA (Certificate Authority) certificate used to verify the NATS server certificate (default: unset, required for TLS)
    tls_ca_path '/path/ca',

    --  Path to the client certificate for mutual TLS authentication (default: unset; optional unless server requires client auth)
    tls_cert_path '/path/cert',

    -- Path to the client private key corresponding to nats.tls.cert (default: unset; required if nats.tls.cert is set)
    tls_key_path '/path/key',

    -- Name of the NATS subject for sending role change notifications (e.g., when the Postgres instance transitions between master and replica)
    notify_subject 'my.subject'

    -- URL of the Patroni REST API used to retrieve the current Postgres instance name.
    -- This is required when sending role change notifications (e.g., when the Postgres instance transitions between master and replica)
    patroni_url 'http://localhost:8008/patroni'
);
```

#### Notification body

```json
{
  "status": "Master",
  "listen_adresses": ["127.0.0.1", "127.0.0.2"],
  "port": 5432,
  "name": "pg-instance-01" // may be null
}
```

### 🔄 Reload configuration

```sql
-- Reload configuration (checks for changes)
SELECT pgnats_reload_conf();

-- Force reload configuration (no change checks)
SELECT pgnats_reload_conf_force();
```

### 📤 Publish

#### 🧊 Binary

```sql
-- Publish binary data to NATS
SELECT nats_publish_binary('sub.ject', 'binary data'::bytea);

-- Publish binary data with a reply subject
SELECT nats_publish_binary('sub.ject', 'binary data'::bytea, 'reply.subject');

-- Publish binary data with headers
SELECT nats_publish_binary(
  'sub.ject',
  'binary data'::bytea,
  NULL,
  '{}'::json
);

-- Publish binary data with both a reply subject and headers
SELECT nats_publish_binary(
  'sub.ject',
  'binary data'::bytea,
  'reply.subject',
  '{}'::json
);

-- Publish binary data via JetStream (sync)
SELECT nats_publish_binary_stream('sub.ject', 'binary data'::bytea);

-- Publish text via JetStream (sync) with headers
SELECT nats_publish_binary_stream(
  'sub.ject',
  'binary data'::bytea,
  '{}'::json
);
```

#### 📝 Utf-8 Text

```sql
-- Publish text to NATS
SELECT nats_publish_text('sub.ject', 'text data');

-- Publish text data with a reply subject
SELECT nats_publish_text('sub.ject', 'text data', 'reply.subject');

-- Publish text data with headers
SELECT nats_publish_text(
  'sub.ject',
  'text data',
  NULL,
  '{}'::json
);

-- Publish text data with both a reply subject and headers
SELECT nats_publish_text(
  'sub.ject',
  'text data',
  'reply.subject',
  '{}'::json
);

-- Publish text via JetStream (sync)
SELECT nats_publish_text('sub.ject', 'text data');

-- Publish text via JetStream (sync) with headers
SELECT nats_publish_text_stream(
  'sub.ject',
  'text data',
  '{}'::json
);
```

#### 📄 JSON

```sql
-- Publish JSON to NATS
SELECT nats_publish_json('sub.ject', '{}'::json);

-- Publish JSON data with a reply subject
SELECT nats_publish_json('sub.ject', '{"key": "value"}'::json, 'reply.subject');

-- Publish JSON data with headers
SELECT nats_publish_json(
  'sub.ject',
  '{"key": "value"}'::json,
  NULL,
  '{}'::json
);

-- Publish JSON data with both a reply subject and headers
SELECT nats_publish_json_reply(
  'sub.ject',
  '{"key": "value"}'::json,
  'reply.subject',
  '{}'::json
);

-- Publish JSON via JetStream (sync)
SELECT nats_publish_json_stream('sub.ject', '{}'::json);

-- Publish JSON via JetStream (sync) with headers
SELECT nats_publish_json_stream(
  'sub.ject',
  '{}'::json,
  '{}'::json
);
```

#### 🧱 Binary JSON

```sql
-- Publish binary JSON (JSONB) to NATS
SELECT nats_publish_jsonb('sub.ject', '{}'::json);

-- Publish JSONB data with a reply subject
SELECT nats_publish_jsonb('sub.ject', '{"key": "value"}'::jsonb, 'reply.subject');

-- Publish JSONB data with headers
SELECT nats_publish_jsonb(
  'sub.ject',
  '{"key": "value"}'::jsonb,
  NULL,
  '{}'::json
);

-- Publish JSONB data with both a reply subject and headers
SELECT nats_publish_jsonb_reply(
  'sub.ject',
  '{"key": "value"}'::jsonb,
  'reply.subject',
  '{}'::json
);

-- Publish binary JSON (JSONB) via JetStream (sync)
SELECT nats_publish_jsonb_stream('sub.ject', '{}'::jsonb);

--  Publish binary JSON (JSONB) via JetStream (sync) with headers
SELECT nats_publish_jsonb_stream(
  'sub.ject',
  '{}'::jsonb,
  '{}'::json
);
```

### 📡 Subscribe to Subjects

> [!WARNING]
> The specified PostgreSQL function **must accept a single argument of type `bytea`**, which contains the message payload from NATS.

```sql
-- Subscribe a PostgreSQL function to a NATS subject
SELECT nats_subscribe('events.user.created', 'schema.handle_user_created'::regproc);

-- Multiple functions can be subscribed to the same subject
SELECT nats_subscribe('events.user.created', 'schema.log_user_created'::regproc);

-- Unsubscribe a specific PostgreSQL function from a NATS subject
SELECT nats_unsubscribe('events.user.created', 'schema.handle_user_created'::regproc);
```

#### Subscription Architecture

![Subscription Architecture](./docs/bgw_sub.svg)

### 📥 Request

```sql
-- Request binary data from NATS (wait for response with timeout in ms)
SELECT nats_request_binary('sub.ject', 'binary request'::bytea, 1000);

-- Request text from NATS (wait for response with timeout in ms)
SELECT nats_request_text('sub.ject', 'text request', 1000);

-- Request JSON from NATS (wait for response with timeout in ms)
SELECT nats_request_json('sub.ject', '{"query": "value"}'::json, 1000);

-- Request binary JSON (JSONB) from NATS (wait for response with timeout in ms)
SELECT nats_request_jsonb('sub.ject', '{"query": "value"}'::jsonb, 1000);
```

### 🗃️ Key-Value Storage

```sql
-- Store binary data in NATS JetStream KV storage with specified key
SELECT nats_put_binary('bucket', 'key', 'binary data'::bytea);

-- Store text data in NATS JetStream KV storage with specified key
SELECT nats_put_text('bucket', 'key', 'text data');

-- Store binary JSON (JSONB) data in NATS JetStream KV storage with specified key
SELECT nats_put_jsonb('bucket', 'key', '{}'::jsonb);

-- Store JSON data in NATS JetStream KV storage with specified key
SELECT nats_put_json('bucket', 'key', '{}'::json);

-- Retrieve binary data by key from specified bucket
SELECT nats_get_binary('bucket', 'key');

-- Retrieve text data by key from specified bucket
SELECT nats_get_text('bucket', 'key');

-- Retrieve binary JSON (JSONB) by key from specified bucket
SELECT nats_get_jsonb('bucket', 'key');

-- Retrieve JSON by key from specified bucket
SELECT nats_get_json('bucket', 'key');

-- Delete value associated with specified key from bucket
SELECT nats_delete_value('bucket', 'key');
```

### 🗂️ Object Storage

```sql
-- Upload file content to NATS Object Store under a given name
SELECT nats_put_file('store', 'file_name.txt', 'file content'::bytea);

-- Download file content from NATS Object Store by name
SELECT nats_get_file('store', 'file_name.txt');

-- Delete a file from the NATS Object Store by name
SELECT nats_delete_file('store', 'file_name.txt');

-- Get metadata for a specific file in the Object Store
SELECT * FROM nats_get_file_info('store', 'file_name.txt');

-- List all files in a given NATS Object Store
SELECT * FROM nats_get_file_list('store');
```

### 🛠️ Utils

```sql
-- Get the current extension information about version
SELECT pgnats_version();

-- Retrieves information about the NATS server connection.
SELECT * FROM nats_get_server_info();
```
