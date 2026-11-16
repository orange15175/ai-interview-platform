"""面试复盘报告：把已落库的评分聚合成一份可读的总结（§三）。

三条边界，决定了这个模块长什么样：

1. **不调用模型**。报告只消费 answers 里已经存在的 score / evaluation，
   不因为"要写总结"再打一次模型。理由不只是省钱：报告生成如果再调模型，
   同一份记录两次打开会得到两份不一样的结论，用户没法信任。
2. **不泄露 rubric**。evaluation 里带着 rubric_results（评分要点 = 答案结构），
   原样返回等于把标准答案发给候选人。这里只取分数、维度分、反馈、缺口。
3. **不拿 0 分冒充"没数据"**。没有有效评分时 overall_score 是 None 并附说明；
   0 分是"答得很差"，两者混为一谈会让人拿着 0 分报告去复盘一场空会话。

所以这个文件里全是纯函数：输入 ORM 行，输出可序列化对象，好测也好读。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.orm import Session as OrmSession

from app.models import InterviewAnswer, InterviewQuestion, InterviewSession
from app.repositories import sessions as sessions_repo
from app.schemas.report import (
    AnswerEvidence,
    CategorySummary,
    DimensionScore,
    InterviewReport,
    ReportSummary,
    WeakPoint,
)

# 五维能力（与 Evaluation.dimension_scores 的键一致，顺序即展示顺序）
DIMENSIONS: tuple[str, ...] = ("correctness", "completeness", "logic", "depth", "clarity")

DIMENSION_LABELS = {
    "correctness": "正确性",
    "completeness": "完整性",
    "logic": "逻辑性",
    "depth": "深度",
    "clarity": "表达清晰度",
}

# 每条建议对应一个维度的短板阈值：低于它才算"需要改进"，
# 高于它硬凑建议只会让报告变成套话。
_DIMENSION_ADVICE_THRESHOLD = 70

MAX_WEAK_POINTS = 10
MAX_EVIDENCE = 6
EXCERPT_CHARS = 500
UNCATEGORIZED = "未分类"


def _valid_score(answer: InterviewAnswer) -> int | None:
    """只接受已完成回答中的合法整数分数，其余返回 None，不用 0 冒充缺失数据。

    跳过（submission_kind=skip）天然没有分，不会走到这里；
    失败的回答分数为 None，同样被排除。"""
    if answer.processing_status != "COMPLETED":
        return None
    score = answer.score
    if isinstance(score, bool) or not isinstance(score, int):
        return None
    if not 0 <= score <= 100:
        return None
    return score


def _excerpt(text: str | None) -> str:
    """折叠空白并截断回答，生成适合报告展示的短摘要。"""
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat[:EXCERPT_CHARS]


def _evaluation_of(answer: InterviewAnswer) -> dict:
    """确保评分结果是字典；缺失或异常类型时按空评分处理。"""
    return answer.evaluation if isinstance(answer.evaluation, dict) else {}


def _dimension_scores(valid: list[tuple[InterviewQuestion, InterviewAnswer]]) -> dict[str, DimensionScore]:
    """分别统计各维度平均分和样本数，缺失维度不按 0 分参与平均。

    只统计"有数值"的记录：某些题型（自我介绍、行为题）本来就不给技术维度，
    用 0 补齐会把平均分拉垮，看起来像能力问题，其实是题型不同。"""
    buckets: dict[str, list[int]] = {k: [] for k in DIMENSIONS}
    for _, answer in valid:
        dims = _evaluation_of(answer).get("dimension_scores") or {}
        if not isinstance(dims, dict):
            continue
        for key in DIMENSIONS:
            value = dims.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            buckets[key].append(value)

    out: dict[str, DimensionScore] = {}
    for key, values in buckets.items():
        if not values:
            out[key] = DimensionScore(score=None, sample_count=0)
        else:
            out[key] = DimensionScore(
                score=int(round(sum(values) / len(values))), sample_count=len(values)
            )
    return out


def _category_summary(
    ordered: list[InterviewQuestion], by_question: dict
) -> list[CategorySummary]:
    """按题目类别统计题数、已完成数和平均分，空类别统一归为未分类。"""
    buckets: dict[str, dict] = defaultdict(
        lambda: {"question_count": 0, "completed_count": 0, "scores": []}
    )
    for q in ordered:
        category = (q.category or "").strip() or UNCATEGORIZED
        bucket = buckets[category]
        bucket["question_count"] += 1
        answer = by_question.get(q.id)
        if answer is None:
            continue
        score = _valid_score(answer)
        if score is None:
            continue
        bucket["completed_count"] += 1
        bucket["scores"].append(score)

    out: list[CategorySummary] = []
    for category, bucket in buckets.items():
        scores = bucket["scores"]
        out.append(CategorySummary(
            category=category,
            question_count=bucket["question_count"],
            completed_count=bucket["completed_count"],
            average_score=int(round(sum(scores) / len(scores))) if scores else None,
        ))
    # 题多的类别在前；同数量按名称稳定排序，避免每次请求顺序都不一样
    out.sort(key=lambda c: (-c.question_count, c.category))
    return out


def _weak_points(valid: list[tuple[InterviewQuestion, InterviewAnswer]]) -> list[WeakPoint]:
    """统计评分中反复出现的缺失要点，并保留对应题目以便追溯。

    完全相同的文字合并计数：同一类缺口在不同题上重复出现，才是"薄弱"的证据；
    出现一次的更像那道题的特性，不值得写进报告。"""
    counter: dict[str, int] = defaultdict(int)
    question_ids: dict[str, list[str]] = defaultdict(list)

    for question, answer in valid:
        points = _evaluation_of(answer).get("missing_points") or []
        if not isinstance(points, list):
            continue
        seen_in_this_answer: set[str] = set()
        for raw in points:
            if not isinstance(raw, str):
                continue
            point = raw.strip()
            if not point or point in seen_in_this_answer:
                continue  # 同一条回答里重复写一次不算两次
            seen_in_this_answer.add(point)
            counter[point] += 1
            question_ids[point].append(str(question.id))

    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_WEAK_POINTS]
    return [
        WeakPoint(point=point, count=count, question_ids=question_ids[point])
        for point, count in ranked
    ]


def _evidence(valid: list[tuple[InterviewQuestion, InterviewAnswer]]) -> list[AnswerEvidence]:
    """从高分和低分回答中挑选展示证据，附题干、摘要和已有反馈。

    只给两头：最高分说明"你做对了什么"，最低分说明"差距在哪"。
    中间那些同质回答对复盘没有增量信息，给多了只是噪音。"""
    if not valid:
        return []

    scored = sorted(valid, key=lambda pair: _valid_score(pair[1]) or 0)
    picks: list[tuple[InterviewQuestion, InterviewAnswer]] = []
    half = max(MAX_EVIDENCE // 2, 1)
    picks.extend(reversed(scored[-half:]))   # 高分在前
    picks.extend(scored[:half])              # 低分在后

    out: list[AnswerEvidence] = []
    seen: set[str] = set()
    for question, answer in picks:
        key = str(answer.id)
        if key in seen:
            continue
        seen.add(key)
        out.append(AnswerEvidence(
            question_id=str(question.id),
            main_index=question.main_index,
            follow_up_index=question.follow_up_index,
            kind=question.kind,
            content=question.content,
            answer_excerpt=_excerpt(answer.answer_content),
            score=_valid_score(answer) or 0,
            feedback=str(_evaluation_of(answer).get("feedback") or "")[:500],
        ))
        if len(out) >= MAX_EVIDENCE:
            break
    return out


def _suggestions(
    dims: dict[str, DimensionScore],
    *,
    coverage: float,
    missing_count: int,
    weak_points: list[WeakPoint],
) -> list[str]:
    """根据维度短板、覆盖率和重复缺口生成规则化建议，不再调模型。

    同一条建议只出一次。宁可少给两条，也不要凑满五条套话。"""
    advice: list[str] = []

    for key in DIMENSIONS:
        dim = dims.get(key)
        if dim is None or dim.score is None or dim.score >= _DIMENSION_ADVICE_THRESHOLD:
            continue
        label = DIMENSION_LABELS[key]
        advice.append(_DIMENSION_ADVICE[key].format(score=dim.score, label=label))

    if missing_count:
        advice.append(
            f"有 {missing_count} 道题没有有效评分（失败或未完成），"
            "这部分能力目前没有数据支撑，建议重新作答后再看报告"
        )

    if weak_points:
        top = weak_points[0]
        advice.append(
            f"反复出现的缺口是「{top.point}」（{top.count} 道题），"
            "建议按这个方向准备一个能讲清背景、做法和结果的案例"
        )

    if coverage and coverage < 0.8 and not missing_count:
        advice.append("有效评分覆盖率偏低，先补齐未作答的题目，结论才站得住")

    # 去重但保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for item in advice:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


# 每个维度的建议模板：把"分数低"翻译成"下次怎么答"，而不是复述分数
_DIMENSION_ADVICE = {
    "correctness": "正确性 {score} 分：关键概念有偏差，先把原理讲准，再谈优化",
    "completeness": "完整性 {score} 分：回答覆盖了部分要点，建议按「背景—方案—结果」补齐结构",
    "logic": "逻辑性 {score} 分：结论与论据之间跳跃，建议用因果链把取舍讲清",
    "depth": "深度 {score} 分：停留在做了什么，缺少为什么这样做、还有什么替代方案",
    "clarity": "表达清晰度 {score} 分：句子偏长、重点靠后，建议先给结论再展开",
}


def build_report(
    *,
    session: InterviewSession,
    questions: list[InterviewQuestion],
    answers: list[InterviewAnswer],
    generated_at: datetime | None = None,
) -> InterviewReport:
    """把传入的题目和回答聚合成报告，不查库、不改状态，也不额外生成模型结论。"""
    ordered = sorted(questions, key=lambda q: (q.main_index, q.follow_up_index))
    by_question = {a.question_id: a for a in answers}

    completed = 0
    skipped = 0
    failed = 0
    incomplete = 0
    followups = 0
    valid: list[tuple[InterviewQuestion, InterviewAnswer]] = []

    for question in ordered:
        answer = by_question.get(question.id)
        if answer is None:
            incomplete += 1  # 已经出过题、还没提交
            continue
        if answer.processing_status == "COMPLETED":
            if answer.submission_kind == "skip":
                skipped += 1
            else:
                completed += 1
            if question.kind == "follow_up":
                followups += 1
            score = _valid_score(answer)
            if score is not None and answer.submission_kind != "skip":
                valid.append((question, answer))
            continue
        if answer.processing_status == "FAILED":
            failed += 1
        else:
            incomplete += 1

    scores = [s for _, answer in valid if (s := _valid_score(answer)) is not None]
    overall = int(round(sum(scores) / len(scores))) if scores else None
    # 分母是"非跳过题"：跳过是用户的选择，不该拉低评分覆盖率；
    # 失败的题在分母里，因为那确实是"本该有分却没有"的缺口。
    denominator = len(ordered) - skipped
    coverage = round(len(scores) / denominator, 4) if denominator > 0 else 0.0

    dims = _dimension_scores(valid)
    weak = _weak_points(valid)
    missing = len(ordered) - skipped - len(scores)

    notice = None
    if overall is None:
        notice = "没有足够有效评分数据：本次面试没有任何一道题产出有效评分，无法给出总体分数"
    elif not is_final_status(session):
        notice = "会话尚未结束，报告只覆盖已完成的题目"

    return InterviewReport(
        session_id=str(session.id),
        status=session.status,
        is_final=is_final_status(session),
        generated_at=generated_at or datetime.now(timezone.utc),
        summary=ReportSummary(
            question_count=len(ordered),
            completed_count=completed,
            skipped_count=skipped,
            failed_count=failed,
            incomplete_count=incomplete,
            overall_score=overall,
            score_coverage=max(0.0, min(1.0, coverage)),
            followup_count=followups,
        ),
        dimension_scores=dims,
        category_summary=_category_summary(ordered, by_question),
        weak_points=weak,
        evidence=_evidence(valid),
        suggestions=_suggestions(
            dims, coverage=coverage, missing_count=missing, weak_points=weak
        ),
        notice=notice,
    )


def is_final_status(session: InterviewSession) -> bool:
    """判断会话是否已结束，用来标明报告是阶段性结果还是最终结果。"""
    return session.status in sessions_repo.TERMINAL_STATUSES


# ---------------------------------------------------------------- 快照读写


def is_cacheable(session: InterviewSession, report: InterviewReport) -> bool:
    """报告是否已经"不会再变"，可以固化下来。

    两个条件缺一不可：
    - 会话已终结；
    - 没有停在 PENDING/PROCESSING 的题（incomplete=0），也没有处理失败的题
      （failed=0）——失败的题之后可能被补偿任务补完、分数会变。

    只在这时写，是为了让快照**永远不会过期**：之后的写入路径要么被
    uq_answers_session_question 挡住（每题只交一次），要么不该发生。
    剩下的边界变化（比如人工改库）走 clear_snapshot 手动清。
    """
    s = report.summary
    return (
        is_final_status(session)
        and s.incomplete_count == 0
        and s.failed_count == 0
    )


def load_snapshot(session: InterviewSession) -> InterviewReport | None:
    """读取已固化的报告；没有就返回 None，由调用方现场聚合。"""
    if not session.report_snapshot:
        return None
    return InterviewReport.model_validate(session.report_snapshot)


def save_snapshot(
    db: OrmSession, session: InterviewSession, report: InterviewReport
) -> bool:
    """把报告写进会话行。返回是否真的写了（不可固化时返回 False，不算错误）。"""
    if not is_cacheable(session, report):
        return False
    session.report_snapshot = report.model_dump(mode="json")
    db.commit()
    db.refresh(session)
    return True


def clear_snapshot(db: OrmSession, session: InterviewSession) -> None:
    """清掉快照，下次读接口重新聚合（评分被改写后用）。"""
    if session.report_snapshot is None:
        return
    session.report_snapshot = None
    db.commit()
