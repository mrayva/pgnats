# PGNats Build Fix Guide

This document describes all the issues encountered when building pgnats and the fixes applied to make it work with PostgreSQL 18.

## Issues Encountered

### 1. C Compilation Error (cee-scape dependency)

**Error:**
```
error: 'sigjmp_buf' undeclared (first use in this function)
```

**Root Cause:** The `cee-scape` dependency was being compiled with `-std=c11` which doesn't expose POSIX extensions like `sigjmp_buf`.

**Fix:** Use `-std=gnu11` instead to enable GNU/POSIX extensions.

### 2. PostgreSQL Version Mismatch

**Error:**
```
Error: Postgres `pg14` is not managed by pgrx
```

**Root Cause:** The project defaulted to PostgreSQL 14, but only PostgreSQL 18 was installed in pgrx.

**Fix:** Updated default feature from `pg14` to `pg18` in `Cargo.toml`.

### 3. Syntax Errors in Static Declarations

**Error:**
```
error: expected `;`, found keyword `pub`
error[E0133]: call to unsafe function `PgLwLock::<T>::new` is unsafe
```

**Root Cause:** Static declarations with `PgLwLock::new()` calls needed to be wrapped in `unsafe` blocks and required proper semicolons.

**Fixes Applied:**
- `src/bgw/mod.rs`: Wrapped `LAUNCHER_MESSAGE_BUS` initialization in `unsafe` block
- `src/pg_tests/macros.rs`: Wrapped macro-generated static initializations in `unsafe` blocks

### 4. Unused Import

**Error:**
```
error: unused import: `PgSharedMemoryInitialization`
```

**Fix:** Removed unused import from `src/pg_tests/bgw_tests.rs`.

## Files Modified

### 1. `Cargo.toml`
```toml
# Changed from:
default = ["pg14", "kv", "object_store", "sub"]

# To:
default = ["pg18", "kv", "object_store", "sub"]
```

### 2. `.cargo/config.toml`
Added CFLAGS environment variable:
```toml
[env]
CFLAGS = "-std=gnu11"
```

### 3. `src/bgw/mod.rs`
```rust
// Fixed static declaration (line 66-69)
pub static LAUNCHER_MESSAGE_BUS: PgLwLock<RingQueue<MESSAGE_BUS_SIZE>> = unsafe {
    PgLwLock::new(c"pgnats_launcher_message_bus")
};
```

### 4. `src/pg_tests/macros.rs`
```rust
// Fixed macro-generated statics (lines 6-11)
#[allow(non_upper_case_globals)]
pub(super) static [<LAUNCHER_MESSAGE_BUS $n>]: pgrx::PgLwLock<$crate::bgw::ring_queue::RingQueue<1024>> = unsafe {
    pgrx::PgLwLock::new($launcher_name)
};

#[allow(non_upper_case_globals)]
pub(super) static [<TEST_RESULT $n>]: pgrx::PgLwLock<u64> = unsafe {
    pgrx::PgLwLock::new($result_name)
};
```

### 5. `src/pg_tests/bgw_tests.rs`
```rust
// Removed unused import (line 3)
// Before:
use pgrx::{PgSharedMemoryInitialization, pg_guard, pg_shmem_init, pg_sys};

// After:
use pgrx::{pg_guard, pg_shmem_init, pg_sys};
```

### 6. `build.sh` (New File)
Created a wrapper script to ensure CFLAGS are properly set:
```bash
#!/bin/bash
# Build wrapper script to set correct CFLAGS for pgnats
export CFLAGS="-std=gnu11"
exec cargo "$@"
```

## Building the Project

### Prerequisites
- PostgreSQL 18 installed
- pgrx initialized with PostgreSQL 18: `cargo pgrx init --pg18 /path/to/pg18`
- Rust toolchain installed

### Build Commands

**Option 1: Using the wrapper script (recommended)**
```bash
./build.sh build
./build.sh test
./build.sh pgrx install
```

**Option 2: Setting CFLAGS manually**
```bash
export CFLAGS="-std=gnu11"
cargo build
cargo test
cargo pgrx install
```

## Running Tests

Tests require write permissions to PostgreSQL system directories.

**Full test procedure:**
```bash
# Grant temporary permissions
sudo chown -R $USER:$USER /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/

# Run tests
./build.sh test

# Restore permissions
sudo chown -R root:root /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
```

**Test Results:**
- ✅ 31/31 tests passed
- 9 unit tests (ring queue tests)
- 22 PostgreSQL integration tests (API, background workers, shared memory)

## Installing the Extension

### Installation Steps

```bash
# Grant temporary permissions (if needed)
sudo chown -R $USER:$USER /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/

# Install the extension
./build.sh pgrx install --pg-config /usr/lib/postgresql/18/bin/pg_config

# Restore permissions
sudo chown -R root:root /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
```

### Installed Files

