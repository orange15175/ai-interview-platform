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
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.core.logging import get_logger
from app.models import InterviewAnswer
from app.models.base import utcnow
from app.services import compensation

log = get_logger(__name__)

# 抢占超过这个时长仍未结束，视为 worker 已崩溃，任务可以被人重新接管
CLAIM_STALE_SECONDS = 300


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """把字符串 ID 转成 UUID，供回答查询条件使用。"""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def get_by_request(
    db: OrmSession, session_id: uuid.UUID | str, request_id: str
) -> InterviewAnswer | None:
    """按会话和请求 ID 找旧提交，这是判断结果回放或续跑的依据。"""
    stmt = select(InterviewAnswer).where(
        InterviewAnswer.session_id == _as_uuid(session_id),
        InterviewAnswer.request_id == request_id,
    )
    return db.scalars(stmt).one_or_none()


def get_by_question(
    db: OrmSession, session_id: uuid.UUID | str, question_id: uuid.UUID | str
) -> InterviewAnswer | None:
    """查这道题是否已经登记回答，防止换请求 ID 再答同一题。"""
    stmt = select(InterviewAnswer).where(
        InterviewAnswer.session_id == _as_uuid(session_id),
        InterviewAnswer.question_id == _as_uuid(question_id),
    )
    return db.scalars(stmt).one_or_none()


def get(db: OrmSession, answer_id: uuid.UUID | str) -> InterviewAnswer | None:
    """按回答 ID 读取单条记录，不负责会话归属检查。"""
    return db.get(InterviewAnswer, _as_uuid(answer_id))


def list_for_session(db: OrmSession, session_id: uuid.UUID | str) -> list[InterviewAnswer]:
    """读取一场面试的全部回答，供正式记录、报告和恢复使用。"""
    stmt = (
        select(InterviewAnswer)
        .where(InterviewAnswer.session_id == _as_uuid(session_id))
        .order_by(InterviewAnswer.created_at)
    )
    return list(db.scalars(stmt))


def average_scores(
    db: OrmSession, session_ids: list[uuid.UUID]
) -> dict[uuid.UUID, float]:
    """按会话批量算平均分（列表页用），一次查询解决，避免每行单独聚合。

    只统计有分的回答：跳过题和评分失败的题 score 为 NULL，把它们算进分母
    等于把"没答"当成"答了 0 分"。会话没有有效分数时不出现在返回的映射里。
    """
    if not session_ids:
        return {}
    stmt = (
        select(InterviewAnswer.session_id, func.avg(InterviewAnswer.score))
        .where(
            InterviewAnswer.session_id.in_(session_ids),
            InterviewAnswer.score.is_not(None),
        )
        .group_by(InterviewAnswer.session_id)
    )
    return {sid: float(avg) for sid, avg in db.execute(stmt).all()}


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
    """登记带输入指纹的 PENDING 回答，先留下原始提交再执行模型。

    唯一约束冲突时抛 IntegrityError，由服务层判定是"重试"还是"重复提交"——
    仓储层不猜业务语义。"""
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
    """更新回答处理到了哪一步，便于失败后判断还能复用什么结果。

    恢复时按它判断哪些阶段已经成功、不必重调模型。"""
    answer.processing_status = "PROCESSING" if stage not in ("COMPLETED", "FAILED") else stage
    answer.processing_stage = stage
    if bump_attempt:
        answer.processing_attempt = (answer.processing_attempt or 0) + 1
    db.flush()
    return answer


def save_evaluation(
    db: OrmSession, answer: InterviewAnswer, *, evaluation: dict | None, score: int | None
) -> InterviewAnswer:
    """保存评分和总分，后续追问失败时仍可保留这份结果。"""
    answer.evaluation = evaluation
    answer.score = score
    answer.processing_stage = "EVALUATED"
    db.flush()
    return answer


def save_decision(db: OrmSession, answer: InterviewAnswer, *, decision: dict) -> InterviewAnswer:
    """保存是否追问、原因码和规则版本，便于复盘与恢复。"""
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
    epoch: int | None = None,
) -> InterviewAnswer:
    """把回答标成完成，并保存响应快照、结果版本和完成时的写入代次。

    幂等重试用它直接返回上次的结果，不再跑模型。
    完成即退出补偿队列：保持 retryable=True 会让后台 worker 反复扫到一条
    已经成功的行（条件里虽有 processing_status='FAILED' 兜底，但留着脏标记
    迟早会在别的查询里咬人）。"""
    answer.processing_status = "COMPLETED"
    answer.processing_stage = "COMPLETED"
    answer.response_snapshot = response_snapshot
    answer.result_state_version = result_state_version
    answer.last_error_code = None
    answer.retryable = False
    answer.next_retry_at = None
    answer.claimed_at = None
    answer.claim_token = None
    if epoch is not None:
        answer.write_epoch = epoch
    answer.completed_at = utcnow()
    db.flush()
    return answer


