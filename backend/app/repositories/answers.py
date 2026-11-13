"""回答仓储（开发说明 §8 interview_answers / §9.3 幂等流程）。

这张表是整个可靠性的支点，两件事必须记牢：
1. 先登记、后处理。答案行在调用模型**之前**就落库（PENDING），
   模型超时/进程被杀后仍有据可查，不出现"用户说交了、系统说没有"。
2. 幂等靠唯一约束，不靠代码里的 if：
   - (session_id, request_id)  → 同一请求重试不新增记录；
   - (session_id, question_id) → 一题只能提交一次（首期口径）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.models import InterviewAnswer
from app.models.base import utcnow


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def get_by_request(
    db: OrmSession, session_id: uuid.UUID | str, request_id: str
) -> InterviewAnswer | None:
    stmt = select(InterviewAnswer).where(
        InterviewAnswer.session_id == _as_uuid(session_id),
        InterviewAnswer.request_id == request_id,
    )
    return db.scalars(stmt).one_or_none()


def get_by_question(
    db: OrmSession, session_id: uuid.UUID | str, question_id: uuid.UUID | str
) -> InterviewAnswer | None:
    stmt = select(InterviewAnswer).where(
        InterviewAnswer.session_id == _as_uuid(session_id),
        InterviewAnswer.question_id == _as_uuid(question_id),
    )
    return db.scalars(stmt).one_or_none()


def get(db: OrmSession, answer_id: uuid.UUID | str) -> InterviewAnswer | None:
    return db.get(InterviewAnswer, _as_uuid(answer_id))


def list_for_session(db: OrmSession, session_id: uuid.UUID | str) -> list[InterviewAnswer]:
    stmt = (
        select(InterviewAnswer)
        .where(InterviewAnswer.session_id == _as_uuid(session_id))
        .order_by(InterviewAnswer.created_at)
    )
    return list(db.scalars(stmt))


def create(
    db: OrmSession,
    *,
    session_id: uuid.UUID | str,
    question_id: uuid.UUID | str,
    request_id: str,
    input_hash: str,
    submission_kind: str,
    answer_content: str | None,
    expected_state_version: int | None = None,
) -> InterviewAnswer:
    """登记一条新回答（PENDING）。

    唯一约束冲突时抛 IntegrityError，由服务层判定是"重试"还是"重复提交"——
    仓储层不猜业务语义。
    """
    row = InterviewAnswer(
        id=uuid.uuid4(),
        session_id=_as_uuid(session_id),
        question_id=_as_uuid(question_id),
        request_id=request_id,
        input_hash=input_hash,
        answer_content=answer_content,
        submission_kind=submission_kind,
        processing_status="PENDING",
        processing_stage=None,
        processing_attempt=0,
        expected_state_version=expected_state_version,
        created_at=utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        raise
    return row


def mark_stage(
    db: OrmSession, answer: InterviewAnswer, stage: str, *, bump_attempt: bool = True
) -> InterviewAnswer:
    """记录"处理到哪一步"。

    恢复时按它判断哪些阶段已经成功、不必重调模型（§9.3 第 6 条）。
    """
    answer.processing_status = "PROCESSING" if stage not in ("COMPLETED", "FAILED") else stage
    answer.processing_stage = stage
    if bump_attempt:
        answer.processing_attempt = (answer.processing_attempt or 0) + 1
    db.flush()
    return answer


def save_evaluation(
    db: OrmSession, answer: InterviewAnswer, *, evaluation: dict | None, score: int | None
) -> InterviewAnswer:
    """评分结果单独落库：追问生成失败时这份评分必须能复用，不能重打一遍模型。"""
    answer.evaluation = evaluation
    answer.score = score
    answer.processing_stage = "EVALUATED"
    db.flush()
    return answer


def save_decision(db: OrmSession, answer: InterviewAnswer, *, decision: dict) -> InterviewAnswer:
    """规则链裁决（含 reason_code / rule_version），复盘依据。"""
    answer.rule_decision = decision
    answer.processing_stage = "DECIDED"
    db.flush()
    return answer


def complete(
    db: OrmSession,
    answer: InterviewAnswer,
    *,
    response_snapshot: dict,
    result_state_version: int | None = None,
) -> InterviewAnswer:
    """标记完成并保存可回放响应。

    幂等重试用它直接返回上次的结果（§9.3 第 2 条），不再跑模型。
    """
    answer.processing_status = "COMPLETED"
    answer.processing_stage = "COMPLETED"
    answer.response_snapshot = response_snapshot
    answer.result_state_version = result_state_version
    answer.completed_at = utcnow()
    db.flush()
    return answer


def fail(db: OrmSession, answer: InterviewAnswer, *, code: str, message: str) -> InterviewAnswer:
    """处理失败：保留记录与阶段，供恢复入口对账（不删行）。"""
    answer.processing_status = "FAILED"
    answer.processing_stage = answer.processing_stage or "FAILED"
    answer.response_snapshot = {
        "error": {"code": code, "message": message[:300]},
    }
    answer.completed_at = None
    db.flush()
    return answer


def delete(db: OrmSession, answer: InterviewAnswer) -> None:
    """物理删除一条**从未产生任何结果**的提交。

    只允许恢复对账时对"空壳"记录使用（没有评分、没有裁决、没有快照）：
    这种行只是用户输入的残影，留着会让 (session_id, question_id) 唯一约束
    把这道题永久锁死。任何带结果的记录都不许删（那是正式记录）。
    """
    if answer.evaluation is not None or answer.rule_decision is not None:
        raise ValueError("拒绝删除带评分或裁决的回答记录")
    db.delete(answer)
    db.flush()
