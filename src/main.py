from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List
from contextlib import asynccontextmanager
import time
import logging
import json

from src.database import db
from src.auth import fake_auth

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- 数据模型 ----------

class ModelUsageRecord(BaseModel):
    """model_proxy_v3 发送的模型使用记录"""
    request_id: str = Field(..., description="Proxy 生成的请求 ID")
    endpoint: str = Field(..., description="请求端点，如 /v1/messages")
    user_key: str = Field(..., description="原始用户认证密钥（Authorization / x-api-key）")
    model: str = Field(..., description="已解析的上游模型 ID")
    response_status: int = Field(..., description="上游 HTTP 状态码")
    input_tokens: int = Field(default=0, description="输入 token 数")
    cached_tokens: int = Field(default=0, description="缓存读取 token 数")
    cache_written_tokens: int = Field(default=0, description="缓存写入 token 数")
    output_tokens: int = Field(default=0, description="输出 token 数")
    total_tokens: int = Field(default=0, description="总 token 数")
    response_body: Optional[Dict[str, Any] | str] = Field(default=None, description="响应体（仅当 record_response_body=true）")
    timestamp: Optional[str] = Field(default=None, description="ISO 8601 时间戳")

class UsageRecordResponse(BaseModel):
    """记录成功的响应"""
    status: str = "success"
    record_id: int
    message: Optional[str] = None

class RecordListResponse(BaseModel):
    """查询列表响应"""
    total: int
    records: List[Dict[str, Any]]

class StatsResponse(BaseModel):
    """统计响应"""
    user_key: Optional[str] = None
    model: Optional[str] = None
    request_count: int
    total_input_tokens: int
    total_cached_tokens: int
    total_cache_written_tokens: int
    total_output_tokens: int
    total_tokens_used: int

# ---------- 生命周期管理 ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动和关闭时的处理"""
    # 启动时：建立连接、启用 WAL、建表、启动写队列 worker
    await db.init()
    logger.info("Database initialized and worker started")
    
    yield  # 应用运行期间
    
    # 关闭时：优雅关闭数据库连接
    await db.close()
    logger.info("Database connection closed")

# 创建 FastAPI 应用
app = FastAPI(
    title="Model Proxy Remote Recording Service",
    description="model_proxy_v3 的远程模型使用记录收集服务",
    version="1.0.0",
    lifespan=lifespan
)

# ---------- 路由 ----------

@app.get("/auth")
async def authenticate_get(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    x_goog_api_key: Optional[str] = Header(None),
    user_agent: Optional[str] = Header(None),
    request_id: Optional[str] = Header(None),
    endpoint: Optional[str] = Header(None),
    x_resource_for: Optional[str] = Header(None),
    x_forwarded_for: Optional[str] = Header(None),
    x_real_ip: Optional[str] = Header(None),
):
    """
    认证端点（GET，默认模式 —— auth_with_model/auth_with_body = false）

    校验来自 model_proxy_v3 的客户端认证密钥（Authorization / x-api-key /
    x-goog-api-key），密钥来自本地 JSON 文件（伪造/测试用，非真实凭证校验）。

    成功：200 + header `one_time_auth_code`（OTAC，用于关联后续的 stats 记录）
    失败：401
    """
    return _do_authenticate(
        authorization, x_api_key, x_goog_api_key,
        user_agent, request_id, endpoint, x_resource_for,
        x_forwarded_for, x_real_ip,
    )


@app.post("/auth")
async def authenticate_post(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    x_goog_api_key: Optional[str] = Header(None),
    user_agent: Optional[str] = Header(None),
    request_id: Optional[str] = Header(None),
    endpoint: Optional[str] = Header(None),
    x_resource_for: Optional[str] = Header(None),
    x_forwarded_for: Optional[str] = Header(None),
    x_real_ip: Optional[str] = Header(None),
):
    """
    认证端点（POST，auth_with_body = true 时代理会带上完整请求体）

    请求体本身不参与本示例的鉴权逻辑（仅做透传日志），密钥仍从请求头读取。
    """
    try:
        body = await request.json()
    except Exception:
        body = None

    if body is not None:
        logger.info(f"Auth POST body received (auth_with_body=true): {json.dumps(body)[:200]}")

    return _do_authenticate(
        authorization, x_api_key, x_goog_api_key,
        user_agent, request_id, endpoint, x_resource_for,
        x_forwarded_for, x_real_ip,
    )


