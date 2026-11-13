"""幂等：同一 request_id 重试只产生一次业务效果（开发说明 §9.3）。

分工要说清楚，否则很容易做错：
- **PostgreSQL 是正式依据**：(session_id, request_id) 唯一约束 + answers 行里
  的阶段结果，决定了"这件事有没有做过"。
- **Redis 只是加速**：命中缓存直接返回上次的响应快照，省掉一次读库 + 序列化；
  缓存丢了不会导致重复处理（会退回数据库判定）。

分类函数刻意做成纯函数（不碰 IO），因为它决定"重试 / 冲突 / 继续"这种
最该被测死的逻辑。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.redis_client import app_key

log = get_logger(__name__)

Action = Literal["proceed", "replay", "resume", "conflict"]


def normalize_answer(answer: str | None) -> str:
    """回答归一化：去掉所有空白后比较。

    用户"重新提交同一段话"时可能多了换行/空格，那不算不同输入；
    反过来，内容有实质变化就必须判冲突，不能悄悄覆盖。
    """
    return "".join((answer or "").split())


def input_fingerprint(*, answer: str | None, skipped: bool) -> str:
    """输入指纹：区分"同一 request_id 携带不同输入"（§9.3 第 2 条）。"""
    payload = "skip" if skipped else f"answer:{normalize_answer(answer)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SubmissionCheck:
    action: Action
    reason: str


def classify_submission(
    existing,
    *,
    input_hash: str,
    question_id,
) -> SubmissionCheck:
    """根据已有回答行判定本次提交该怎么走。

    - 无已有行          → proceed（正常新提交）
    - 输入指纹不同      → conflict（同 ID 不同内容，客户端 bug 或重放攻击）
    - 题目不同          → conflict（同一个 request_id 不能换题）
    - 已完成            → replay（直接返回上次响应，不再跑模型）
    - 未完成（FAILED/PENDING/PROCESSING）
                        → resume（复用已保存的阶段结果，从中断处继续）
    """
    if existing is None:
        return SubmissionCheck("proceed", "新请求")

    if existing.input_hash != input_hash:
        return SubmissionCheck("conflict", "同一 request_id 携带了不同输入")

    if str(existing.question_id) != str(question_id):
        return SubmissionCheck("conflict", "同一 request_id 对应不同题目")

    if existing.processing_status == "COMPLETED":
        return SubmissionCheck("replay", "该请求已完成，返回上次响应")

    return SubmissionCheck("resume", f"该请求未完成（{existing.processing_status}），继续处理")


# ---------------------------------------------------------------- Redis 幂等缓存


def cache_key(session_id, request_id: str) -> str:
    return app_key("idem", str(session_id), str(request_id))


def get_cached(client: Redis | None, session_id, request_id: str) -> dict | None:
    """读缓存；Redis 异常当作未命中（缓存不是正确性来源）。"""
    if client is None:
        return None
    try:
        raw = client.get(cache_key(session_id, request_id))
    except RedisError as exc:
        log.warning("幂等缓存读取失败，退回数据库判定: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def put_cached(
    client: Redis | None, session_id, request_id: str, payload: dict
) -> None:
    """写缓存（带 TTL）。失败只记日志：正式记录已在 PG，缓存丢了不影响幂等。"""
    if client is None:
        return
    ttl_seconds = int(get_settings().business.idempotency_ttl_hours) * 3600
    try:
        client.set(
            cache_key(session_id, request_id),
            json.dumps(payload, ensure_ascii=False),
            ex=ttl_seconds,
        )
    except RedisError as exc:
        log.warning("幂等缓存写入失败（不影响正确性）: %s", exc)


def drop_cached(client: Redis | None, session_id, request_id: str) -> None:
    if client is None:
        return
    try:
        client.delete(cache_key(session_id, request_id))
    except RedisError:
        pass