After installation, the following files are created:
- `/usr/lib/postgresql/18/lib/pgnats.so` - Shared library (140MB)
- `/usr/share/postgresql/18/extension/pgnats.control` - Extension control file
- `/usr/share/postgresql/18/extension/pgnats--1.1.0.sql` - Main SQL script (18KB)
- `/usr/share/postgresql/18/extension/pgnats--1.0.0--1.1.0.sql` - Upgrade script (1.2KB)

## Using the Extension

### Enable in PostgreSQL

```sql
-- Connect to your database
psql -U postgres -d your_database

-- Create the extension
CREATE EXTENSION pgnats;

-- Verify installation
\dx pgnats

-- List available functions
\df pgnats.*
```

### Extension Features

The extension provides 33 functions across three main categories:

#### 1. Key-Value Storage (kv)
- `pgnats.put_text(key, value)` - Store text data
- `pgnats.get_text(key)` - Retrieve text data
- `pgnats.put_json(key, value)` - Store JSON data
- `pgnats.get_json(key)` - Retrieve JSON data
- `pgnats.put_jsonb(key, value)` - Store JSONB data
- `pgnats.get_jsonb(key)` - Retrieve JSONB data
- `pgnats.put_binary(key, value)` - Store binary data
- `pgnats.get_binary(key)` - Retrieve binary data
- `pgnats.delete_value(key)` - Delete a key-value pair

#### 2. Object Store (object_store)
- `pgnats.put_file(bucket, object_name, content)` - Upload a file
- `pgnats.get_file(bucket, object_name)` - Download a file
- `pgnats.delete_file(bucket, object_name)` - Delete a file
- `pgnats.file_info(bucket, object_name)` - Get file metadata
- `pgnats.file_list(bucket)` - List files in a bucket

#### 3. Pub/Sub (sub)
- `pgnats.publish(subject, message)` - Publish a message
- `pgnats.publish_stream(stream, subject, message)` - Publish to a stream
- `pgnats.publish_with_reply_and_headers(...)` - Publish with reply-to and headers
- `pgnats.request(subject, message)` - Send request and wait for reply
- Background worker support via Foreign Data Wrappers (FDW)

### Example Usage

```sql
-- Key-Value operations
SELECT pgnats.put_text('mykey', 'Hello, World!');
SELECT pgnats.get_text('mykey');

-- JSON operations
SELECT pgnats.put_json('config', '{"setting": "value"}'::json);
SELECT pgnats.get_json('config');

-- Pub/Sub operations
SELECT pgnats.publish('news.updates', 'Breaking news!');
SELECT pgnats.request('service.ping', 'ping');

-- Object Store operations
SELECT pgnats.put_file('mybucket', 'file.txt', 'File contents'::bytea);
SELECT pgnats.get_file('mybucket', 'file.txt');
SELECT pgnats.file_list('mybucket');
```

## Git Repository

### Repository Structure

The fixes have been committed and pushed to the forked repository:

**Fork:** https://github.com/mrayva/pgnats
**Upstream:** https://github.com/luxms/pgnats

### Commit Information

**Commit:** d3336ab
**Message:** "fix: resolve build errors and update to PostgreSQL 18"

**Changes:**
- Fixed syntax errors in static PgLwLock declarations
- Removed unused imports
- Updated PostgreSQL version to 18
- Added CFLAGS configuration
- Created build wrapper script
- All 31 tests passing

### Git Remotes

```bash
git remote -v
# origin   https://github.com/mrayva/pgnats.git (your fork)
# upstream https://github.com/luxms/pgnats.git (original)
```

## Troubleshooting

### Issue: Permission Denied During Installation

**Symptom:**
```
Error: failed writing to `/usr/share/postgresql/18/extension/pgnats.control`
Permission denied (os error 13)
```

**Solution:**
```bash
# Temporarily grant ownership
sudo chown -R $USER:$USER /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/

# Run installation
./build.sh pgrx install

# Restore ownership
sudo chown -R root:root /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
```

### Issue: CFLAGS Not Being Applied

**Symptom:**
```
error: 'sigjmp_buf' undeclared
```

**Solution:**
Use the `build.sh` wrapper script instead of calling cargo directly, or manually export CFLAGS:
```bash
export CFLAGS="-std=gnu11"
cargo build
```

### Issue: Wrong PostgreSQL Version

**Symptom:**
```
Error: Postgres `pg14` is not managed by pgrx
```

**Solution:**
Either:
1. Use the updated `Cargo.toml` with `pg18` as default
2. Or specify the feature explicitly: `cargo build --no-default-features --features "pg18,kv,object_store,sub"`

## Summary

All build issues have been resolved:
- ✅ C compilation fixed with GNU11 standard
- ✅ PostgreSQL 18 compatibility established
- ✅ Syntax errors in unsafe code blocks corrected
- ✅ All 31 tests passing
- ✅ Extension successfully installed
- ✅ Build automation via wrapper script

The extension is now ready for use with PostgreSQL 18.
