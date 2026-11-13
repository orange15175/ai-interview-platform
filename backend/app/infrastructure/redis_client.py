"""Redis 客户端与 LangGraph Redis Checkpointer（开发说明 §9）。

三块职责（彼此独立，不要混用）：
1. Checkpointer：LangGraph 的执行位置与临时上下文 —— 用 redis-stack 的
   RedisJSON + RediSearch 能力，普通 redis:7.x 镜像会直接报错。
2. 会话锁：按 session_id 加锁（阶段 4 实现在 services/locks.py，这里只提供客户端）。
3. 幂等缓存：加速返回已处理请求的结果。

所有 key 带应用前缀 `aiiv:` + 环境标识，避免与其他项目共用实例时互相污染。
"""

from __future__ import annotations

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

KEY_PREFIX = "aiiv"  # AI interView 的缩写；所有 key 形如 aiiv:env:...

_client: Redis | None = None


def get_redis() -> Redis:
    """惰性单例客户端。decode_responses=True：直接拿 str，省一层手工解码。"""
    global _client
    if _client is None:
        url = get_settings().redis_url
        _client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=3)
        log.info("Redis 客户端已创建: %s", _sanitize_redis_url(url))
    return _client


def reset_redis() -> None:
    """测试用：强制下次重新创建客户端。"""
    global _client
    _client = None


def ping_redis() -> bool:
    """探活，给健康检查用。Redis 不可用不抛异常，只返回 False。"""
    try:
        return bool(get_redis().ping())
    except RedisError as exc:
        log.warning("Redis 不可用: %s", exc)
        return False


def _sanitize_redis_url(url: str) -> str:
    """日志里不输出密码（如果有）。"""
    if "@" in url:
        return url.split("@", 1)[-1]
    return url


def app_key(*parts: str) -> str:
    """统一 key 生成：aiiv:{env}:{part1}:{part2}...（§9.1）。

    环境标识不是装饰：同一个 Redis 实例上跑 dev 与 prod 时，没有它就会
    互相清锁、互相读到别人的幂等缓存。
    """
    env = get_settings().env_name or "dev"
    return f"{KEY_PREFIX}:{env}:" + ":".join(str(p) for p in parts)