def fail(
    db: OrmSession,
    answer: InterviewAnswer,
    *,
    code: str,
    message: str,
    epoch: int | None = None,
) -> InterviewAnswer:
    """记录提交失败原因，再按错误类别安排自动重试或停止重试。

    同时决定"这条失败要不要进后台补偿队列"：
    - 模型瞬时失败 → retryable=True + next_retry_at（30 秒后第一次接手）；
    - 需恢复/业务冲突/不可重试 → retryable=False，永不自动重试。
    分类逻辑在 services/compensation.py，这里只负责把结论写到行上。"""
    answer.processing_status = "FAILED"
    answer.processing_stage = answer.processing_stage or "FAILED"
    answer.last_error_code = code
    answer.response_snapshot = {
        "error": {"code": code, "message": message[:300]},
    }
    answer.completed_at = None
    if epoch is not None:
        answer.write_epoch = epoch

    now = utcnow()
    attempt = int(answer.attempt_count or 0)
    delay = compensation.backoff_seconds(attempt)
    can_retry = compensation.is_retryable(code) and delay is not None

    answer.retryable = can_retry
    answer.next_retry_at = (
        now + timedelta(seconds=delay) if can_retry and delay is not None else None
    )
    # 失败即放弃本次抢占：worker 崩溃后靠 claimed_at 超时重新入队，
    # 正常失败则立刻把 claim_token 清空，让下一轮扫描马上能接手。
    answer.claimed_at = None
    answer.claim_token = None

    level = compensation.degradation_level_for_failure(
        code, has_evaluation=answer.evaluation is not None
    )
    if level:
        answer.degradation_level = level
        answer.degradation_reason = f"{code}: {message}"[:200]
    db.flush()
    return answer


def record_evaluation_meta(
    db: OrmSession,
    answer: InterviewAnswer,
    *,
    model_name: str | None,
    prompt_version: str | None = "v1",
) -> InterviewAnswer:
    """记录评分模型和提示词版本，便于定位不同版本的评分差异。"""
    if model_name:
        answer.model_name = model_name[:64]
    if prompt_version:
        answer.prompt_version = prompt_version[:16]
    db.flush()
    return answer


def record_degradation(
    db: OrmSession, answer: InterviewAnswer, *, level: str, reason: str
) -> InterviewAnswer:
    """记录评分或追问降级信息，不直接改变回答完成状态。

    追问失败就是走这里：分已经算出来了，只是少问了一句——这不是失败，
    不该把整条回答标成 FAILED，但必须留痕，否则"追问率低"永远查不出原因。"""
    answer.degradation_level = level[:32]
    answer.degradation_reason = reason[:200]
    db.flush()
    return answer


def record_graph_position(
    db: OrmSession,
    answer: InterviewAnswer,
    *,
    stage: str,
    wait_question_id: uuid.UUID | None,
) -> InterviewAnswer:
    """保存执行前图的等待位置，重试时据此判断是否已经消费过回答。

    重试时它是唯一能把"图已推进"和"图还在等这一题"分开的证据：
    两次记录的等待题不同 → 图消费过这次回答，只能补写数据库，不能重跑。"""
    answer.graph_stage = stage
    answer.graph_wait_question_id = (
        _as_uuid(wait_question_id) if wait_question_id is not None else None
    )
    db.flush()
    return answer


def delete(db: OrmSession, answer: InterviewAnswer) -> None:
    """删除恢复流程确认无结果的提交记录；调用方负责决定它是否可删。

    只允许恢复对账时对"空壳"记录使用（没有评分、没有裁决、没有快照）：
    这种行只是用户输入的残影，留着会让 (session_id, question_id) 唯一约束
    把这道题永久锁死。任何带结果的记录都不许删（那是正式记录）。"""
    if answer.evaluation is not None or answer.rule_decision is not None:
        raise ValueError("拒绝删除带评分或裁决的回答记录")
    db.delete(answer)
    db.flush()


