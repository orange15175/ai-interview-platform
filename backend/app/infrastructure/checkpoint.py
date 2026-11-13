"""LangGraph Redis Checkpointer 工厂（开发说明 §9.1 / §9.4）。

- 用 langgraph-checkpoint-redis 的 RedisSaver（依赖 redis-stack 的 JSON/Search 模块）。
- **必须是进程内单例**：RedisSaver 的 `_key_registry` 在 setup() 之后才可用，
  而 setup() 只在首次调用时执行。若每次返回新实例，后创建的那个实例 registry
  为 None，`get_state()` 会直接 AttributeError（实测踩过：恢复变体图的
  get_state 抛 'NoneType' object has no attribute 'make_write_keys_zset_key'，
  症状是"图明明停在 wait_answer，却查不到 interrupt"）。单例同时省掉重复建索引。
- Checkpoint 只存「执行位置 + 临时上下文」，正式业务数据一律以 PostgreSQL 为准；
  Redis 丢失后的重建在 services/recovery.py。
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.redis import RedisSaver

from app.core.logging import get_logger
from app.infrastructure.redis_client import get_redis

log = get_logger(__name__)

_saver: BaseCheckpointSaver | None = None


def get_checkpointer() -> BaseCheckpointSaver:
    """惰性单例：整个进程共用同一个 RedisSaver（含已建好的索引与 registry）。"""
    global _saver
    if _saver is None:
        client = get_redis()
        saver = RedisSaver(redis_client=client)
        # setup() 建 RediSearch 索引，只在 redis-stack 上可用；幂等。
        saver.setup()
        _saver = saver
        log.info("Redis Checkpointer 已就绪（redis-stack，进程内单例）")
    return _saver


def reset_checkpointer_cache() -> None:
    """测试/运维用：丢弃单例（下次调用重新 setup）。"""
    global _saver
    _saver = None
