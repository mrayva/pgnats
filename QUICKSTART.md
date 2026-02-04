# PGNats Quick Start Guide

## TL;DR - Build & Install

```bash
# Build
./build.sh build

# Test
sudo chown -R $USER:$USER /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
./build.sh test
sudo chown -R root:root /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/

# Install
sudo chown -R $USER:$USER /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
./build.sh pgrx install
sudo chown -R root:root /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
```

## Enable in PostgreSQL

```sql
CREATE EXTENSION pgnats;
\dx pgnats
```

## Quick Examples

```sql
-- Store and retrieve text
SELECT pgnats.put_text('mykey', 'Hello, World!');
SELECT pgnats.get_text('mykey');

-- Store and retrieve JSON
SELECT pgnats.put_json('config', '{"name": "test"}'::json);
SELECT pgnats.get_json('config');

-- Publish a message
SELECT pgnats.publish('news', 'Breaking news!');

-- Upload a file
SELECT pgnats.put_file('bucket', 'data.txt', 'contents'::bytea);
SELECT pgnats.get_file('bucket', 'data.txt');
```

## Features

- **Key-Value Store**: Text, JSON, JSONB, Binary data storage
- **Object Store**: File upload/download/list/delete operations
- **Pub/Sub**: NATS messaging with publish/subscribe/request-reply
- **Background Workers**: Automated message handling via FDW

## Full Documentation

See [BUILD_FIX_GUIDE.md](BUILD_FIX_GUIDE.md) for:
- Detailed build instructions
- Troubleshooting guide
- Complete API reference
- All fixes and changes made

## Requirements

- PostgreSQL 18
- Rust toolchain
- pgrx with PostgreSQL 18 support

## Notes

This fork includes fixes for:
- PostgreSQL 18 compatibility
- C compilation issues (cee-scape)
- Unsafe code block declarations
- Build automation via `build.sh`

**Repository:** https://github.com/mrayva/pgnats
**Upstream:** https://github.com/luxms/pgnats
