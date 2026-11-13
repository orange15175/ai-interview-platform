"""契约层校验测试：题目集、评分归一化、材料结构。"""

import pytest
from pydantic import ValidationError

from app.schemas.evaluation import DimensionScores, Evaluation
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
