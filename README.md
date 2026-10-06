# NOlas

A high-performance, async-based headless email client that is Nylas-API compatible.

## 🚀 Key Features

- **Massive Scale**: Handle 1000+ email accounts with simple polling
- **Async Architecture**: Uses asyncio for efficient I/O operations
- **Simple Polling**: Reliable 60-second polling instead of complex IDLE sessions
- **Distributed Workers**: Horizontal scaling with multiple worker processes
- **Database-Driven**: PostgreSQL for reliable state management
- **Webhook Delivery**: Reliable webhook delivery with retry logic
- **Health Monitoring**: Built-in health checks and automatic recovery
- **Graceful Shutdown**: Clean resource cleanup on shutdown

## 📋 Requirements

- Python 3.13+
- PostgreSQL 12+
- uv for package management (recommended)
- Redis (the existing email-service instance can be reused)

## Microsoft request concurrency

Set `REDIS_URL` on both the API and Nolas workers to the same Redis instance
(default: `redis://redis:6379/0`; `rediss://` supports TLS).
`REDIS_SOCKET_TIMEOUT_SECONDS` defaults to 3.

Microsoft Graph requests share a per-mailbox limit across Nolas replicas and apps.
The default `MICROSOFT_CONCURRENCY_LIMIT=4` allows four active requests;
`MICROSOFT_WORKER_CONCURRENCY_LIMIT=3` reserves one slot for API traffic. Waiting
API requests take priority over background jobs, with FIFO ordering within each
priority. The worker limit must be lower than the total limit (zero pauses workers).
The total limit cannot exceed 4. Read-update batch operations execute sequentially
within each batch so each batch consumes only one concurrency slot.

`MICROSOFT_CONCURRENCY_LEASE_SECONDS=30` controls crash recovery; active requests
renew their leases. `MICROSOFT_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS=30` bounds
waiting for capacity, returning a provider 429 on timeout. If Redis is unavailable
or a lease cannot be renewed, the request fails with a provider 503 so it can be
retried without bypassing the limit. Google and IMAP requests are unaffected.

Nolas uses separate keys from email-service to avoid nesting the same semaphore
when email-service calls Nolas. The two services do not share a combined limit.

Run the limiter's integration checks against a test Redis instance with
`REDIS_TEST_URL=redis://localhost:6379/15 uv run pytest tests/app/controllers/providers/microsoft/test_concurrency_limiter.py`.

## 🛠 Installation

1. **Clone the repository**:

```bash
git clone git@github.com:gvso/nolas.git
cd nolas
```

2. **Install dependencies**:

```bash
uv sync
```

3. **Setup PostgreSQL database**:

```bash
createdb nolas
```

## 🚦 Quick Start

### 1. List Accounts

View all accounts in the database:

```bash
python manage.py --mode list
```

### 2. Start the System

**Production (Cluster Mode)**:

```bash
python manage.py --mode cluster --workers 4
```

**Development (Single Worker)**:

```bash
python manage.py --mode single
```

## Run webserver

```
python server.py
```
