"""契约层校验测试：题目集、评分归一化、材料结构。"""

import pytest
from pydantic import ValidationError

from app.schemas.evaluation import DimensionScores, Evaluation, FollowUpQuestion
from app.schemas.materials import ParsedMaterials, ResumeProfile
from app.schemas.questions import QuestionSet


def _question(idx: int, content: str | None = None, weight: float = 0.5) -> dict:
    return {
        "main_index": idx,
        "category": "项目经验",
        "difficulty": "medium",
        "content": content or f"请说明第 {idx} 个技术决策的权衡。",
        "material_evidence_ids": ["ev-1"],
        "evaluation_rubric": [
            {"point": "讲清背景", "weight": weight},
            {"point": "讲清权衡", "weight": 1 - weight},
        ],
    }


def test_question_set_ok():
    qs = QuestionSet.model_validate({"questions": [_question(1), _question(2)]})
    assert [q.main_index for q in qs.sorted_questions()] == [1, 2]


def test_question_index_gap_rejected():
    with pytest.raises(ValidationError, match="编号"):
        QuestionSet.model_validate({"questions": [_question(1), _question(3)]})


def test_duplicate_content_rejected():
    with pytest.raises(ValidationError, match="重复"):
        QuestionSet.model_validate({"questions": [
            _question(1, "请说明你最有挑战的一个技术决策及其权衡过程？"),
            _question(2, "请说明你最有挑战的一个技术决策及其权衡过程!"),
        ]})


def test_rubric_weight_sum_rejected():
    bad = _question(1)
    bad["evaluation_rubric"] = [{"point": "只有一个要点", "weight": 0.3}]
    with pytest.raises(ValidationError, match="权重"):
        QuestionSet.model_validate({"questions": [bad]})


def test_evidence_cross_check():
    qs = QuestionSet.model_validate({"questions": [_question(1)]})
    with pytest.raises(ValueError, match="不存在的依据"):
        qs.assert_evidence_exists({"ev-other"})


def test_evaluation_code_recomputes_score():
    """维度分齐全时，代码按权重算分并标记来源（防止文字与分数不一致）。"""
    ev = Evaluation(
        score=95,
        feedback="回答还行",
        dimension_scores=DimensionScores(
            correctness=60, completeness=60, logic=60, depth=60, clarity=60
        ),
    )
    fixed = ev.with_code_score()
    assert fixed.score == 60
    assert fixed.score_source == "code"
    assert "归一化" in fixed.feedback


def test_evaluation_keeps_model_score_when_close():
    ev = Evaluation(
        score=80,
        feedback="不错",
        dimension_scores=DimensionScores(
            correctness=80, completeness=80, logic=80, depth=80, clarity=80
        ),
    )
    assert ev.with_code_score().score == 80
    assert ev.with_code_score().score_source == "model"


def test_evaluation_blank_feedback_rejected():
    with pytest.raises(ValidationError, match="反馈"):
        Evaluation(score=80, feedback="   ")


def test_materials_duplicate_evidence_rejected():
    payload = {
        "resume": {"skills": ["Python"]},
        "evidence": [
            {"id": "ev-1", "source": "resume", "quote": "a"},
            {"id": "ev-1", "source": "resume", "quote": "b"},
        ],
    }
    with pytest.raises(ValidationError, match="evidence"):
        ParsedMaterials.model_validate(payload)


def test_skills_dedup_and_empty_check():
    r = ResumeProfile(skills=["Python", "python", " FastAPI "])
    assert r.skills == ["Python", "FastAPI"]
    assert r.is_empty is True  # 只有技能、没有经历/教育/项目


# ---------------------------------------------------------------- 追问：单问句


def test_two_clause_followup_collapsed_into_one_question():
    """实测模型常写"确认句？真问题？"：要收束成一个问号，且不丢内容。"""
    fq = FollowUpQuestion(
        content="那在动手改之前，你有没有做过压测？QPS 从 500 到 5000 是怎么测出来的？"
    )
    assert fq.content.count("？") == 1
    assert fq.content.endswith("？")
    # 内容不能丢：两端信息都还在
    assert "压测" in fq.content and "5000" in fq.content


def test_ascii_question_mark_normalized():
    fq = FollowUpQuestion(content="你说的 P99 是怎么统计的?")
    assert fq.content.endswith("？")


def test_followup_with_question_list_rejected():
    """罗列一串问题（4 个以上问号）不是追问，宁可重试。"""
    with pytest.raises(ValidationError, match="过多问句"):
        FollowUpQuestion(content="为什么用 Redis？为什么不用本地缓存？压测怎么做的？数据可靠吗？")


def test_collapse_does_not_produce_broken_punctuation():
    fq = FollowUpQuestion(content="你提到 QPS 提到 5000？那这个数是压测还是线上真实流量？")
    assert "，？" not in fq.content and "，，" not in fq.content
    assert fq.content == "你提到 QPS 提到 5000，那这个数是压测还是线上真实流量？"
