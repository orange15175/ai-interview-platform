"""新面试流程图的组装（开发说明 §6）。

    START -> prepare -> dispatch -> wait_answer -> evaluate_answer
                                                          |
                                            build_followup <- decide
                                                          |
                          advance <-----------------------  (stop)
                            |                                ^
                            +-> dispatch（未收尾）             |
                                                            ask
                            +-> END（收尾）

调用方式（CLI / API 层）：
    graph = build_interview_graph(checkpointer=...)
    cfg = {"configurable": {"thread_id": session_id}}
    result = graph.invoke({"session_id": ..., "questions": [...]}, cfg)
    # result["__interrupt__"][0].value  ← 当前题 payload（无评分要点）
    # 候选人提交后：
    graph.invoke(Command(resume={"answer": "..."}), cfg)
    # 跳过：
    graph.invoke(Command(resume={"skipped": True}), cfg)
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.graph import nodes
from app.graph.state import InterviewGraphState


def build_interview_graph(
    checkpointer: BaseCheckpointSaver | None = None, *, restore: bool = False
):
    """编译面试图。

    checkpointer 决定会话存在哪（Memory / Redis）。
    restore=True 时编译**恢复变体**：START 直接进 wait_answer，跳过
    prepare/dispatch。用于 Redis checkpoint 丢失后从 PostgreSQL 重建
    （§9.4）——由调用方把材料/题目/已答记录/进度直接作为 invoke 的初始
    state 灌进去，图会真的在 wait_answer 处 interrupt，后续仍走
    Command(resume=...)，而不是"往空 thread 里塞字段假装恢复"。
    """
    builder = StateGraph(InterviewGraphState)

    builder.add_node("prepare", nodes.prepare)
    builder.add_node("dispatch", nodes.dispatch)
    builder.add_node("wait_answer", nodes.wait_answer)
    builder.add_node("evaluate_answer", nodes.evaluate_answer_node)
    builder.add_node("decide", nodes.decide_followup_node)
    builder.add_node("build_followup", nodes.build_followup_node)
    builder.add_node("advance", nodes.advance_node)

    if restore:
        # 恢复变体：初始 state 已由服务层从数据库重建，直接停在等待点
        builder.add_edge(START, "wait_answer")
    else:
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "dispatch")
        builder.add_edge("dispatch", "wait_answer")

    builder.add_edge("wait_answer", "evaluate_answer")
    builder.add_edge("evaluate_answer", "decide")

    # 规则链放行 → 生成追问，追问也走等待-评分循环（不越题：main_index 不变）
    builder.add_conditional_edges(
        "decide",
        nodes.route_after_decision,
        {"build_followup": "build_followup", "advance": "advance"},
    )
    # 追问生成失败（FOLLOWUP_FAILED）→ 放弃追问直接推进，绝不重问已答过的题
    builder.add_conditional_edges(
        "build_followup",
        nodes.route_after_build_followup,
        {"wait_answer": "wait_answer", "advance": "advance"},
    )

    # 收束：进入下一道主问题，或全部问完
    builder.add_conditional_edges(
        "advance",
        nodes.route_after_advance,
        {"dispatch": "dispatch", "__end__": END},
    )

    return builder.compile(checkpointer=checkpointer)
