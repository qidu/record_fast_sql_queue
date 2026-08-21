# Integration with model_proxy_v3

This document shows how to integrate the recording service with a running `model_proxy_v3` instance.

## Configuration

### 1. Add to `proxy_config.toml`

```toml
[remote.recording]
# Point to this recording service
record_server = "http://localhost:8000/model-usage"

# Optional: include response bodies in records (for debugging)
# record_response_body = false
```

If using a combined auth + recording service, you can use the same URL for both:

```toml
[remote.authentication]
auth_server = "http://localhost:8000/auth"
# auth_with_model = false
# auth_with_body = false

[remote.recording]
record_server = "http://localhost:8000/model-usage"
record_response_body = false
```

### 2. Ensure Service is Accessible

The proxy must be able to reach the recording service at `http://localhost:8000/model-usage`:

```bash
# From the proxy machine, test connectivity
curl http://localhost:8000/health
# Should return: {"status": "healthy", "record_count": 0, "queue_size": 0}
```

## Request/Response Flow

### Proxy → Recording Service

When `record_server` is configured, the proxy POSTs to `/model-usage` after every request:

```
POST /model-usage HTTP/1.1
Host: localhost:8000
Content-Type: application/json
one-time-auth-code: otac-abc123
x-forwarded-for: 192.168.1.100
x-real-ip: 10.0.0.5

{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2025-01-15T10:30:45Z",
  "endpoint": "/v1/messages",
  "user_key": "sk-ant-xxxxxxxxxxxxxxxxxxxx",
  "model": "claude-3-5-sonnet-20241022",
  "response_status": 200,
  "input_tokens": 145,
  "cached_tokens": 50,
  "cache_written_tokens": 100,
  "output_tokens": 215,
  "total_tokens": 360
}
```

### Recording Service → Proxy

Returns immediately (fire-and-forget):

```json
{
  "status": "success",
  "record_id": 42,
  "message": "Usage record for request 550e... recorded successfully"
}
```

## Real Example: Auth + Recording in One Service

If you want a unified auth + recording service:

### Recording Service with Auth Endpoint

```python
# Add this to main.py to handle auth requests from the proxy
@app.get("/auth")
async def authenticate(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    x_goog_api_key: Optional[str] = Header(None),
    user_agent: Optional[str] = Header(None),
    request_id: Optional[str] = Header(None),
    endpoint: Optional[str] = Header(None),
    x_forwarded_for: Optional[str] = Header(None),
    x_real_ip: Optional[str] = Header(None),
):
    """
    Simple auth endpoint (fake passthrough).
    
    In production, validate against a key database.
    For now, accept any key and return an OTAC.
    """
    auth_key = authorization or x_api_key or x_goog_api_key
    if not auth_key:
        raise HTTPException(status_code=401, detail="No auth key provided")
    
    # Generate a unique OTAC for this auth event
    import uuid
    otac = f"otac-{uuid.uuid4()}"
    
    logger.info(f"Auth request from {x_forwarded_for}: key={auth_key[:10]}..., endpoint={endpoint}")
    
    return JSONResponse(
        status_code=200,
        content={},
        headers={
            "one-time-auth-code": otac
        }
    )
```

### Updated proxy_config.toml

```toml
[remote.authentication]
auth_server = "http://localhost:8000/auth"

[remote.recording]
record_server = "http://localhost:8000/model-usage"
```

## Monitoring & Querying

Once records are flowing, you can query them:

### Check Service Health

```bash
curl http://localhost:8000/health
```

### List Recent Records

```bash
curl 'http://localhost:8000/records?limit=10'
```

### Get User Stats

```bash
curl 'http://localhost:8000/stats/user?user_key=sk-ant-xxxxxxxxxxxx'
```

### Get Model Stats

```bash
curl 'http://localhost:8000/stats/model?model=claude-3-5-sonnet-20241022'
```

### Search by Request ID

```bash
curl 'http://localhost:8000/records/search?keyword=550e8400&field=request_id'
```

## Troubleshooting

### Records Not Being Received

1. Check proxy logs for errors posting to `/model-usage`:
   ```bash
   tail -f proxy.log | grep "record_server\|recording\|/model-usage"
   ```

2. Check recording service is running:
   ```bash
   curl http://localhost:8000/health
   ```

3. Check firewall/network connectivity:
   ```bash
   curl -v http://localhost:8000/model-usage \
     -X POST \
     -H "Content-Type: application/json" \
     -d '{"request_id":"test","endpoint":"/v1/messages","user_key":"test","model":"test","response_status":200}'
   ```

### Slow Recording

If recording is slow, check queue depth:

```bash
curl http://localhost:8000/stats/queue
```

If `queue_size` is growing, the writer may be backed up. Consider:
- Moving the database to faster storage (SSD)
- Running the service on a dedicated machine
- Increasing memory allocation

### Auth Not Linked to Records

If records don't have `one_time_auth_code` set, ensure:
1. The auth service is returning the header:
   ```bash
   curl -v http://localhost:8000/auth \
     -H "authorization: Bearer sk-test"
   # Should include: one-time-auth-code: otac-...
   ```

2. Proxy config has both `auth_server` and `record_server` configured

3. Proxy can reach the auth service (same as recording service)

## Performance Notes

- **Throughput**: With SQLite on SSD, expect ~1000–5000 records/sec
- **Latency**: Recording POSTs are fire-and-forget; proxy doesn't wait for a response
- **Storage**: Each record is ~300–500 bytes; 1 million records ≈ 300–500 MB database file
- **Indexes**: The service creates indexes on `request_id`, `timestamp`, `user_key`, `model` for fast querying

For high-volume deployments (>10k records/sec), consider:
- PostgreSQL instead of SQLite
- Separate read replicas for analytics
- Kafka or similar for async log collection
