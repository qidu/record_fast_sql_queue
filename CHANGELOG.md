# Changelog: model_proxy_v3 Recording Service

## Updated for model_proxy_v3 Integration

### Database Schema (`database.py`)

**Before**: Generic data storage with `data` (JSON) and `source_ip` fields.

**After**: Specialized for model usage tracking:
- `request_id` (UNIQUE) — Proxy-generated request identifier
- `endpoint` — API endpoint (e.g., `/v1/messages`)
- `user_key` — Raw authentication key (Authorization / x-api-key)
- `model` — Resolved upstream model ID
- `response_status` — Upstream HTTP status
- `input_tokens`, `cached_tokens`, `cache_written_tokens`, `output_tokens`, `total_tokens` — Token counters
- `one_time_auth_code` — OTAC from auth_server (for audit linkage)
- `x_forwarded_for`, `x_real_ip` — Request context headers
- `response_body` — Optional response body (when `record_response_body=true`)
- Indexes on: `request_id`, `timestamp`, `user_key`, `model`

### New Database Methods

- `record_usage(record_data)` — Insert a usage record from proxy
- `get_stats_by_user_key(user_key)` — Aggregate stats per auth key
- `get_stats_by_model(model)` — Aggregate stats per model

### API Endpoints (`main.py`)

**Before**: Generic `/records` endpoint for arbitrary JSON data.

**After**: 

- **POST `/model-usage`** — Main endpoint for proxy to send usage records
  - Headers: `one-time-auth-code`, `x-forwarded-for`, `x-real-ip` (passthrough)
  - Body: ModelUsageRecord (with all token fields)
  - Returns: `{ status, record_id, message }`

- **GET `/records`** — Query all records (unchanged, but returns model-specific fields)

- **GET `/records/search`** — Search by keyword in field (now supports: request_id, user_key, model, endpoint)

- **GET `/stats/user?user_key=...`** — NEW: Aggregate stats for a user key

- **GET `/stats/model?model=...`** — NEW: Aggregate stats for a model

- **GET `/stats/queue`** — Queue status (unchanged)

- **GET `/health`** — Health check (unchanged)

### Data Models (`main.py`)

**New**: `ModelUsageRecord` 
- All fields from the proxy's stats payload
- Optional: `timestamp`, `response_body`
- Descriptions map to proxy protocol

### Test Suite (`tests/batch.py`)

**Before**: Generic `/records` POST with arbitrary data.

**After**:
- Sends to `/model-usage` endpoint
- Includes all token fields
- Simulates multiple users (test-key-001, test-key-002, test-key-003)
- Includes OTAC, x-forwarded-for, x-real-ip headers
- Queries `/stats/user` and `/stats/model` endpoints

### Documentation

**New**: `INTEGRATION.md`
- How to configure proxy_config.toml
- Request/response flow with real examples
- Combined auth + recording service pattern
- Troubleshooting guide
- Performance notes

**Updated**: `README.md`
- Complete rewrite for recording service context
- OTAC linkage explanation
- All endpoint documentation with examples
- Database schema reference
- Design notes on serialized writes vs concurrent reads

### Requirements

**Added**: `aiohttp==3.9.1` for test client

## Compatibility

✅ **Fully compatible with `model_proxy_v3` `[remote.recording]` protocol**

Can be used:
1. As `record_server` alone
2. Combined with auth_server (different URL)
3. As unified auth + recording service (same URL)

## Migration Notes

If you have an existing database:
1. The schema is incompatible (different table structure)
2. Either: delete `fastapi_data.db` (fresh start) or
3. Dump old records manually before upgrading