# ---------------------------------------------------------------- 后台补偿


def _claim_deadline(now: datetime, stale_seconds: int) -> datetime:
    """计算抢占已超时的时间分界：早于这个时间的抢占可以重新领取。"""
    return now - timedelta(seconds=stale_seconds)


def due_for_retry(
    db: OrmSession,
    *,
    limit: int = 10,
    now: datetime | None = None,
    stale_seconds: int = CLAIM_STALE_SECONDS,
) -> list[InterviewAnswer]:
    """只读扫描到期失败记录，扫描到不代表本 worker 已经取得处理权。

    条件全部写在这里，扫描与抢占必须用同一套口径，否则会出现
    "扫出来却抢不到"的假任务，把 worker 的空转伪装成有活干。"""
    now = now or datetime.now(timezone.utc)
    stmt = (
        select(InterviewAnswer)
        .where(
            InterviewAnswer.processing_status == "FAILED",
            InterviewAnswer.retryable.is_(True),
            InterviewAnswer.next_retry_at.is_not(None),
            InterviewAnswer.next_retry_at <= now,
            # 已经试满的行不再交给 worker：即便某条路径漏了清 retryable，
            # 也不能让它在队列里无限转（扫描与抢占的口径必须一致）
            InterviewAnswer.attempt_count < compensation.MAX_ATTEMPTS,
            or_(
                InterviewAnswer.claimed_at.is_(None),
                InterviewAnswer.claimed_at < _claim_deadline(now, stale_seconds),
            ),
        )
        .order_by(InterviewAnswer.next_retry_at, InterviewAnswer.attempt_count)
        .limit(limit)
    )
    return list(db.scalars(stmt))


def claim_for_retry(
    db: OrmSession,
    answer_id: uuid.UUID | str,
    *,
    claim_token: str,
    now: datetime | None = None,
    stale_seconds: int = CLAIM_STALE_SECONDS,
) -> bool:
    """通过数据库条件更新抢占任务，只有影响一行的 worker 才能继续处理。

    为什么不能"先查出来再改状态"：两个 worker 同时扫到同一行时，两边都会
    查到、都会去跑模型，结果是重复计费 + 两份结果抢着落库。条件更新把
    "读—判断—改"压成一条 SQL，数据库替我们做互斥。"""
    now = now or datetime.now(timezone.utc)
    result = db.execute(
        update(InterviewAnswer)
        .where(
            InterviewAnswer.id == _as_uuid(answer_id),
            InterviewAnswer.processing_status == "FAILED",
            InterviewAnswer.retryable.is_(True),
            InterviewAnswer.next_retry_at.is_not(None),
            InterviewAnswer.next_retry_at <= now,
            InterviewAnswer.attempt_count < compensation.MAX_ATTEMPTS,
            or_(
                InterviewAnswer.claimed_at.is_(None),
                InterviewAnswer.claimed_at < _claim_deadline(now, stale_seconds),
            ),
        )
        .values(
            processing_status="PROCESSING",
            claimed_at=now,
            claim_token=claim_token,
            attempt_count=InterviewAnswer.attempt_count + 1,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def release_claim(db: OrmSession, answer: InterviewAnswer, *, reason: str) -> None:
    """清除本次任务抢占，让后续扫描可以再次领取。

    刻意不删记录：这类"不该由后台处理"的情况要留下失败事实，
    只是把抢占放掉。processing_status 显式写回 FAILED——抢占时它是 PROCESSING，
    不写回来这条记录就永远卡在"处理中"，恢复对账时会被当成半途任务。"""
    log.info("释放补偿抢占 answer=%s: %s", answer.id, reason)
    answer.processing_status = "FAILED"
    answer.claimed_at = None
    answer.claim_token = None
    if not compensation.is_retryable(answer.last_error_code):
        answer.retryable = False
        answer.next_retry_at = None
    db.flush()


def mark_retry_exhausted(db: OrmSession, answer: InterviewAnswer, *, reason: str) -> None:
    """停止该回答的自动重试，保留失败记录等待人工或用户处理。"""
    answer.processing_status = "FAILED"
    answer.retryable = False
    answer.next_retry_at = None
    answer.claimed_at = None
    answer.claim_token = None
    answer.degradation_level = answer.degradation_level or "COMPENSATION_GAVE_UP"
    answer.degradation_reason = reason[:200]
    db.flush()