def _do_authenticate(
    authorization: Optional[str],
    x_api_key: Optional[str],
    x_goog_api_key: Optional[str],
    user_agent: Optional[str],
    request_id: Optional[str],
    endpoint: Optional[str],
    x_resource_for: Optional[str],
    x_forwarded_for: Optional[str],
    x_real_ip: Optional[str],
) -> JSONResponse:
    raw_key = fake_auth.extract_key(authorization, x_api_key, x_goog_api_key)
    matched_key = fake_auth.validate(raw_key)

    if matched_key is None:
        logger.warning(
            f"Auth failed: request_id={request_id}, endpoint={endpoint}, "
            f"x_forwarded_for={x_forwarded_for}"
        )
        raise HTTPException(status_code=401, detail="Invalid or unknown auth key")

    otac = fake_auth.generate_otac()
    logger.info(
        f"Auth OK: request_id={request_id}, endpoint={endpoint}, "
        f"model={x_resource_for}, otac={otac}"
    )

    # 200 + empty JSON body (no dynamic routing override) + OTAC header
    return JSONResponse(
        status_code=200,
        content={},
        headers={"one-time-auth-code": otac},
    )


@app.post("/model-usage", response_model=UsageRecordResponse, status_code=201)
async def record_model_usage(
    record: ModelUsageRecord,
    request: Request,
    one_time_auth_code: Optional[str] = Header(None, alias="one-time-auth-code"),
    x_forwarded_for: Optional[str] = Header(None),
    x_real_ip: Optional[str] = Header(None),
):
    """
    记录模型使用情况（来自 model_proxy_v3）
    
    接收来自代理的 POST 请求，包含该请求的 token 使用情况、模型 ID、用户密钥等。
    所有写操作通过队列串行执行，避免 SQLite 并发写冲突。
    
    Headers:
    - one-time-auth-code (OTAC): 认证服务返回的一次性认证代码
    - x-forwarded-for: 客户端 IP
    - x-real-ip: 真实客户端 IP（仅当调用者未发送时）
    """
    try:
        client_ip = request.client.host if request.client else "unknown"
        start_time = time.time()
        
        # 构建完整的记录数据
        record_data = record.model_dump()
        record_data['one_time_auth_code'] = one_time_auth_code
        record_data['x_forwarded_for'] = x_forwarded_for
        record_data['x_real_ip'] = x_real_ip
        
        # 记录数据（会等待队列处理完成）
        record_id = await db.record_usage(record_data)
        
        elapsed = (time.time() - start_time) * 1000
        logger.info(
            f"Recorded usage: request_id={record.request_id}, "
            f"model={record.model}, user_key={record.user_key[:10]}..., "
            f"tokens={record.total_tokens} in {elapsed:.2f}ms"
        )
        
        return UsageRecordResponse(
            status="success",
            record_id=record_id,
            message=f"Usage record for request {record.request_id} recorded successfully"
        )
    
    except Exception as e:
        logger.error(f"Failed to record usage: {e}")
        raise HTTPException(status_code=500, detail="Failed to record usage")

@app.get("/records", response_model=RecordListResponse)
async def get_records(
    limit: int = 100,
    offset: int = 0
):
    """
    获取所有使用记录（支持分页）
    
    查询操作不走队列，直接读取，支持并发
    """
    try:
        total = await db.get_record_count()
        records = await db.get_records(limit, offset)
        
        return RecordListResponse(
            total=total,
            records=records
        )
    
    except Exception as e:
        logger.error(f"Query failed: {e}")
        raise HTTPException(status_code=500, detail="Query failed")

@app.get("/records/search")
async def search_records(
    keyword: str,
    field: str = "request_id",
    limit: int = 50
):
    """
    搜索使用记录
    
    可搜索字段：request_id, user_key, model, endpoint
    """
    try:
        if field not in ["request_id", "user_key", "model", "endpoint"]:
            raise HTTPException(status_code=400, detail=f"Invalid field: {field}")

        records = await db.search_records(keyword, field)
        return {
            "keyword": keyword,
            "field": field,
            "count": len(records[:limit]),
            "records": records[:limit]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@app.get("/stats/user")
async def get_user_stats(user_key: str):
    """
    获取特定用户密钥的使用统计
    """
    try:
        stats = await db.get_stats_by_user_key(user_key)
        return stats
    except Exception as e:
        logger.error(f"Stats query failed: {e}")
        raise HTTPException(status_code=500, detail="Stats query failed")

@app.get("/stats/model")
async def get_model_stats(model: str):
    """
    获取特定模型的使用统计
    """
    try:
        stats = await db.get_stats_by_model(model)
        return stats
    except Exception as e:
        logger.error(f"Stats query failed: {e}")
        raise HTTPException(status_code=500, detail="Stats query failed")

@app.get("/stats/queue")
async def get_queue_stats():
    """队列处理统计"""
    stats = await db.get_queue_stats()
    return stats

@app.get("/health")
async def health_check():
    """健康检查接口"""
    try:
        count = await db.get_record_count()
        queue_stats = await db.get_queue_stats()
        return {
            "status": "healthy" if queue_stats["worker_alive"] else "degraded",
            "record_count": count,
            "queue_size": queue_stats["queue_size"],
            "worker_alive": queue_stats["worker_alive"]
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy"}
        )

# ---------- 可选：异常处理 ----------
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "detail": exc.detail
        }
    )

# ---------- 启动命令 ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True  # 开发模式
    )
