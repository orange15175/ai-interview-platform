"""编译好的图实例与调用辅助（开发说明 §6.2 / §9.4）。

为什么要有这一层：图的调用细节（thread_id 约定、interrupt payload 在哪、
"重试是 resume 还是 continue"）散落在 API、CLI、测试里最容易各写一套、
各错一处。集中在这里，所有人用同一套语义。

三条语义，务必记住（都是实测踩出来的）：
1. **thread_id 必须等于 session_id**（字符串）。换一个就等于换一场面试。
2. 被 **interrupt** 的 thread 只能用 `Command(resume=...)` 恢复；
   直接 invoke 新输入会从 START 重开一轮。
3. 节点**抛错**后 thread 停在 `next=(节点,)` 且没有 interrupt —— 重试要用
   `invoke(None, cfg)` 从断点继续，而不是再 resume（没有 pending interrupt）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core.logging import get_logger
from app.graph.builder import build_interview_graph
from app.infrastructure.checkpoint import get_checkpointer

log = get_logger(__name__)


@lru_cache(maxsize=4)
def _build_graph(restore: bool = False) -> CompiledStateGraph:
    """实际编译（不直接对外用，见 get_compiled_graph 的自愈逻辑）。"""
    return build_interview_graph(checkpointer=get_checkpointer(), restore=restore)


def get_compiled_graph(restore: bool = False) -> CompiledStateGraph:
    """进程内缓存编译结果，并保证与 checkpointer 单例**永远一致**。

    编译本身不贵，但每次新建 RedisSaver 会重跑 setup 索引检查；缓存后
    所有请求共享同一份图定义（图是无状态的，状态全在 checkpointer）。

    自愈：谁若在中途 reset 了 checkpointer 单例（测试/运维），缓存里的图
    还抱着旧 saver——两个实例的 _key_registry 不互通，get_state 会炸。
    这里发现单例换了就主动重建，杜绝"图与单例失联"。
    """
    graph = _build_graph(restore)
    saver = get_checkpointer()
    if graph.checkpointer is not saver:
        _build_graph.cache_clear()
        graph = _build_graph(restore)
    return graph


def build_graph_with(saver: BaseCheckpointSaver, *, restore: bool = False) -> CompiledStateGraph:
    """显式指定 checkpointer 编译（测试用 MemorySaver，CLI 也走它）。"""
    return build_interview_graph(checkpointer=saver, restore=restore)


def reset_graph_cache() -> None:
    _build_graph.cache_clear()


def thread_config(session_id) -> dict[str, Any]:
    return {"configurable": {"thread_id": str(session_id)}}


def interrupt_payload(result: Any) -> dict | None:
    """从 invoke 返回值里取中断 payload（没有中断时返回 None）。"""
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__") or ()
    if not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    return value if isinstance(value, dict) else None


def snapshot_state(graph: CompiledStateGraph, session_id) -> tuple[dict, tuple[str, ...], bool]:
    """返回 (状态值, 下一批节点, 是否在等待回答)。

    `是否在等待` 用 snapshot.interrupts 判断，比看 next 更准：
    节点抛错时 next 也有值，但没有 interrupt。
    """
    snap = graph.get_state(thread_config(session_id))
    waiting = bool(getattr(snap, "interrupts", ()))
    return dict(snap.values or {}), tuple(snap.next or ()), waiting


def has_checkpoint(graph: CompiledStateGraph, session_id) -> bool:
    """Redis 里是否还有这场会话的执行状态。

    空 values + 无 next + 无 interrupt = 没有检查点，必须走恢复入口（§9.4）。
    """
    values, next_nodes, waiting = snapshot_state(graph, session_id)
    return bool(values) or bool(next_nodes) or waiting


def resume_answer(graph: CompiledStateGraph, session_id, payload: dict) -> Any:
    """在等待点恢复：payload 形如 {"answer": "..."} 或 {"skipped": True}。"""
    return graph.invoke(Command(resume=payload), thread_config(session_id))


def continue_run(graph: CompiledStateGraph, session_id) -> Any:
    """从失败节点继续（没有 pending interrupt 时用这个）。"""
    return graph.invoke(None, thread_config(session_id))


def delete_thread(saver: BaseCheckpointSaver, session_id) -> None:
    """删除 thread 的检查点（恢复前清掉脏状态；RedisSaver 才支持）。"""
    thread_id = str(session_id)
    try:
        saver.delete_thread(thread_id)  # type: ignore[attr-defined]
    except AttributeError:
        log.debug("当前 checkpointer 不支持 delete_thread，跳过清理")
    except Exception as exc:  # noqa: BLE001 —— 清理失败不阻断恢复，由后续检查兜底
        log.warning("清理检查点失败 thread=%s: %s", thread_id, exc)
