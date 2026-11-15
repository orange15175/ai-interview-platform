"""复盘报告的统计口径（§3.7）。全部是纯函数测试：不碰数据库、不碰模型。

要守住的口径：
- 跳过不计入平均分，但要计入题数与 skipped_count；
- 失败/未完成不计入评分，分别计数；
- 没有有效评分时 overall_score 是 None（绝不用 0 代替），并给说明；
- 五维只统计有数值的记录（某些题型本来不给技术维度）；
- 类别为空归到"未分类"；重复的 missing_points 合并计数；
- 报告里绝不出现 rubric_results。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import reporting


def _question(main: int, follow: int = 0, *, category: str = "项目经验",
              kind: str | None = None, content: str | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        main_index=main,
        follow_up_index=follow,
        kind=kind or ("main" if follow == 0 else "follow_up"),
        category=category,
        content=content or f"第 {main}-{follow} 题：请说明你的技术取舍。",
        difficulty="medium",
    )


def _answer(question, *, status="COMPLETED", score=80, skip=False,
            dims=None, missing=None, feedback="回答结构清晰，结论明确。"):
    evaluation = None
    if status == "COMPLETED" and not skip:
        evaluation = {
            "score": score,
            "feedback": feedback,
            "dimension_scores": dims,
            "missing_points": missing or [],
            # 内部评分要点：绝不能出现在报告里
            "rubric_results": [{"point": "内部要点", "met": "yes"}],
        }
    return SimpleNamespace(
        id=uuid.uuid4(),
        question_id=question.id,
        processing_status=status,
        submission_kind="skip" if skip else "answer",
        answer_content=None if skip else "我做了一个取舍，QPS 从 500 提升到 5000。",
        score=None if skip or status != "COMPLETED" else score,
        evaluation=evaluation,
    )


def _session(status="FINISHED"):
    return SimpleNamespace(id=uuid.uuid4(), status=status)


DIMS = {"correctness": 80, "completeness": 70, "logic": 90, "depth": 60, "clarity": 85}


# ---------------------------------------------------------------- 全量完成


def test_all_completed_produces_full_report():
    session = _session()
    questions = [_question(1), _question(2)]
    answers = [_answer(q, score=80 if q.main_index == 1 else 90, dims=DIMS) for q in questions]

    report = reporting.build_report(session=session, questions=questions, answers=answers)

    assert report.summary.question_count == 2
    assert report.summary.completed_count == 2
    assert report.summary.overall_score == 85
    assert report.summary.score_coverage == 1.0
    assert report.is_final is True
    assert report.notice is None
    # 五维聚合
    assert report.dimension_scores["correctness"].score == 80
    assert report.dimension_scores["depth"].sample_count == 2
    # 证据里最高分在前、最低分在后
    assert report.evidence[0].score == 90
    assert report.evidence[-1].score == 80
    # 不泄题：rubric 绝不能出现在任何地方
    assert "rubric" not in report.model_dump_json()


# ---------------------------------------------------------------- 跳过与部分完成


def test_skipped_is_counted_but_not_scored():
    session = _session()
    q1, q2 = _question(1), _question(2)
    answers = [_answer(q1, score=80, dims=DIMS), _answer(q2, skip=True)]

    report = reporting.build_report(session=session, questions=[q1, q2], answers=answers)

    assert report.summary.skipped_count == 1
    assert report.summary.completed_count == 1
    # 平均分只由真正作答的那道题决定，跳过的题不拉低分数
    assert report.summary.overall_score == 80
    # 分母是"非跳过题"：跳过是用户的选择
    assert report.summary.score_coverage == 1.0


def test_partial_session_counts_unanswered_and_marks_not_final():
    session = _session(status="IN_PROGRESS")
    q1, q2, q3 = _question(1), _question(2), _question(3)
    answers = [_answer(q1, score=70, dims=DIMS), _answer(q2, status="FAILED", score=None)]

    report = reporting.build_report(
        session=session, questions=[q1, q2, q3], answers=answers
    )

    assert report.summary.failed_count == 1
    assert report.summary.incomplete_count == 1  # q3 还没提交
    assert report.summary.question_count == 3
    assert report.is_final is False
    assert report.notice and "尚未结束" in report.notice
    # 覆盖率：1 个有效分 / (3 题 - 0 跳过)
    assert report.summary.score_coverage == pytest.approx(0.3333, abs=1e-4)
    # 有缺口时必须给出一条说明，而不是假装报告完整
    assert any("没有有效评分" in s for s in report.suggestions)


# ---------------------------------------------------------------- 没有数据


def test_no_scores_returns_null_overall_not_zero():
    session = _session(status="CANCELLED")
    q1 = _question(1)
    answers = [_answer(q1, status="FAILED", score=None)]

    report = reporting.build_report(session=session, questions=[q1], answers=answers)

    assert report.summary.overall_score is None  # 关键：不是 0
    assert report.summary.score_coverage == 0.0
    assert report.evidence == []
    assert report.weak_points == []
    assert report.notice and "没有足够有效评分数据" in report.notice


def test_empty_session_reports_zero_denominator_safely():
    session = _session(status="PREPARATION_FAILED")
    report = reporting.build_report(session=session, questions=[], answers=[])

    assert report.summary.question_count == 0
    assert report.summary.overall_score is None
    assert report.summary.score_coverage == 0.0  # 分母为 0 时返回 0，不是崩溃


# ---------------------------------------------------------------- 维度与类别


def test_dimension_partial_null_is_ignored_not_zeroed():
    session = _session()
    q1, q2 = _question(1), _question(2)
    answers = [
        _answer(q1, score=80, dims={"correctness": 90, "logic": 70}),
        _answer(q2, score=60, dims=None),  # 行为题不给技术维度
    ]

    report = reporting.build_report(session=session, questions=[q1, q2], answers=answers)

    assert report.dimension_scores["correctness"].score == 90
    assert report.dimension_scores["correctness"].sample_count == 1
    # 没有数值的维度不能变成 0 分（那看起来像"能力为零"）
    assert report.dimension_scores["depth"].score is None
    assert report.dimension_scores["depth"].sample_count == 0


def test_blank_category_falls_into_uncategorized():
    session = _session()
    q1 = _question(1, category="")
    q2 = _question(2, category="专业能力")
    answers = [_answer(q1, score=80, dims=DIMS), _answer(q2, score=60, dims=DIMS)]

    report = reporting.build_report(session=session, questions=[q1, q2], answers=answers)
    cats = {c.category: c for c in report.category_summary}

    assert reporting.UNCATEGORIZED in cats
    assert cats[reporting.UNCATEGORIZED].average_score == 80
    assert cats["专业能力"].average_score == 60


# ---------------------------------------------------------------- 薄弱点与建议


def test_repeated_missing_points_are_merged_and_ranked():
    session = _session()
    q1, q2, q3 = _question(1), _question(2), _question(3)
    answers = [
        _answer(q1, score=60, dims=DIMS, missing=["缺少量化结果", "没讲清权衡"]),
        _answer(q2, score=65, dims=DIMS, missing=["缺少量化结果", "  "]),  # 空串要被丢掉
        _answer(q3, score=70, dims=DIMS, missing=["缺少量化结果"]),
    ]

    report = reporting.build_report(
        session=session, questions=[q1, q2, q3], answers=answers
    )

    top = report.weak_points[0]
    assert top.point == "缺少量化结果"
    assert top.count == 3
    assert len(top.question_ids) == 3
    # 只出现一次的缺口排在后面
    assert report.weak_points[-1].point == "没讲清权衡"
    assert all(w.point.strip() for w in report.weak_points)


def test_low_dimension_generates_actionable_suggestion():
    session = _session()
    q1 = _question(1)
    answers = [_answer(q1, score=55, dims={"correctness": 50, "depth": 40})]

    report = reporting.build_report(session=session, questions=[q1], answers=answers)

    joined = " ".join(report.suggestions)
    assert "正确性" in joined
    assert "深度" in joined
    # 建议条数不堆砌：每个短板一条，重复去重
    assert len(report.suggestions) == len(set(report.suggestions))


def test_high_dimensions_do_not_fabricate_advice():
    session = _session()
    q1 = _question(1)
    answers = [_answer(q1, score=95, dims={
        "correctness": 95, "completeness": 92, "logic": 90, "depth": 88, "clarity": 90,
    })]

    report = reporting.build_report(session=session, questions=[q1], answers=answers)
    assert report.suggestions == []
