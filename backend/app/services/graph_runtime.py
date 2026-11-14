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

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core.logging import get_logger
from app.graph.builder import build_interview_graph
from app.infrastructure.checkpoint import get_checkpointer

log = get_logger(__name__)

# 图侧状态分类。**不要**用"有没有 checkpoint"当唯一判断：
# 一个 thread 有 checkpoint，也可能停在"节点抛错可继续"或"已跑完"的位置，
# 这两种状态下把旧回答再喂进去，等于给下一题/已结束的会话乱塞输入。
GRAPH_WAITING = "WAITING_INTERRUPT"     # 停在 interrupt，等某道题的回答
GRAPH_NODE_FAILED = "NODE_INTERRUPTED"  # 节点抛错，next 非空但没有 interrupt
GRAPH_FINISHED = "GRAPH_FINISHED"       # 图已跑完（无 next、无 interrupt）
GRAPH_MISSING = "NO_CHECKPOINT"         # 没有任何执行状态


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


@dataclass(frozen=True)
class GraphSnapshot:
    """图在**此刻**的执行位置（对账用）。

    值必须一次性取齐：分两次 get_state 之间别的请求可能推进图，
    拿到的"等待题"和"状态值"就会互相矛盾。
    """

    values: dict = field(default_factory=dict)
    next_nodes: tuple[str, ...] = ()
    waiting: bool = False
    interrupt: dict | None = None

    @property
    def stage(self) -> str:
        if self.waiting:
            return GRAPH_WAITING
        if self.next_nodes:
            return GRAPH_NODE_FAILED
        if self.values:
            return GRAPH_FINISHED
        return GRAPH_MISSING

    @property
    def wait_main_index(self) -> int | None:
        if not self.interrupt:
            return None
        value = self.interrupt.get("main_index")
        return value if isinstance(value, int) else None

    @property
    def wait_follow_up_index(self) -> int | None:
        if not self.interrupt:
            return None
        value = self.interrupt.get("follow_up_index", 0)
        return value if isinstance(value, int) else None

    def waits_for(self, main_index: int, follow_up_index: int) -> bool | None:
        """图等的是不是这道题。

        None 表示**无法判断**（不在等待点 / payload 缺坐标）——
        调用方必须把 None 与 False 区别对待，不能当成"不是它"。
        """
        if not self.waiting:
            return None
        m, f = self.wait_main_index, self.wait_follow_up_index
        if m is None or f is None:
            return None
        return (m, f) == (main_index, follow_up_index)


def snapshot(graph: CompiledStateGraph, session_id) -> GraphSnapshot:
    """取一次完整快照（值 + 下一批节点 + 中断 payload）。"""
    snap = graph.get_state(thread_config(session_id))
    interrupts = tuple(getattr(snap, "interrupts", ()) or ())
    payload = None
    if interrupts:
        value = getattr(interrupts[0], "value", None)
        payload = value if isinstance(value, dict) else None
    return GraphSnapshot(
        values=dict(snap.values or {}),
        next_nodes=tuple(snap.next or ()),
        waiting=bool(interrupts),
        interrupt=payload,
    )


def snapshot_state(graph: CompiledStateGraph, session_id) -> tuple[dict, tuple[str, ...], bool]:
    """返回 (状态值, 下一批节点, 是否在等待回答)。

    `是否在等待` 用 snapshot.interrupts 判断，比看 next 更准：
    节点抛错时 next 也有值，但没有 interrupt。
    """
    snap = snapshot(graph, session_id)
    return snap.values, snap.next_nodes, snap.waiting


def has_checkpoint(graph: CompiledStateGraph, session_id) -> bool:
    """Redis 里是否还有这场会话的执行状态。

    空 values + 无 next + 无 interrupt = 没有检查点，必须走恢复入口（§9.4）。
    注意：这只回答"有没有"，不回答"能不能继续"——后者用 snapshot().stage。
    """
    snap = snapshot(graph, session_id)
    return snap.stage != GRAPH_MISSING


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
