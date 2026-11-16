"""评分 / 追问 Agent 单元测试（mock 驱动，不联网）。

重点验证行为约束，而不是 mock 数据本身：
- 评分：维度分齐全时总分由代码重算；不合法输出 → 修复一次 → 仍失败抛可重试错误；
- 追问：单问题；与已问清单重复 → 修复 → 仍重复抛错。
"""

from __future__ import annotations

import json

import pytest

from app.agents.adapters import MockTextAdapter
from app.agents.evaluate_agent import evaluate_answer
from app.agents.followup_agent import generate_followup
from app.core.errors import ModelCallError, ModelOutputInvalidError
from app.schemas.evaluation import Evaluation, FollowUpQuestion, GeneratedFollowUpQuestion

# 当前题的两条评分依据，模型必须按顺序逐条评价。
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


def _valid_evaluation():
    return {
        "score": 60, "feedback": "核心正确，需补充结果的测量方法。",
        "dimension_scores": dict.fromkeys(["correctness", "completeness", "logic", "depth", "clarity"], 60),
        "rubric_results": [{"point": p["point"], "met": "yes", "note": "回答覆盖该点"}
                           for p in _QUESTION["evaluation_rubric"]],
        "missing_points": [], "follow_up_suggested": False,
        "follow_up_reason": "", "follow_up_target": "",
    }


def test_evaluate_invalid_then_valid_repair_once():
    bad = _FixedAdapter(["这不是 JSON", json.dumps(_valid_evaluation())])
    ev = evaluate_answer(_QUESTION, "回答", adapter=bad)
    assert ev.score == 60
    assert bad.calls == 2  # 第一次失败，修复一次成功


def test_evaluate_all_invalid_raises_retryable_error():
    bad = _FixedAdapter(["垃圾", "还是垃圾"])
    with pytest.raises(ModelOutputInvalidError):
        evaluate_answer(_QUESTION, "回答", adapter=bad)


def test_rubric_coverage_never_overrides_dimension_score():
    # 全部要点 yes 不代表五维一定100分，唯一总分依据仍是五维。
    adapter = _FixedAdapter([json.dumps(_valid_evaluation())])
    ev = evaluate_answer(_QUESTION, "回答", adapter=adapter)
    assert ev.score == 60
    assert ev.score_source == "code"
    assert ev.prompt_version == "v2"
    assert adapter.calls == 1


@pytest.mark.parametrize("change", [
    {"dimension_scores": None},
    {"dimension_scores": {"correctness": 60}},
    {"rubric_results": []},
    {"score": "60"},
    {"score": True},
    {"follow_up_suggested": "false"},
    {"missing_points": ["  "]},
    {"follow_up_suggested": True, "follow_up_target": ""},
])
def test_invalid_scoring_contract_is_rejected(change):
    payload = {**_valid_evaluation(), **change}
    adapter = _FixedAdapter([json.dumps(payload)] * 2)
    with pytest.raises(ModelOutputInvalidError):
        evaluate_answer(_QUESTION, "回答", adapter=adapter)



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
        [json.dumps({"content": asked, "evaluation_rubric": [{"point": "指标测量方法", "weight": 1.0}]})] * 2
    )
    with pytest.raises(ModelOutputInvalidError):
        generate_followup(_QUESTION, "回答", ev,
                          asked_contents=[asked],
                          adapter=dup)
    assert dup.calls == 2


def test_followup_missing_independent_rubric_is_rejected():
    ev = Evaluation(score=70, feedback="需说明测量", follow_up_target="性能指标")
    adapter = _FixedAdapter(['{"content":"当时是怎么测量P99延迟的？"}'] * 2)
    with pytest.raises(ModelOutputInvalidError):
        generate_followup(_QUESTION, "回答", ev, [], adapter=adapter)


def test_context_and_answer_tail_reach_followup_model():
    from app.agents.followup_agent import _build_user_prompt
    ev = Evaluation(score=70, feedback="需说明测量", follow_up_target="性能指标")
    question = {**_QUESTION, "main_question": "主问题原文", "history": [
        {"question": "之前问句", "answer": "之前的有效回答"}]}
    prompt = _build_user_prompt(question, "字" * 2100 + "关键尾部", ev, [])
    assert "关键尾部" in prompt and "之前的有效回答" in prompt and "主问题原文" in prompt


def test_new_followup_cannot_hide_multiple_questions_by_normalizing_punctuation():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="一个问号"):
        GeneratedFollowUpQuestion.model_validate({
            "content": "你有没有做压测？当时如何统计延迟？",
            "evaluation_rubric": [{"point": "延迟统计方法", "weight": 1.0}],
        })
