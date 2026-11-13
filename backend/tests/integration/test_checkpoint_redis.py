"""Redis Checkpointer 集成测试（需要 redis-stack 容器在跑）。

验证的是「跨 invoke 的状态持久化」——这是阶段 4 做 interrupt/resume、
进程重启后恢复面试的物理基础。Redis 不可用时整体 skip，不阻塞纯单测流程。

    docker compose up -d      # 起 PG + redis-stack
    pytest tests/integration/test_checkpoint_redis.py
"""

from __future__ import annotations

import uuid
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from app.core.config import get_settings
from app.infrastructure.checkpoint import get_checkpointer, reset_checkpointer_cache


def _redis_alive() -> bool:
    """先探活再决定是否 skip。导入期不做网络 IO。"""
    try:
        from app.infrastructure.redis_client import get_redis

        get_redis().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_alive(),
    reason="未连上 Redis（redis-stack 容器未启动），跳过 checkpoint 集成测试",
)


class Counter(TypedDict, total=False):
    n: int


def _build_graph():
    builder = StateGraph(Counter)
    builder.add_node("inc", lambda s: {"n": s.get("n", 0) + 1})
    builder.add_edge(START, "inc")
    builder.add_edge("inc", END)
    return builder.compile(checkpointer=get_checkpointer())


def test_checkpoint_survives_across_invocations():
    """第二次只传空 state，仍应读到上次写入的 n —— 证明状态来自 Redis 而非内存。"""
    reset_checkpointer_cache()   # 保证 setup() 索引建好
    graph = _build_graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}

    assert graph.invoke({"n": 0}, cfg) == {"n": 1}
    assert graph.invoke({}, cfg) == {"n": 2}
    assert graph.get_state(cfg).values == {"n": 2}


def test_different_threads_are_isolated():
    """不同 thread_id 互不串线——多用户并发的正确性前提。"""
    graph = _build_graph()
    cfg_a = {"configurable": {"thread_id": str(uuid.uuid4())}}
    cfg_b = {"configurable": {"thread_id": str(uuid.uuid4())}}

    graph.invoke({"n": 10}, cfg_a)
    graph.invoke({"n": 100}, cfg_b)

    assert graph.get_state(cfg_a).values == {"n": 11}
    assert graph.get_state(cfg_b).values == {"n": 101}


def test_settings_point_to_compose_ports():
    """配置默认值必须指向 compose 映射的 5433/6380，否则连不上容器。"""
    s = get_settings()
    assert ":5433/" in s.database_url
    assert ":6380/" in s.redis_url


def test_checkpointer_is_singleton_and_variants_share_it():
    """两个编译变体必须共用同一个 RedisSaver。

    回归背景：每次 get_checkpointer() 都 new 一个 RedisSaver 时，只有第一个
    实例跑过 setup()，第二个实例的 _key_registry 为 None——它的 get_state()
    会 AttributeError，表现为"图停在 wait_answer 却查不到 interrupt"。
    """
    from app.services import graph_runtime

    saver = get_checkpointer()
    assert get_checkpointer() is saver

    graph = graph_runtime.get_compiled_graph()
    restore_graph = graph_runtime.get_compiled_graph(restore=True)
    assert graph.checkpointer is saver and restore_graph.checkpointer is saver


def test_restore_variant_interrupt_is_visible_to_normal_graph():
    """恢复变体写入的中断，普通图必须读得到（跨图读取同一 thread）。"""
    from app.services import graph_runtime

    graph = graph_runtime.get_compiled_graph()
    restore_graph = graph_runtime.get_compiled_graph(restore=True)
    thread = f"cp-test-{uuid.uuid4()}"

    seed = {
        "session_id": thread,
        "questions": [
            {
                "main_index": 1,
                "kind": "main",
                "category": "项目经验",
                "difficulty": "medium",
                "content": "请说明你做过的关键技术取舍与量化结果。",
                "material_evidence_ids": [],
                "evaluation_rubric": [{"point": "讲清取舍", "weight": 1.0}],
            }
        ],
        "current_main_index": 1,
        "current_followup_index": 0,
        "pending_question": {
            "main_index": 1,
            "kind": "main",
            "follow_up_index": 0,
            "content": "请说明你做过的关键技术取舍与量化结果。",
            "category": "项目经验",
            "difficulty": "medium",
            "evaluation_rubric": [{"point": "讲清取舍", "weight": 1.0}],
            "material_evidence_ids": [],
        },
        "evaluations": [],
        "followup_decisions": [],
        "asked_contents": [],
        "finished": False,
    }
    try:
        restore_graph.invoke(seed, {"configurable": {"thread_id": thread}})
        _, next_nodes, waiting = graph_runtime.snapshot_state(graph, thread)
        assert waiting is True
        assert next_nodes == ("wait_answer",)
    finally:
        graph.checkpointer.delete_thread(thread)  # type: ignore[union-attr]
