# PGNats Deployment Guide

## Overview

This guide covers deploying pgnats in various environments from development to production.

## Deployment Scenarios

- [Quick Start (Single Server)](#quick-start-single-server)
- [Production Deployment](#production-deployment)
- [Docker Deployment](#docker-deployment)
- [Kubernetes Deployment](#kubernetes-deployment)
- [CI/CD Integration](#cicd-integration)

---

## Quick Start (Single Server)

### Prerequisites

- PostgreSQL 18 installed
- Rust toolchain (1.82.0+)
- NATS server running
- Build tools (gcc, make, libclang)

### Step 1: Install Build Dependencies

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y build-essential libclang-dev pkg-config postgresql-server-dev-18

# RHEL/CentOS/Fedora
sudo dnf install -y gcc clang-devel postgresql18-devel

# macOS
brew install llvm postgresql@18
```

### Step 2: Install Rust and pgrx

```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env

# Install cargo-pgrx
cargo install cargo-pgrx --version 0.16.1

# Initialize pgrx with PostgreSQL 18
cargo pgrx init --pg18 $(which pg_config)
```

### Step 3: Clone and Build pgnats

```bash
# Clone your fork
git clone https://github.com/mrayva/pgnats.git
cd pgnats

# Build the extension
export CFLAGS="-std=gnu11"
cargo build --release --no-default-features --features "pg18,kv,object_store,sub"

# Or use the build script
./build.sh build --release
```

### Step 4: Install Extension

```bash
# Temporarily grant permissions (if needed)
sudo chown -R $USER:$USER /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/

# Install the extension
./build.sh pgrx install --release

# Restore permissions
sudo chown -R root:root /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
```

### Step 5: Install NATS Server

```bash
# Using Docker
docker run -d --name nats -p 4222:4222 -p 8222:8222 nats:latest

# Or download binary
curl -L https://github.com/nats-io/nats-server/releases/download/v2.12.4/nats-server-v2.12.4-linux-amd64.zip -o nats-server.zip
unzip nats-server.zip
sudo mv nats-server-v2.12.4-linux-amd64/nats-server /usr/local/bin/
nats-server -js  # Start with JetStream enabled
```

### Step 6: Configure PostgreSQL

```bash
# Edit postgresql.conf
sudo nano /etc/postgresql/18/main/postgresql.conf
```

Add these settings:
```conf
# Required for pgnats
shared_preload_libraries = 'pgnats'

# Recommended settings
max_worker_processes = 16
max_parallel_workers = 8
```

Restart PostgreSQL:
```bash
sudo systemctl restart postgresql
```

### Step 7: Create Extension in Database

```sql
-- Connect to your database
psql -U postgres -d your_database

-- Create the extension
CREATE EXTENSION pgnats;

-- Verify installation
SELECT * FROM pgnats_version();

-- Configure NATS connection
CREATE SERVER nats_server
    FOREIGN DATA WRAPPER pgnats_fdw
    OPTIONS (nats_url 'nats://localhost:4222');
```

### Step 8: Test the Installation

```sql
-- Test KV storage
SELECT nats_put_text('test', 'hello', 'world');
SELECT nats_get_text('test', 'hello');

-- Test messaging
SELECT nats_publish_text('test.subject', 'Hello NATS!', NULL, NULL);

-- Check NATS connection
SELECT nats_get_server_info();
```

---

## Production Deployment

### Architecture Overview

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Application   │────▶│  PostgreSQL 18  │────▶│  NATS Cluster   │
│     Server      │     │   with pgnats   │     │  (JetStream)    │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │                         │
                               │                         │
                        ┌──────▼──────┐          ┌──────▼──────┐
                        │   Replica   │          │  NATS Node  │
                        │ PostgreSQL  │          │     #2      │
                        └─────────────┘          └─────────────┘
                                                        │
                                                 ┌──────▼──────┐
                                                 │  NATS Node  │
                                                 │     #3      │
                                                 └─────────────┘
```

### Production Prerequisites

1. **PostgreSQL Cluster**
   - Primary + Replicas (streaming replication)
   - Connection pooling (PgBouncer)
   - Backup solution (pgBackRest, Barman)

2. **NATS Cluster**
   - 3+ node cluster for HA
   - JetStream enabled
   - Persistent storage for streams

3. **Monitoring**
   - PostgreSQL metrics (pg_stat_statements)
   - NATS monitoring (NATS surveyor)
   - Logging (Loki, ELK)

### Production Installation Steps

#### 1. Build Optimized Binary

```bash
# Build with release profile
export CFLAGS="-std=gnu11"
cargo build --release --no-default-features --features "pg18,kv,object_store,sub"

# Strip debug symbols
strip target/release/pgnats.so

# Verify size (should be smaller)
ls -lh target/release/pgnats.so
```

#### 2. Create Installation Package

```bash
# Create deployment directory
mkdir -p pgnats-deploy/DEBIAN
mkdir -p pgnats-deploy/usr/lib/postgresql/18/lib
mkdir -p pgnats-deploy/usr/share/postgresql/18/extension

# Copy files
cp target/release/pgnats.so pgnats-deploy/usr/lib/postgresql/18/lib/
cp pgnats.control pgnats-deploy/usr/share/postgresql/18/extension/
cp pgnats--*.sql pgnats-deploy/usr/share/postgresql/18/extension/

# Create control file for package
cat > pgnats-deploy/DEBIAN/control <<EOF
Package: postgresql-18-pgnats
Version: 1.1.0
Architecture: amd64
Maintainer: Your Name <your.email@example.com>
Description: NATS messaging extension for PostgreSQL
Depends: postgresql-18
EOF

# Build .deb package
dpkg-deb --build pgnats-deploy postgresql-18-pgnats_1.1.0_amd64.deb
```

#### 3. Deploy to Servers

```bash
# Copy package to servers
scp postgresql-18-pgnats_1.1.0_amd64.deb user@db-server:/tmp/

# Install on each PostgreSQL server
ssh user@db-server
sudo dpkg -i /tmp/postgresql-18-pgnats_1.1.0_amd64.deb

# Or use Ansible
ansible-playbook -i inventory deploy-pgnats.yml
```

#### 4. Configure PostgreSQL for Production

```sql
-- postgresql.conf
shared_preload_libraries = 'pgnats'
max_worker_processes = 32
max_parallel_workers = 16
work_mem = 64MB
maintenance_work_mem = 512MB

-- Enable logging
log_min_duration_statement = 1000  # Log slow queries
log_line_prefix = '%t [%p]: [%l-1] user=%u,db=%d,app=%a,client=%h '
log_destination = 'csvlog'
logging_collector = on
```

#### 5. Setup NATS Cluster

```bash
# nats-server-1.conf
server_name: nats-1
port: 4222
http_port: 8222

jetstream {
    store_dir: /var/lib/nats/jetstream
    max_mem: 4G
    max_file: 100G
}

cluster {
    name: prod-cluster
    listen: 0.0.0.0:6222
    routes: [
        nats-route://nats-1:6222
        nats-route://nats-2:6222
        nats-route://nats-3:6222
    ]
}

# Repeat for nats-2 and nats-3 with different server_name
```

Start NATS cluster:
```bash
# On each node
nats-server -c /etc/nats/nats-server.conf -D
```

#### 6. Configure High Availability

```sql
-- Create foreign server with failover
CREATE SERVER nats_server
    FOREIGN DATA WRAPPER pgnats_fdw
    OPTIONS (
        nats_url 'nats://nats-1:4222,nats-2:4222,nats-3:4222',
        max_reconnects '10',
        reconnect_wait '2'
    );

-- Grant permissions
GRANT USAGE ON FOREIGN SERVER nats_server TO app_user;
```

---

## Docker Deployment

### Dockerfile for PostgreSQL with pgnats

```dockerfile
FROM postgres:18

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    libclang-dev \
    pkg-config \
    postgresql-server-dev-18 \
    && rm -rf /var/lib/apt/lists/*

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install cargo-pgrx
RUN cargo install cargo-pgrx --version 0.16.1
RUN cargo pgrx init --pg18 $(which pg_config)

# Copy pgnats source
COPY . /build/pgnats
WORKDIR /build/pgnats

# Build and install pgnats
ENV CFLAGS="-std=gnu11"
RUN cargo build --release --no-default-features --features "pg18,kv,object_store,sub"
RUN cargo pgrx install --release

# Configure PostgreSQL
RUN echo "shared_preload_libraries = 'pgnats'" >> /usr/share/postgresql/postgresql.conf.sample

# Cleanup
RUN apt-get remove -y build-essential curl && \
    apt-get autoremove -y && \
    rm -rf /build /root/.cargo/registry

CMD ["postgres"]
```

### Docker Compose Setup

```yaml
version: '3.8'

services:
  nats:
    image: nats:2.12.4
    ports:
      - "4222:4222"
      - "8222:8222"
    command: ["-js", "-sd", "/data"]
    volumes:
      - nats-data:/data
    networks:
      - pgnats-network

  postgres:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
      POSTGRES_DB: ${POSTGRES_DB:-app}
    ports:
      - "5432:5432"
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    depends_on:
      - nats
    networks:
      - pgnats-network

volumes:
  nats-data:
  postgres-data:

networks:
  pgnats-network:
    driver: bridge
```

### init.sql for Docker

```sql
-- Create extension
CREATE EXTENSION pgnats;

-- Configure NATS server
CREATE SERVER nats_server
    FOREIGN DATA WRAPPER pgnats_fdw
    OPTIONS (nats_url 'nats://nats:4222');

-- Create example callback
CREATE FUNCTION log_event(payload BYTEA)
RETURNS void AS $$
BEGIN
    RAISE NOTICE 'Event received: %', convert_from(payload, 'UTF8');
END;
$$ LANGUAGE plpgsql;

-- Subscribe to events
SELECT nats_subscribe('app.events', 'log_event'::regproc::oid);
SELECT pgnats_reload_conf();
```

### Deploy with Docker Compose

```bash
# Build and start
docker-compose up -d

# Check logs
docker-compose logs -f postgres

# Test connection
docker-compose exec postgres psql -U postgres -d app -c "SELECT * FROM pgnats_version();"

# Stop
docker-compose down

# Stop and remove volumes
docker-compose down -v
```

---

## Kubernetes Deployment

### PostgreSQL StatefulSet with pgnats

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: pgnats-init
data:
  init.sql: |
    CREATE EXTENSION IF NOT EXISTS pgnats;
    CREATE SERVER IF NOT EXISTS nats_server
        FOREIGN DATA WRAPPER pgnats_fdw
        OPTIONS (nats_url 'nats://nats-cluster:4222');
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres-pgnats
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
      - name: postgres
        image: your-registry/postgres-pgnats:1.1.0
        env:
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: postgres-secret
              key: password
        - name: POSTGRES_DB
          value: "app"
        ports:
        - containerPort: 5432
          name: postgres
        volumeMounts:
        - name: data
          mountPath: /var/lib/postgresql/data
        - name: init-scripts
          mountPath: /docker-entrypoint-initdb.d
      volumes:
      - name: init-scripts
        configMap:
          name: pgnats-init
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 10Gi
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
spec:
  selector:
    app: postgres
  ports:
  - port: 5432
    targetPort: 5432
  type: ClusterIP
```

### NATS Cluster on Kubernetes

```yaml
apiVersion: v1
kind: Service
metadata:
  name: nats-cluster
spec:
  selector:
    app: nats
  ports:
  - port: 4222
    name: client
  - port: 6222
    name: cluster
  - port: 8222
    name: monitor
  clusterIP: None
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: nats
spec:
  serviceName: nats-cluster
  replicas: 3
  selector:
    matchLabels:
      app: nats
  template:
    metadata:
      labels:
        app: nats
    spec:
      containers:
      - name: nats
        image: nats:2.12.4
        ports:
        - containerPort: 4222
          name: client
        - containerPort: 6222
          name: cluster
        - containerPort: 8222
          name: monitor
        args:
        - "-js"
        - "-sd"
        - "/data"
        - "-cluster"
        - "nats://0.0.0.0:6222"
        - "-routes"
        - "nats://nats-0.nats-cluster:6222,nats://nats-1.nats-cluster:6222,nats://nats-2.nats-cluster:6222"
        volumeMounts:
        - name: data
          mountPath: /data
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 10Gi
```

### Deploy to Kubernetes

```bash
# Create namespace
kubectl create namespace pgnats

# Create secrets
kubectl create secret generic postgres-secret \
  --from-literal=password='your-secure-password' \
  -n pgnats

# Deploy NATS
kubectl apply -f nats-cluster.yaml -n pgnats

# Wait for NATS to be ready
kubectl wait --for=condition=ready pod -l app=nats -n pgnats --timeout=300s

# Deploy PostgreSQL
kubectl apply -f postgres-pgnats.yaml -n pgnats

# Check status
kubectl get pods -n pgnats
kubectl logs -f postgres-pgnats-0 -n pgnats

# Test
kubectl exec -it postgres-pgnats-0 -n pgnats -- psql -U postgres -d app -c "SELECT * FROM pgnats_version();"
```

---

## CI/CD Integration

### GitHub Actions Workflow

```yaml
name: Build and Deploy pgnats

on:
  push:
    branches: [main]
    tags: ['v*']

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3

    - name: Setup Rust
      uses: actions-rs/toolchain@v1
      with:
        toolchain: stable
        override: true

    - name: Install PostgreSQL
      run: |
        sudo apt-get update
        sudo apt-get install -y postgresql-18 postgresql-server-dev-18 libclang-dev

    - name: Install cargo-pgrx
      run: cargo install cargo-pgrx --version 0.16.1

    - name: Initialize pgrx
      run: cargo pgrx init --pg18 $(which pg_config)

    - name: Build extension
      env:
        CFLAGS: "-std=gnu11"
      run: cargo build --release --no-default-features --features "pg18,kv,object_store,sub"

    - name: Run tests
      run: |
        sudo chown -R $USER:$USER /usr/share/postgresql/18/extension/ /usr/lib/postgresql/18/lib/
        cargo test --no-default-features --features "pg18,kv,object_store,sub"

    - name: Create package
      run: |
        mkdir -p package/DEBIAN package/usr/lib/postgresql/18/lib package/usr/share/postgresql/18/extension
        cp target/release/libpgnats.so package/usr/lib/postgresql/18/lib/pgnats.so
        cp *.control package/usr/share/postgresql/18/extension/
        cp *.sql package/usr/share/postgresql/18/extension/
        dpkg-deb --build package pgnats_${{ github.ref_name }}_amd64.deb

    - name: Upload artifact
      uses: actions/upload-artifact@v3
      with:
        name: pgnats-package
        path: pgnats_*.deb

  deploy:
    needs: build
    runs-on: ubuntu-latest
    if: startsWith(github.ref, 'refs/tags/')
    steps:
    - uses: actions/download-artifact@v3
      with:
        name: pgnats-package

    - name: Deploy to servers
      env:
        SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
      run: |
        echo "$SSH_KEY" > deploy_key
        chmod 600 deploy_key
        scp -i deploy_key pgnats_*.deb deploy@prod-server:/tmp/
        ssh -i deploy_key deploy@prod-server "sudo dpkg -i /tmp/pgnats_*.deb && sudo systemctl restart postgresql"
```

### GitLab CI/CD

```yaml
stages:
  - build
  - test
  - deploy

variables:
  CARGO_HOME: $CI_PROJECT_DIR/.cargo

build:
  stage: build
  image: rust:latest
  before_script:
    - apt-get update
    - apt-get install -y postgresql-server-dev-18 libclang-dev
    - cargo install cargo-pgrx --version 0.16.1
  script:
    - export CFLAGS="-std=gnu11"
    - cargo build --release --no-default-features --features "pg18,kv,object_store,sub"
  artifacts:
    paths:
      - target/release/libpgnats.so
    expire_in: 1 week

test:
  stage: test
  image: rust:latest
  services:
    - postgres:18
    - nats:2.12
  variables:
    POSTGRES_DB: test
    POSTGRES_USER: postgres
    POSTGRES_PASSWORD: postgres
  script:
    - cargo test --no-default-features --features "pg18,kv,object_store,sub"

deploy_production:
  stage: deploy
  only:
    - tags
  script:
    - scp target/release/libpgnats.so deploy@$PROD_SERVER:/tmp/pgnats.so
    - ssh deploy@$PROD_SERVER "sudo cp /tmp/pgnats.so /usr/lib/postgresql/18/lib/ && sudo systemctl restart postgresql"
```

---

## Configuration Management

### Ansible Playbook

```yaml
# playbook.yml
---
- name: Deploy pgnats extension
  hosts: postgres_servers
  become: yes
  vars:
    pgnats_version: "1.1.0"
    nats_url: "nats://nats-cluster:4222"

  tasks:
    - name: Copy pgnats package
      copy:
        src: "postgresql-18-pgnats_{{ pgnats_version }}_amd64.deb"
        dest: "/tmp/pgnats.deb"

    - name: Install pgnats
      apt:
        deb: /tmp/pgnats.deb

    - name: Configure PostgreSQL
      lineinfile:
        path: /etc/postgresql/18/main/postgresql.conf
        regexp: '^shared_preload_libraries'
        line: "shared_preload_libraries = 'pgnats'"
      notify: restart postgresql

    - name: Create extension in databases
      postgresql_ext:
        name: pgnats
        db: "{{ item }}"
        login_user: postgres
      loop: "{{ databases }}"

  handlers:
    - name: restart postgresql
      service:
        name: postgresql
        state: restarted
```

Run deployment:
```bash
ansible-playbook -i inventory playbook.yml
```

---

## Security Considerations

### 1. Network Security

```sql
-- Restrict NATS connection to specific hosts
CREATE SERVER nats_server
    FOREIGN DATA WRAPPER pgnats_fdw
    OPTIONS (
        nats_url 'nats://internal-nats.company.local:4222',
        tls_required 'true',
        tls_cert '/etc/ssl/certs/client.crt',
        tls_key '/etc/ssl/private/client.key',
        tls_ca '/etc/ssl/certs/ca.crt'
    );
```

### 2. Access Control

```sql
-- Create role for pgnats users
CREATE ROLE pgnats_user;

-- Grant minimal permissions
GRANT USAGE ON FOREIGN SERVER nats_server TO pgnats_user;
GRANT EXECUTE ON FUNCTION nats_publish_text TO pgnats_user;
GRANT EXECUTE ON FUNCTION nats_get_text TO pgnats_user;

-- Revoke admin functions
REVOKE EXECUTE ON FUNCTION pgnats_reload_conf FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pgnats_reload_conf TO postgres;
```

### 3. Audit Logging

```sql
-- Enable audit logging for pgnats functions
CREATE EXTENSION pgaudit;

ALTER SYSTEM SET pgaudit.log = 'function';
ALTER SYSTEM SET pgaudit.log_catalog = 'off';
SELECT pg_reload_conf();

-- Log all pgnats function calls
CREATE OR REPLACE FUNCTION audit_nats_call()
RETURNS event_trigger AS $$
BEGIN
    INSERT INTO nats_audit_log (function_name, executed_by, executed_at)
    VALUES (TG_TAG, current_user, now());
END;
$$ LANGUAGE plpgsql;

CREATE EVENT TRIGGER audit_nats
    ON ddl_command_end
    WHEN TAG IN ('CREATE FUNCTION', 'ALTER FUNCTION')
    EXECUTE FUNCTION audit_nats_call();
```

---

## Monitoring

### Prometheus Metrics

```sql
-- Create metrics view
CREATE VIEW pgnats_metrics AS
SELECT
    COUNT(*) as total_subscriptions,
    (SELECT COUNT(*) FROM message_log WHERE received_at > now() - interval '1 hour') as messages_last_hour,
    (SELECT COUNT(*) FROM message_log WHERE received_at > now() - interval '5 minutes') as messages_last_5min
FROM pgnats.subscriptions;

-- Export to prometheus_exporter
```

### Health Check Endpoint

```sql
-- Health check function
CREATE OR REPLACE FUNCTION pgnats_health_check()
RETURNS TABLE(component TEXT, status TEXT, details TEXT) AS $$
BEGIN
    -- Check extension
    RETURN QUERY
    SELECT 'extension'::TEXT,
           CASE WHEN EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pgnats')
                THEN 'ok' ELSE 'error' END::TEXT,
           ''::TEXT;

    -- Check NATS connection
    RETURN QUERY
    SELECT 'nats_connection'::TEXT,
           CASE WHEN (SELECT nats_get_server_info()) IS NOT NULL
                THEN 'ok' ELSE 'error' END::TEXT,
           ''::TEXT;

    -- Check background workers
    RETURN QUERY
    SELECT 'background_workers'::TEXT,
           'ok'::TEXT,
           (SELECT COUNT(*)::TEXT FROM pgnats.subscriptions);
END;
$$ LANGUAGE plpgsql;

-- Use in health checks
SELECT * FROM pgnats_health_check();
```

---

## Backup and Recovery

### Backup Strategy

```bash
# 1. Backup PostgreSQL with pg_basebackup
pg_basebackup -D /backup/postgres -Ft -z -P

# 2. Backup NATS streams
nats stream backup my_stream /backup/nats/my_stream.tar.gz

# 3. Backup configuration
cp /etc/postgresql/18/main/postgresql.conf /backup/config/
```

### Recovery Procedure

```bash
# 1. Restore PostgreSQL
pg_restore -d your_database /backup/postgres.tar

# 2. Restore NATS
nats stream restore my_stream /backup/nats/my_stream.tar.gz

# 3. Verify pgnats extension
psql -d your_database -c "SELECT * FROM pgnats_version();"

# 4. Reload configuration
psql -d your_database -c "SELECT pgnats_reload_conf();"
```

---

## Troubleshooting

### Common Issues

#### Extension Not Loading
```bash
# Check if library exists
ls -l /usr/lib/postgresql/18/lib/pgnats.so

# Check shared_preload_libraries
psql -c "SHOW shared_preload_libraries;"

# Check PostgreSQL logs
sudo tail -f /var/log/postgresql/postgresql-18-main.log
```

#### Background Workers Not Starting
```sql
-- Check foreign server
SELECT * FROM pg_foreign_server WHERE srvname = 'nats_server';

-- Force reload
SELECT pgnats_reload_conf_force();

-- Check logs
\! sudo grep PGNATS /var/log/postgresql/postgresql-18-main.log | tail -20
```

#### NATS Connection Issues
```bash
# Test NATS connection
nats-server --version
nats pub test "Hello"
nats sub test

# Check firewall
sudo ufw status
sudo ufw allow 4222/tcp
```

---

## Upgrade Procedure

### From v1.0.0 to v1.1.0

```sql
-- 1. Backup current database
\! pg_dump your_database > backup_before_upgrade.sql

-- 2. Install new version
\! sudo dpkg -i postgresql-18-pgnats_1.1.0_amd64.deb

-- 3. Upgrade extension
ALTER EXTENSION pgnats UPDATE TO '1.1.0';

-- 4. Verify version
SELECT * FROM pgnats_version();

-- 5. Reload configuration
SELECT pgnats_reload_conf_force();
```

---

## Performance Tuning

### PostgreSQL Settings

```sql
-- postgresql.conf
max_worker_processes = 32
max_parallel_workers = 16
shared_buffers = 8GB
effective_cache_size = 24GB
work_mem = 64MB
maintenance_work_mem = 2GB

# For pgnats specifically
max_connections = 200
```

### NATS Settings

```conf
# nats-server.conf
max_connections: 10000
max_payload: 8MB

jetstream {
    max_memory_store: 8G
    max_file_store: 500G
}
```

---

## Summary

You now have multiple deployment options:

✅ **Single Server** - Quick start for development
✅ **Production** - HA setup with clustering
✅ **Docker** - Containerized deployment
✅ **Kubernetes** - Cloud-native deployment
✅ **CI/CD** - Automated deployments

Choose the one that fits your infrastructure!
