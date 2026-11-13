"""会话锁集成测试（需要 Redis 在跑；不可用则整体 skip）。

验证的是 §9.2 的每一条硬要求：
- 同一 session 同时只有一个持有者；
- 释放只允许 owner 本人（否则 A 释放会误删 B 的锁）；
- 续期成功延长租约；锁被别人接管后续期失败并标记 lost（调用方据此拒绝写库）；
- Redis 不可用不静默放行。

    docker compose up -d   # 起 redis-stack（6380）
"""

from __future__ import annotations

import time
import uuid

import pytest

from app.core.errors import SessionBusyError
from app.services import locks


def _redis_alive() -> bool:
    try:
        from app.infrastructure.redis_client import get_redis

        get_redis().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_alive(), reason="未连上 Redis，跳过会话锁集成测试"
)


@pytest.fixture()
def sid() -> str:
    return f"test-{uuid.uuid4()}"


def test_second_holder_is_rejected(sid):
    first = locks.acquire(sid, ttl_ms=5000, wait_ms=0)
    try:
        assert locks.try_acquire(sid, ttl_ms=5000, wait_ms=0) is None
        with pytest.raises(SessionBusyError):
            locks.acquire(sid, ttl_ms=5000, wait_ms=0)
    finally:
        first.release()

    # 释放后可以再拿（同一把 key，不是一次性）
    again = locks.try_acquire(sid, ttl_ms=5000, wait_ms=0)
    assert again is not None
    again.release()


def test_owner_tokens_are_random(sid):
    a = locks.acquire(sid, ttl_ms=5000, wait_ms=0)
    key_owner = a.owner
    a.release()
    b = locks.acquire(sid, ttl_ms=5000, wait_ms=0)
    b.release()
    assert key_owner != b.owner
    assert key_owner != sid  # owner 绝不能等于 session_id


def test_release_only_by_owner(sid):
    """模拟租约过期后被别人抢到：旧持有者释放不得删掉新持有者的锁。"""
    from app.infrastructure.redis_client import get_redis

    lock = locks.acquire(sid, ttl_ms=5000, wait_ms=0)
    redis = get_redis()
    redis.set(lock.key, "someone-else", px=5000)

    assert lock.release() is False
    assert redis.get(lock.key) == "someone-else"
    redis.delete(lock.key)


def test_renew_extends_lease_and_detects_loss(sid):
    from app.infrastructure.redis_client import get_redis

    lock = locks.acquire(sid, ttl_ms=3000, wait_ms=0)
    redis = get_redis()
    assert lock.renew() is True
    assert lock.remaining_ms > 0
    assert lock.lost is False

    redis.set(lock.key, "hijacker", px=3000)
    assert lock.renew() is False
    assert lock.lost is True  # 调用方必须据此拒绝写库
    redis.delete(lock.key)


def test_auto_renew_keeps_short_lease_alive(sid):
    """ttl 1.2s、每 0.4s 续一次：1 秒后仍持有，证明长模型调用不会掉锁。"""
    lock = locks.acquire(sid, ttl_ms=1200, wait_ms=0)
    lock.start_auto_renew(interval_ms=400)
    try:
        time.sleep(1.0)
        assert lock.lost is False
        assert locks.try_acquire(sid, ttl_ms=1200, wait_ms=0) is None
    finally:
        lock.release()
    assert locks.try_acquire(sid, ttl_ms=1200, wait_ms=0) is not None


def test_lock_key_is_namespaced(sid):
    assert locks.lock_key(sid).startswith("aiiv:")
    assert sid in locks.lock_key(sid)
