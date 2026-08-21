import asyncio
import aiohttp
import json
import uuid
from datetime import datetime

async def send_usage_record(session, i, user_key="test-key-001"):
    """
    发送一条模型使用记录到远程记录服务
    
    模拟 model_proxy_v3 的记录请求
    """
    url = "http://localhost:8000/model-usage"
    
    # 生成唯一的请求 ID（模拟 proxy 生成的）
    request_id = str(uuid.uuid4())
    
    data = {
        "request_id": request_id,
        "endpoint": "/v1/messages",
        "user_key": user_key,
        "model": "claude-3-5-sonnet-20241022",
        "response_status": 200,
        "input_tokens": 100 + i * 10,
        "cached_tokens": 20,
        "cache_written_tokens": 50,
        "output_tokens": 150 + i * 5,
        "total_tokens": 250 + i * 15,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    
    headers = {
        "one-time-auth-code": f"otac-{i:03d}",
        "x-forwarded-for": f"192.168.1.{100 + i}",
        "x-real-ip": f"10.0.0.{i}"
    }
    
    async with session.post(url, json=data, headers=headers) as resp:
        result = await resp.json()
        print(f"[{i}] Status: {resp.status}, Request ID: {request_id}, Result: {result}")
        return result

async def send_batch_records(count=20, user_keys=None):
    """
    发送一批使用记录
    """
    if user_keys is None:
        user_keys = ["test-key-001", "test-key-002", "test-key-003"]
    
    async with aiohttp.ClientSession() as session:
        tasks = []
        for i in range(count):
            user_key = user_keys[i % len(user_keys)]
            tasks.append(send_usage_record(session, i, user_key))
        
        results = await asyncio.gather(*tasks)
        return results

async def query_stats():
    """
    查询统计信息
    """
    async with aiohttp.ClientSession() as session:
        # 查询用户统计
        async with session.get("http://localhost:8000/stats/user?user_key=test-key-001") as resp:
            user_stats = await resp.json()
            print("\nUser Stats (test-key-001):")
            print(json.dumps(user_stats, indent=2))
        
        # 查询模型统计
        async with session.get("http://localhost:8000/stats/model?model=claude-3-5-sonnet-20241022") as resp:
            model_stats = await resp.json()
            print("\nModel Stats (claude-3-5-sonnet-20241022):")
            print(json.dumps(model_stats, indent=2))
        
        # 查询所有记录
        async with session.get("http://localhost:8000/records?limit=5") as resp:
            records = await resp.json()
            print(f"\nRecent Records (limit=5):")
            print(json.dumps(records, indent=2))

async def main():
    print("=== 发送 20 条使用记录 ===")
    await send_batch_records(count=20)
    
    print("\n=== 等待 2 秒，然后查询统计 ===")
    await asyncio.sleep(2)
    
    await query_stats()

if __name__ == "__main__":
    asyncio.run(main())
