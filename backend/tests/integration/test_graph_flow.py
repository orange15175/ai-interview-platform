"""面试图全流程集成测试（MemorySaver + mock 模型，不联网）。

验证的是图的行为契约（开发说明 §6/§7）：
1. interrupt payload 不含 evaluation_rubric（泄题 = 安全事故）；
2. 回答 → 评分 → 规则链放行 → 追问 → 再回答 → 规则链拦截（重复方向）→ 推进；
3. 跳过：不产生评分记录、直接推进；
4. 空回答：invoke 抛错且状态停在等待点，重试后流程继续（无半截状态）；
5. 有限步内必然收尾（防死循环）——3 道主问题 × 每题最多 1 次 mock 追问。
"""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph import build_interview_graph


def _questions(n: int) -> list[dict]:
    return [
        {
            "main_index": i,
            "kind": "main",
            "category": ["项目经验", "专业能力", "场景解决"][i % 3],
            "difficulty": ["easy", "medium", "hard"][i % 3],
            "content": f"第 {i} 题：请结合项目说明你做过的第 {i} 个技术决策的完整权衡过程。",
            "material_evidence_ids": ["ev-1"],
            "evaluation_rubric": [
                {"point": "讲清背景与权衡", "weight": 0.5},
                {"point": "有量化结果", "weight": 0.5},
            ],
        }
        for i in range(1, n + 1)
    ]


def _start(graph, n: int):
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = graph.invoke({"session_id": "s1", "questions": _questions(n)}, cfg)
    return cfg, result


def _interrupt_value(result: dict) -> dict:
    return result["__interrupt__"][0].value


def test_full_flow_with_followups_and_termination():
    graph = build_interview_graph(checkpointer=MemorySaver())
    cfg, result = _start(graph, 3)

    seen: list[dict] = []
    for step in range(6):  # 3 主 + 3 追问 = 6 次等待；超过即死循环
        payload = _interrupt_value(result)
        seen.append(payload)
        # 安全断言：payload 绝不能带评分要点
        assert "evaluation_rubric" not in payload
        result = graph.invoke(Command(resume={"answer": f"关于第 {step} 题的回答，做了取舍与量化。"}), cfg)

    assert result.get("finished") is True
    # 题目顺序：main1 → followup1 → main2 → followup2 → main3 → followup3
    kinds = [(p["main_index"], p["follow_up_index"]) for p in seen]
    assert kinds == [(1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1)]

    state = graph.get_state(cfg)
    # 6 次回答全部产生评分（skipped 不会出现在这条链路里）
    evals = state.values["evaluations"]
    assert len(evals) == 6 and all(e is not None for e in evals)
    # 每道主问题 2 条裁决：第 1 条放行追问，第 2 条因方向重复停止
    decisions = state.values["followup_decisions"]
    assert len(decisions) == 6
    asks = [d for d in decisions if d["decision"] == "ask"]
    stops = [d for d in decisions if d["decision"] == "stop"]
    assert len(asks) == 3 and len(stops) == 3
    assert all(d["reason_code"] == "SUGGESTED" for d in asks)
    assert all(d["reason_code"] == "DUPLICATE_TARGET" for d in stops)
    assert all(d["rule_version"] for d in decisions)
    # 已问清单 = 3 主 + 3 追问
    assert len(state.values["asked_contents"]) == 6


def test_skip_answer_advances_without_evaluation():
    graph = build_interview_graph(checkpointer=MemorySaver())
    cfg, result = _start(graph, 2)

    # 跳过第 1 题
    result = graph.invoke(Command(resume={"skipped": True}), cfg)
    payload = _interrupt_value(result)
    assert payload["main_index"] == 2  # 直接进入第 2 题

    # 正常回答第 2 题 → mock 会追问一次
    result = graph.invoke(Command(resume={"answer": "正常回答第 2 题，含取舍与量化。"}), cfg)
    assert _interrupt_value(result)["follow_up_index"] == 1

    state = graph.get_state(cfg)
    # 第 1 题无评分记录（None），第 2 题有
    evals = state.values["evaluations"]
    assert evals[0] is None
    assert evals[1] is not None
    # 第 1 题的裁决是 SKIPPED
    assert state.values["followup_decisions"][0]["reason_code"] == "SKIPPED"


