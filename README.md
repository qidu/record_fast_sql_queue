# Model Usage Recording Service

A lightweight async FastAPI service that acts as a `[remote.authentication]` /
`[remote.recording]` sidecar for `model_proxy_v3`. It authenticates callers
against a static, file-based list of fake keys (OTAC passthrough) and records
per-request model usage — auth key, resolved model id, token counts — to
SQLite via a serialized write queue.

This is intended for local development and integration testing of the proxy's
auth/stats protocol, not as a production credential store.

## Status

- Auth endpoints (`GET /auth`, `POST /auth`) implemented against a JSON key file
- Recording endpoint (`POST /model-usage`) implemented per the proxy's
  `ModelUsageRecordPayload` contract
- Read/query endpoints for records and aggregate stats
- Usage recording is idempotent on `request_id` (proxy-side retries return
  the existing record id instead of a `500`)
- Source code lives under `src/`; run via `run.py` or `uvicorn src.main:app`
- Known correctness/security/performance gaps are tracked below — see
  [Known Issues](#known-issues)

## Features

- **Fake-key auth** — validates `Authorization` / `x-api-key` / `x-goog-api-key`
  against `auth_keys.json`, hot-reloaded on file change (mtime check)
- **OTAC issuance** — on successful auth, returns a fresh `one-time-auth-code`
  header per request, for linkage with the later usage record
- **Serialized writes** — all inserts go through an async queue to avoid
  SQLite write contention
- **Concurrent reads** — queries bypass the queue; a single persistent
  connection with `PRAGMA journal_mode=WAL` allows reads to proceed while
  writes are in flight
- **Usage recording** — captures `request_id`, `endpoint`, `user_key`, `model`,
  `response_status`, token counters, OTAC, and forwarded IP headers
- **Aggregate stats** — per user key and per model

## Project Layout

```
record_fast_sql_queue/
├── run.py                   # entry point: uvicorn src.main:app
├── auth_keys.example.json   # sample fake-key file — copy to auth_keys.json
├── requirements.txt
├── pyproject.toml
├── src/
│   ├── __init__.py
│   ├── main.py               # FastAPI app, routes
│   ├── database.py           # AsyncSerializedDB (SQLite + write queue)
│   └── auth.py                # FakeKeyAuth (file-backed key validation)
└── tests/
    └── batch.py               # concurrent load test / usage example
```

## Architecture

```
model_proxy_v3
    │
    │ 1) GET/POST /auth  (Authorization / x-api-key / x-goog-api-key)
    ▼
Recording Service — src/auth.py
    │  validates against auth_keys.json
    │  200 + header one-time-auth-code (OTAC)  — or 401
    ▼
model_proxy_v3 routes request to upstream, gets response
    │
    │ 2) POST /model-usage
    │    body: request_id, endpoint, user_key, model, tokens...
    │    header: one-time-auth-code, x-forwarded-for, x-real-ip
    ▼
Recording Service — src/main.py + src/database.py
    │  writes go through an asyncio.Queue, one worker serializes them
    ▼
SQLite (usage_records table)
```

## Setup

### Requirements

```bash
pip install -r requirements.txt
```

### Configure fake auth keys

```bash
cp auth_keys.example.json auth_keys.json
# edit auth_keys.json to add/remove keys
```

`auth_keys.json` format:

```json
{
  "keys": [
    "sk-fake-test-key-001",
    "sk-fake-test-key-002"
  ]
}
```

The file is re-read automatically when its mtime changes — no restart needed
to add/revoke a key. Set a custom path via `AUTH_KEYS_FILE` env var.

### Running

```bash
# Development mode (with reload)
python run.py

# Equivalent explicit uvicorn invocation
uvicorn src.main:app --reload --host 0.0.0.0 --port 8989
```

### Configure model_proxy_v3

In `proxy_config.toml`, point both sidecars at this service:

```toml
[remote.authentication]
auth_server = "http://localhost:8989/auth"
# auth_with_model = false
# auth_with_body = false

[remote.recording]
record_server = "http://localhost:8989/model-usage"
# record_response_body = false
```

Since both endpoints are served by the same process, the OTAC issued by
`/auth` is automatically linkable to the record posted to `/model-usage`.

## API Endpoints

### `GET /auth`, `POST /auth`

Fake-key authentication. `GET` is used by default
(`auth_with_model = false`, `auth_with_body = false`); `POST` is used when the
proxy is configured with `auth_with_body = true` (the parsed request body is
read and logged, but not currently used in the validation decision).

**Headers read**: `Authorization`, `x-api-key`, `x-goog-api-key`,
`user-agent`, `request_id`, `endpoint`, `x-resource-for`, `x-forwarded-for`,
`x-real-ip` — mirrors what the proxy forwards per the auth protocol.

**Response**:
- `200` with header `one-time-auth-code: otac-<uuid4>` on success
- `401` when the key is missing or not present in `auth_keys.json`

```bash
curl -i http://localhost:8989/auth \
  -H "Authorization: Bearer sk-fake-test-key-001"
```

### `POST /model-usage`

Records a usage entry for a completed proxy request.

**Headers** (all optional, passthrough): `one-time-auth-code`,
`x-forwarded-for`, `x-real-ip`.

**Body** (`ModelUsageRecord`):

```json
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "endpoint": "/v1/messages",
  "user_key": "sk-fake-test-key-001",
  "model": "claude-3-5-sonnet-20241022",
  "response_status": 200,
  "input_tokens": 120,
  "cached_tokens": 20,
  "cache_written_tokens": 50,
  "output_tokens": 180,
  "total_tokens": 300,
  "timestamp": "2025-01-15T10:30:45Z"
}
```

**Response**:

```json
{ "status": "success", "record_id": 42, "message": "Usage record for request 550e8400... recorded successfully" }
```

### `GET /records?limit=100&offset=0`

List recorded usage entries, most recent first.

### `GET /records/search?keyword=...&field=request_id&limit=50`

Search by `request_id`, `user_key`, `model`, or `endpoint`.

### `GET /stats/user?user_key=...`

Aggregate token counts and request count for one auth key (the api key userd).

### `GET /stats/model?model=...`

Aggregate token counts and request count for one model id (the resolved target model id)

### `GET /stats/queue`

Internal write-queue depth and worker health.

### `GET /health`

Liveness/readiness check. Returns `record_count`, `queue_size`, and
`worker_alive`; `status` is `healthy` when the write-queue worker is
running, `degraded` if it has died, `unhealthy` (503) on a query failure.

## Testing

```bash
# terminal 1
python run.py

# terminal 2 — sends 20 concurrent usage records + queries stats
python tests/batch.py
```

Manual smoke test:

```bash
curl -i http://localhost:8989/auth -H "Authorization: Bearer sk-fake-test-key-001"

curl -X POST http://localhost:8989/model-usage \
  -H "Content-Type: application/json" \
  -H "one-time-auth-code: otac-manual-test" \
  -d '{
    "request_id": "manual-001",
    "endpoint": "/v1/messages",
    "user_key": "sk-fake-test-key-001",
    "model": "claude-3-5-sonnet-20241022",
    "response_status": 200,
    "input_tokens": 100,
    "output_tokens": 150,
    "total_tokens": 250
  }'

curl http://localhost:8989/records?limit=5
curl http://localhost:8989/stats/user?user_key=sk-fake-test-key-001
```

## Database Schema

```sql
CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    request_id TEXT NOT NULL UNIQUE,
    endpoint TEXT NOT NULL,
    user_key TEXT NOT NULL,
    model TEXT NOT NULL,
    response_status INTEGER,
    input_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    cache_written_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    one_time_auth_code TEXT,
    x_forwarded_for TEXT,
    x_real_ip TEXT,
    response_body TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_request_id ON usage_records(request_id);
CREATE INDEX idx_timestamp ON usage_records(timestamp DESC);
CREATE INDEX idx_user_key ON usage_records(user_key);
CREATE INDEX idx_model ON usage_records(model);
```

## Known Issues

This service is a working prototype, not hardened for production. Open items:

- **Auth keys stored/logged in plaintext** — `user_key` is persisted verbatim
  in SQLite and partially logged (first 10 chars) on every write.
- **No auth on the recording/query endpoints themselves** — `/model-usage`,
  `/records`, `/stats/*` are unauthenticated; only `/auth` gates anything.
- **`search_records` does unindexed `LIKE '%...%'` scans** — degrades linearly
  as the table grows; leading-wildcard queries can't use the existing indexes.
- **No upper bound on `limit` query params** — a caller can request an
  arbitrarily large page in one response.
- **`response_body` has no size cap** — large accumulated SSE bodies (with
  `record_response_body = true`) can grow the DB unbounded.

See project history / prior review notes for the full writeup of each item.

## License

MIT.
