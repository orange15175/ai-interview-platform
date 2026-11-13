"""会话级分布式锁（开发说明 §9.2）。

为什么需要：同一场面试的两个请求（用户连点两次、两个 worker、重试与超时竞态）
如果并行推进，会出现"两道题都被判为当前题""同一题被评分两次"。锁把修改操作
串行化；数据库的 state_version CAS 是锁失效时的最后一道闸。

实现要点（照抄 §9.2 逐条）：
1. key 按 session_id，不按用户、不用全局锁 —— 否则一个用户卡住所有人。
2. 原子获取：`SET key owner NX PX ttl`，owner 是随机 token，不是会话 ID
   （否则 A 释放时可能误删 B 刚拿到的锁）。
3. 释放与续期都用 Lua 做"先比对 owner 再操作"，保证不会误删别人的锁。
4. 租约续期：模型调用可能超过 ttl，后台线程按 ttl/3 续期；续期失败即标记
   `lost=True`，调用方必须在写库前检查——锁丢了就不许写。
5. Redis 不可用 → LockUnavailableError（503），绝不无锁继续写。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.errors import LockUnavailableError, SessionBusyError
from app.core.logging import get_logger
from app.infrastructure.redis_client import app_key, get_redis

log = get_logger(__name__)

# 释放：owner 匹配才删；返回 1 表示确实由本次持有者释放
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# 续期：owner 匹配才延长；返回 1 表示续期成功
_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


def lock_key(session_id: str) -> str:
    return app_key("lock", "session", session_id)


class SessionLock:
    """已持有的会话锁。用 with 语句或 try/finally 保证释放。"""

    def __init__(self, client: Redis, session_id: str, owner: str, ttl_ms: int):
        self._client = client
        self.session_id = session_id
        self.owner = owner
        self.ttl_ms = ttl_ms
        self.key = lock_key(session_id)
        self._lost = threading.Event()      # 续期失败/被别人抢走 → 置位
        self._stop = threading.Event()      # 通知续期线程退出
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 状态
    @property
    def lost(self) -> bool:
        """锁是否已经失去。写库前必须检查（§9.2 租约失效场景）。"""
        return self._lost.is_set()

    @property
    def remaining_ms(self) -> int:
        try:
            ttl = self._client.pttl(self.key)
        except RedisError:
            return -2
        return int(ttl)

    # ------------------------------------------------------------ 续期与释放
    def renew(self) -> bool:
        """续期一次；失败即视为失去锁（不重试、不忽略）。"""
        try:
            ok = bool(self._client.eval(_RENEW_LUA, 1, self.key, self.owner, self.ttl_ms))
        except RedisError as exc:
            log.warning("会话锁续期失败（Redis 异常）session=%s: %s", self.session_id, exc)
            self._lost.set()
            return False
        if not ok:
            log.warning("会话锁已不属于本请求（租约过期或被抢占）session=%s", self.session_id)
            self._lost.set()
        return ok

    def release(self) -> bool:
        """释放锁：只有 owner 匹配才删。已在别的请求手里时什么都不做。"""
        self.stop_auto_renew()
        try:
            return bool(self._client.eval(_RELEASE_LUA, 1, self.key, self.owner))
        except RedisError as exc:
            # 释放失败不会造成正确性问题（租约会自动过期），但必须留痕
            log.warning("会话锁释放失败 session=%s: %s", self.session_id, exc)
            return False

    def start_auto_renew(self, interval_ms: int | None = None) -> None:
        """启动后台续期线程（模型调用动辄 30~90 秒，必须续）。"""
        if self._thread is not None:
            return
        interval = interval_ms or max(int(self.ttl_ms / 3), 1000)

        def _loop() -> None:
            while not self._stop.wait(interval / 1000):
                if not self.renew():
                    return

        self._thread = threading.Thread(
            target=_loop, name=f"session-lock-{self.session_id[:8]}", daemon=True
        )
        self._thread.start()

    def stop_auto_renew(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._thread = None

    # ------------------------------------------------------------ 上下文管理
    def __enter__(self) -> "SessionLock":
        self.start_auto_renew()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def try_acquire(
    session_id: str,
    *,
    ttl_ms: int | None = None,
    wait_ms: int | None = None,
    client: Redis | None = None,
    poll_ms: int = 50,
) -> SessionLock | None:
    """尝试拿锁，拿不到返回 None（不抛错，供"先试一次"的场景用）。

    等待时长刻意很短（默认 800ms）：HTTP 请求不该堵在别人的模型调用后面，
    拿不到就返回 SESSION_BUSY，让客户端用同一个 request_id 重试。
    """
    settings = get_settings()
    ttl = ttl_ms or settings.business.session_lock_ttl_ms
    wait = settings.business.session_lock_wait_ms if wait_ms is None else wait_ms
    redis = client or get_redis()
    key = lock_key(str(session_id))
    owner = uuid.uuid4().hex  # 随机 owner：不能用 session_id 当 owner

    deadline = time.monotonic() + max(wait, 0) / 1000
    while True:
        try:
            if redis.set(key, owner, nx=True, px=ttl):
                return SessionLock(redis, str(session_id), owner, ttl)
        except RedisError as exc:
            # 锁服务不可用：不许绕过（§9.2），明确报 503
            raise LockUnavailableError() from exc

        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_ms / 1000)


def acquire(
    session_id: str,
    *,
    ttl_ms: int | None = None,
    wait_ms: int | None = None,
    client: Redis | None = None,
) -> SessionLock:
    """拿锁，拿不到抛 SessionBusyError（幂等重试仍可成功，故 retryable）。"""
    lock = try_acquire(session_id, ttl_ms=ttl_ms, wait_ms=wait_ms, client=client)
    if lock is None:
        raise SessionBusyError()
    return lock


def lock_debug_info(session_id: str, client: Redis | None = None) -> dict[str, Any]:
    """排查用：看当前锁的 owner 与剩余租约（不返回完整 owner）。"""
    redis = client or get_redis()
    key = lock_key(str(session_id))
    try:
        owner = redis.get(key)
        ttl = redis.pttl(key)
    except RedisError as exc:
        return {"available": False, "error": str(exc)[:100]}
    return {
        "available": True,
        "held": owner is not None,
        "owner_prefix": (owner[:8] + "…") if owner else None,
        "remaining_ms": int(ttl),
    }


def describe_lock_session(session_id: str) -> str:
    """日志用：锁 key 的可读描述（不含明文 owner）。"""
    return json.dumps(lock_debug_info(session_id), ensure_ascii=False)