def test_empty_answer_reinterrupts_with_error_then_recovers():
    """非法回答不烧检查点：带 error 重新中断，客户端修正后流程继续。

    背景：如果节点抛错，失败的 resume 值会留在 LangGraph 的 pending
    writes 里，重试会被旧值重放（实测行为）。所以 wait_answer 对非法
    输入采用「重新 interrupt + error 字段」而不是抛异常。
    """
    graph = build_interview_graph(checkpointer=MemorySaver())
    cfg, result = _start(graph, 1)

    # 空回答 → 不抛错，而是带 error 的重新中断
    result = graph.invoke(Command(resume={"answer": "   "}), cfg)
    payload = _interrupt_value(result)
    assert "error" in payload
    assert "回答内容为空" in payload["error"]
    assert payload["content"]  # 题目还在，候选人可以重新作答
    assert graph.get_state(cfg).next == ("wait_answer",)

    # 修正后重试 → 正常放行（mock 会建议追问）
    result = graph.invoke(Command(resume={"answer": "带取舍与量化的完整回答。"}), cfg)
    assert _interrupt_value(result)["follow_up_index"] == 1


def test_followup_generation_failure_advances_without_reasking(monkeypatch):
    """追问生成失败（修复后仍非法）→ 记 FOLLOWUP_FAILED → 直接推进主流程。

    回归背景：早期版本失败后仍走 wait_answer，导致候选人看到
    已答过的主问题被再问一遍（pending_question 停留在旧题上）。
    """
    from app.core.errors import ModelOutputInvalidError

    def boom(*args, **kwargs):
        raise ModelOutputInvalidError("追问输出一直不合法")

    monkeypatch.setattr("app.graph.nodes.generate_followup", boom)

    graph = build_interview_graph(checkpointer=MemorySaver())
    cfg, result = _start(graph, 2)

    # 回答第 1 题 → mock 建议追问 → 追问生成失败 → 应直接进入第 2 题
    result = graph.invoke(Command(resume={"answer": "正常回答第 1 题。"}), cfg)
    payload = _interrupt_value(result)
    assert payload["main_index"] == 2
    assert payload["follow_up_index"] == 0  # 是新主问题，不是旧题重问

    state = graph.get_state(cfg).values
    last = state["followup_decisions"][-1]
    assert last["decision"] == "stop"
    assert last["reason_code"] == "FOLLOWUP_FAILED"


def test_checkpoint_continuation_and_accumulation():
    """检查点语义验证：
    - 回答后图停在 wait_answer，可再恢复（追问等待点）；
    - 追问回答后正常收尾，评分记录按顺序累积、无重复追加。

    注意契约：被中断的 thread 上恢复只能用 Command(resume=...)；
    直接传新输入会从 START 重新开一轮（LangGraph 语义，API 层必须遵守）。
    """
    graph = build_interview_graph(checkpointer=MemorySaver())
    cfg, result = _start(graph, 1)

    # 回答主问题 → mock 放行追问 → 停在追问的等待点
    result = graph.invoke(Command(resume={"answer": "第一次回答。"}), cfg)
    assert _interrupt_value(result)["follow_up_index"] == 1
    assert graph.get_state(cfg).next == ("wait_answer",)
    evals_after_first = graph.get_state(cfg).values["evaluations"]
    assert len(evals_after_first) == 1

    # 追问回答 → 规则链因方向重复停止 → 唯一主问题收尾
    result = graph.invoke(Command(resume={"answer": "追问回答。"}), cfg)
    assert result.get("finished") is True

    state = graph.get_state(cfg).values
    # 两条评分记录（主问题 + 追问），第二次恢复没有重放第一次的评分
    assert len(state["evaluations"]) == 2
    assert state["evaluations"][0] == evals_after_first[0]
    decisions = state["followup_decisions"]
    assert [d["decision"] for d in decisions] == ["ask", "stop"]
    assert decisions[1]["reason_code"] == "DUPLICATE_TARGET"
