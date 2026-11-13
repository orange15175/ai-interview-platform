"""评分 / 追问 Agent 单元测试（mock 驱动，不联网）。

重点验证行为约束，而不是 mock 数据本身：
- 评分：维度分齐全时总分由代码重算；不合法输出 → 修复一次 → 仍失败抛可重试错误；
- 追问：单问题；与已问清单重复 → 修复 → 仍重复抛错。
"""

from __future__ import annotations

import pytest

from app.agents.adapters import MockTextAdapter
from app.agents.evaluate_agent import evaluate_answer
from app.agents.followup_agent import generate_followup
from app.core.errors import ModelCallError, ModelOutputInvalidError
from app.schemas.evaluation import Evaluation, FollowUpQuestion

# 与 mock 评分 rubric_results（1 条）不同长度 → 触发"要点齐全才校验"的分支
_QUESTION = {
    "main_index": 1,
    "kind": "main",
    "category": "项目经验",
    "difficulty": "medium",
    "content": "请介绍你的项目中最有挑战的技术决策，当时的权衡是什么？",
    "evaluation_rubric": [
        {"point": "讲清背景与权衡", "weight": 0.5},
        {"point": "有量化结果", "weight": 0.5},
    ],
}


class _FixedAdapter:
    """按预设序列返回输出的假适配器：测失败/修复路径。"""

    def __init__(self, outputs: list[str], fail_first_with: Exception | None = None):
        self.outputs = list(outputs)
        self.calls = 0
        self.fail_first_with = fail_first_with

    def complete(self, system, user, *, temperature, max_tokens=None, json_mode=False):
        self.calls += 1
        if self.calls == 1 and self.fail_first_with is not None:
            raise self.fail_first_with
        return self.outputs.pop(0)


def test_evaluate_with_mock_adapter_overrides_score_by_code():
    ev = evaluate_answer(_QUESTION, "我做了 A，权衡了 B，量化了 C", adapter=MockTextAdapter())
    assert isinstance(ev, Evaluation)
    # mock 维度分齐全 → 代码重算 0.4*80+0.2*70+0.2*80+0.15*75+0.05*85 = 77.5 → 78
    assert ev.score == 78
    assert ev.score_source == "code"
    assert ev.follow_up_suggested is True


def test_evaluate_invalid_then_valid_repair_once():
    bad = _FixedAdapter(["这不是 JSON", "{\"score\": 60, \"feedback\": \"ok\"}"])
    ev = evaluate_answer(_QUESTION, "回答", adapter=bad)
    assert ev.score == 60
    assert bad.calls == 2  # 第一次失败，修复一次成功


def test_evaluate_all_invalid_raises_retryable_error():
    bad = _FixedAdapter(["垃圾", "还是垃圾"])
    with pytest.raises(ModelOutputInvalidError):
        evaluate_answer(_QUESTION, "回答", adapter=bad)


def test_evaluate_rubric_inconsistency_normalizes_score():
    """要点全 yes 期望 100 分，模型两次都给 40：修复无效 → 信要点、修总分。

    真实模型实测（DeepSeek）：要点判定严格可靠、总分常常虚高，所以
    持续背离时用要点期望分归一化（score_source=code + feedback 说明），
    绝不让评分环节卡死整场面试，也绝不静默采用虚高的总分。
    """
    inconsistent = (
        '{"score": 40, "feedback": "回答完整", '
        '"rubric_results": ['
        '{"point": "讲清背景与权衡", "met": "yes", "note": ""},'
        '{"point": "有量化结果", "met": "yes", "note": ""}],'
        '"follow_up_suggested": false}'
    )
    stubborn = _FixedAdapter([inconsistent, inconsistent])
    ev = evaluate_answer(_QUESTION, "回答", adapter=stubborn)
    assert stubborn.calls == 2  # 修复过一次
    assert ev.score == 100      # 以要点期望分为准
    assert ev.score_source == "code"
    assert "归一化" in ev.feedback


def test_evaluate_model_call_failure_raises_retryable():
    broken = _FixedAdapter(["never-used"], fail_first_with=RuntimeError("网络断了"))
    with pytest.raises(ModelCallError):
        evaluate_answer(_QUESTION, "回答", adapter=broken)
    assert broken.calls == 1  # 调用失败不进入修复循环，直接抛


def test_followup_with_mock_adapter():
    ev = Evaluation(score=70, feedback="缺量化", follow_up_suggested=True,
                    follow_up_target="性能指标")
    fq = generate_followup(_QUESTION, "回答内容", ev,
                           asked_contents=["主问题"], adapter=MockTextAdapter())
    assert isinstance(fq, FollowUpQuestion)
    assert fq.content
    assert fq.content.count("？") + fq.content.count("?") == 1


def test_followup_duplicate_rejected_then_error():
    ev = Evaluation(score=70, feedback="缺量化", follow_up_suggested=True,
                    follow_up_target="性能指标")
    # 模型连续两次都输出与已问清单重复的问题 → 修复后仍重复 → 抛错
    asked = "你提到该系统，当时是怎么测试 QPS 和 P99 延迟的？"
    dup = _FixedAdapter(
        ['{"content": "你提到该系统，当时是怎么测试 QPS 和 P99 延迟的？"}'] * 2
    )
    with pytest.raises(ModelOutputInvalidError):
        generate_followup(_QUESTION, "回答", ev,
                          asked_contents=[asked],
                          adapter=dup)
    assert dup.calls == 2
