import json
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime
import aiosqlite

class AsyncSerializedDB:
    """异步串行化数据库操作类 - 用于 model_proxy_v3 远程记录服务"""

    def __init__(self, db_path: str = "model_usage.db"):
        self.db_path = db_path
        self._queue = asyncio.Queue()
        self._worker_task = None
        self._running = False
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self):
        """异步初始化：建立持久连接、启用 WAL、建表、启动写队列 worker。

        Must be called from within a running event loop (e.g. FastAPI's
        lifespan startup) — no I/O or task creation happens in __init__.
        """
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute('''
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
            )
        ''')
        await self._conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_request_id
            ON usage_records(request_id)
        ''')
        await self._conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_timestamp
            ON usage_records(timestamp DESC)
        ''')
        await self._conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_key
            ON usage_records(user_key)
        ''')
        await self._conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_model
            ON usage_records(model)
        ''')
        await self._conn.commit()

        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self):
        """工作协程：串行处理所有数据库写任务"""
        while self._running:
            try:
                task = await self._queue.get()
                try:
                    result = await task['func'](*task['args'], **task['kwargs'])
                    if task.get('future') and not task['future'].done():
                        task['future'].set_result(result)
                except Exception as e:
                    if task.get('future') and not task['future'].done():
                        task['future'].set_exception(e)
                    else:
                        # Nobody is waiting on this result; the failure
                        # would otherwise be silently dropped.
                        print(f"Worker task failed with no waiter: {e}")
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break

    async def _execute_task(self, func, *args, **kwargs):
        """
        内部方法：将任务放入队列并等待结果。

        如果写 worker 已经停止（崩溃或关闭中），立即失败而不是无限期挂起。
        """
        if not self._running or (self._worker_task and self._worker_task.done()):
            raise RuntimeError("DB write worker is not running; refusing to enqueue task")

        future = asyncio.get_event_loop().create_future()
        await self._queue.put({
            'func': func,
            'args': args,
            'kwargs': kwargs,
            'future': future
        })
        try:
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError("DB write timed out after 30s waiting on serialized queue")

    # ---------- 对外 API ----------

    async def record_usage(self, record_data: Dict[str, Any]) -> int:
        """
        记录模型使用情况（从 model_proxy_v3 的记录请求）

        Idempotent on request_id: if a record with the same request_id
        already exists (e.g. a proxy-side retry), no new row is inserted
        and the existing record's id is returned instead of raising a
        UNIQUE constraint error.

        Args:
            record_data: 包含 request_id, endpoint, user_key, model, response_status,
                        input_tokens, cached_tokens, cache_written_tokens, output_tokens,
                        total_tokens, response_body (可选)

        Returns:
            record_id: 新插入或已存在记录的 ID
        """
        async def _insert():
            cursor = await self._conn.execute(
                """INSERT OR IGNORE INTO usage_records
                (request_id, endpoint, user_key, model, response_status,
                 input_tokens, cached_tokens, cache_written_tokens, output_tokens,
                 total_tokens, one_time_auth_code, x_forwarded_for, x_real_ip, response_body)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_data.get('request_id'),
                    record_data.get('endpoint'),
                    record_data.get('user_key'),
                    record_data.get('model'),
                    record_data.get('response_status', 0),
                    record_data.get('input_tokens', 0),
                    record_data.get('cached_tokens', 0),
                    record_data.get('cache_written_tokens', 0),
                    record_data.get('output_tokens', 0),
                    record_data.get('total_tokens', 0),
                    record_data.get('one_time_auth_code'),
                    record_data.get('x_forwarded_for'),
                    record_data.get('x_real_ip'),
                    json.dumps(record_data.get('response_body')) if record_data.get('response_body') else None
                )
            )
            await self._conn.commit()

            if cursor.rowcount and cursor.rowcount > 0:
                # New row inserted.
                return cursor.lastrowid

            # rowcount == 0 means INSERT OR IGNORE hit the UNIQUE
            # constraint on request_id and skipped the insert — treat this
            # as an idempotent retry and return the existing record's id
            # instead of surfacing a UNIQUE constraint error.
            existing = await self._conn.execute(
                "SELECT id FROM usage_records WHERE request_id = ?",
                (record_data.get('request_id'),)
            )
            row = await existing.fetchone()
            if row is None:
                raise RuntimeError(
                    f"record_usage: insert was ignored but no existing row found "
                    f"for request_id={record_data.get('request_id')!r}"
                )
            return row[0]

        return await self._execute_task(_insert)

    async def get_records(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """
        查询使用记录（不经过队列；WAL 模式下与写操作并发安全）
        """
        cursor = await self._conn.execute(
            "SELECT * FROM usage_records ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def get_record_count(self) -> int:
        """获取总记录数"""
        cursor = await self._conn.execute("SELECT COUNT(*) FROM usage_records")
        count = await cursor.fetchone()
        return count[0] if count else 0

    async def get_stats_by_user_key(self, user_key: str) -> Dict[str, Any]:
        """按用户密钥获取统计信息"""
        cursor = await self._conn.execute(
            """SELECT
                COUNT(*) as request_count,
                SUM(input_tokens) as total_input_tokens,
                SUM(cached_tokens) as total_cached_tokens,
                SUM(cache_written_tokens) as total_cache_written_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(total_tokens) as total_tokens_used
            FROM usage_records WHERE user_key = ?""",
            (user_key,)
        )
        row = await cursor.fetchone()
        return {
            'user_key': user_key,
            'request_count': row[0] if row[0] else 0,
            'total_input_tokens': row[1] if row[1] else 0,
            'total_cached_tokens': row[2] if row[2] else 0,
            'total_cache_written_tokens': row[3] if row[3] else 0,
            'total_output_tokens': row[4] if row[4] else 0,
            'total_tokens_used': row[5] if row[5] else 0,
        }

    async def get_stats_by_model(self, model: str) -> Dict[str, Any]:
        """按模型获取统计信息"""
        cursor = await self._conn.execute(
            """SELECT
                COUNT(*) as request_count,
                SUM(input_tokens) as total_input_tokens,
                SUM(cached_tokens) as total_cached_tokens,
                SUM(cache_written_tokens) as total_cache_written_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(total_tokens) as total_tokens_used
            FROM usage_records WHERE model = ?""",
            (model,)
        )
        row = await cursor.fetchone()
        return {
            'model': model,
            'request_count': row[0] if row[0] else 0,
            'total_input_tokens': row[1] if row[1] else 0,
            'total_cached_tokens': row[2] if row[2] else 0,
            'total_cache_written_tokens': row[3] if row[3] else 0,
            'total_output_tokens': row[4] if row[4] else 0,
            'total_tokens_used': row[5] if row[5] else 0,
        }

    async def search_records(self, keyword: str, field: str = "request_id") -> List[Dict[str, Any]]:
        """搜索记录"""
        cursor = await self._conn.execute(
            f"SELECT * FROM usage_records WHERE {field} LIKE ? ORDER BY timestamp DESC",
            (f'%{keyword}%',)
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        return {
            'id': row['id'],
            'timestamp': row['timestamp'],
            'request_id': row['request_id'],
            'endpoint': row['endpoint'],
            'user_key': row['user_key'],
            'model': row['model'],
            'response_status': row['response_status'],
            'input_tokens': row['input_tokens'],
            'cached_tokens': row['cached_tokens'],
            'cache_written_tokens': row['cache_written_tokens'],
            'output_tokens': row['output_tokens'],
            'total_tokens': row['total_tokens'],
            'one_time_auth_code': row['one_time_auth_code'],
            'x_forwarded_for': row['x_forwarded_for'],
            'x_real_ip': row['x_real_ip'],
            'response_body': json.loads(row['response_body']) if row['response_body'] else None
        }

    async def close(self):
        """优雅关闭：等待所有任务完成"""
        self._running = False
        if self._worker_task:
            # 等待队列中的任务全部处理完
            await self._queue.join()
            # 取消工作协程
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def get_queue_stats(self):
        """获取队列状态"""
        return {
            "queue_size": self._queue.qsize(),
            "is_running": self._running,
            "worker_alive": bool(self._worker_task) and not self._worker_task.done()
        }

# 创建全局数据库实例（未初始化 —— 需在 FastAPI lifespan 中调用 await db.init()）
db = AsyncSerializedDB()
