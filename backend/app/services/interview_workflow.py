"""面试流程编排：启动、提交回答（开发说明 §9.3 逐条落地）。

这一层不实现 HTTP，只实现"一次业务动作要做完哪些事、按什么顺序、失败怎么收场"。
API 层（阶段 5）拿到 session_id/visitor_id 后调用这里；CLI 与测试也调这里。

提交回答的 8 步（对应 §9.3）：
1. 校验归属与终态；2. 幂等判定（replay/conflict/resume/proceed）；
3. 拿会话锁，锁内**重新读取**最新状态并再次幂等检查；
4. 短事务登记回答（PENDING），唯一约束防重复；
5. 恢复图执行评分/规则/追问（模型调用不在事务里）；
6. 阶段结果幂等写库（评分→裁决→响应快照）；
7. CAS 递增 state_version，更新当前题与进度；
8. 写 Redis 幂等缓存，释放锁，返回结果。

跨存储不一致的处理口径（§9.3 末段，必须诚实）：
- 图已推进、DB 未写 → 同一 request_id 重试走 resume 分支，从图状态里
  把评分/裁决**读出来补写**，不重复调模型；
- DB 已写、checkpoint 丢 → 走恢复入口从 PG 重建（recovery.py）；
- 锁在写库前丢失 → 抛 VERSION_CONFLICT，绝不带病写入，交给恢复对账。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.core.errors import (
    AppError,
    RequestConflictError,
    StaleQuestionError,
    StateRecoveryRequiredError,
    VersionConflictError,
)
from app.core.logging import get_logger
from app.models import InterviewAnswer, InterviewSession
from app.repositories import answers as answers_repo
from app.repositories import questions as questions_repo
from app.repositories import sessions as sessions_repo
from app.schemas.questions import GeneratedQuestion, QuestionSet
from app.services import graph_runtime, idempotency, locks

log = get_logger(__name__)


# ---------------------------------------------------------------- 对外投影


def public_evaluation(evaluation: dict | None) -> dict | None:
    """评分对外投影：**剔除 rubric_results**。

    rubric_results 是"评分要点 + 是否满足"，等于把答案结构告诉了候选人
    （§10：不得向前端输出私有评分标准）。分数、反馈、维度分可以给。
    """
    if not evaluation:
        return None
    return {
        "score": evaluation.get("score"),
        "feedback": evaluation.get("feedback", ""),
        "dimension_scores": evaluation.get("dimension_scores"),
        "missing_points": evaluation.get("missing_points", []),
        "follow_up_suggested": evaluation.get("follow_up_suggested", False),
    }


def public_decision(decision: dict | None) -> dict | None:
    """裁决对外投影：只给结论与稳定原因码，不给内部 detail 文案。"""
    if not decision:
        return None
    return {
        "decision": decision.get("decision"),
        "reason_code": decision.get("reason_code"),
    }


def _next_question_payload(
    db: OrmSession, session_id: uuid.UUID, payload: dict | None, state_values: dict
) -> dict | None:
    """把中断 payload 落成"带 question_id 的公开题目"。

    主问题的行在准备阶段已入库；追问是图里现生成的，必须在这里补一行，
    否则恢复时无题可重建（§8 一题一行）。
    """
    if not payload:
        return None
    main_index = payload["main_index"]
    follow_index = payload.get("follow_up_index", 0)
    row = questions_repo.get_by_index(db, session_id, main_index, follow_index)

    if row is None and payload.get("kind") == "follow_up":
        parent = questions_repo.get_by_index(db, session_id, main_index, 0)
        if parent is None:
            raise StateRecoveryRequiredError("追问缺少所属主问题，无法落库")
        pending = state_values.get("pending_question", {})
        row = questions_repo.add_followup(
            db,
            session_id=session_id,
            parent=parent,
            follow_up_index=follow_index,
            content=payload["content"],
            rubric=pending.get("evaluation_rubric"),
            evidence_ids=pending.get("material_evidence_ids"),
        )
    if row is None:
        raise StateRecoveryRequiredError(f"第 {main_index} 题缺少数据库记录")

    out = questions_repo.public_dict(row)
    out["total_questions"] = payload.get("total_questions")
    return out


def _progress(state_values: dict, payload: dict | None, *, finished: bool) -> dict:
    """受控的恢复进度（§8 末段）：重建时要靠它，不是只给页面看的字段。"""
    answered = sum(1 for e in state_values.get("evaluations", []) if e is not None)
    skipped = sum(1 for e in state_values.get("evaluations", []) if e is None)
    base = {
        "stage": "FINISHED" if finished else "WAITING_ANSWER",
        "current_main_index": state_values.get("current_main_index", 1),
        "current_followup_index": state_values.get("current_followup_index", 0),
        "answered_count": answered,
        "skipped_count": skipped,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload:
        base["question_kind"] = payload.get("kind")
        base["question_content"] = payload.get("content", "")[:200]
    return base


# ---------------------------------------------------------------- 启动面试


def start_interview(
    *,
    db: OrmSession,
    graph: CompiledStateGraph,
    session: InterviewSession,
    questions: QuestionSet | list[GeneratedQuestion],
) -> dict:
    """题目入库 → 启动图 → 停在第一题的等待点。

    图的初始 state 只放 session_id 与题目（模型已经在准备阶段调用过，
    图里不再重复解析/出题）。返回的 payload 已带 question_id。
    """
    qs = (
        questions.sorted_questions()
        if isinstance(questions, QuestionSet)
        else sorted(questions, key=lambda q: q.main_index)
    )
    rows = questions_repo.insert_main_questions(db, session.id, qs)

    seed = {
        "session_id": str(session.id),
        "questions": [q.model_dump() for q in qs],
    }
    result = graph.invoke(seed, graph_runtime.thread_config(session.id))
    payload = graph_runtime.interrupt_payload(result)
    if payload is None:
        raise StateRecoveryRequiredError("图未停在等待点，无法确定当前题")

    state_values, _, _ = graph_runtime.snapshot_state(graph, session.id)
    view = _next_question_payload(db, session.id, payload, state_values)

    sessions_repo.touch(
        db,
        session,
        status="IN_PROGRESS",
        current_question_id=uuid.UUID(view["question_id"]) if view else None,
        progress=_progress(state_values, payload, finished=False),
        started=True,
    )
    sessions_repo.bump_state_version(db, session)
    db.commit()

    log.info(
        "会话 %s 启动完成：%d 道题，当前第 %s 题", session.id, len(rows),
        payload.get("main_index"),
    )
    return {
        "session_id": str(session.id),
        "status": session.status,
        "state_version": session.state_version,
        "question": view,
        "total_questions": len(rows),
    }


# ---------------------------------------------------------------- 提交回答


def submit_answer(
    *,
    db: OrmSession,
    graph: CompiledStateGraph,
    session_id: uuid.UUID | str,
    owner_id: uuid.UUID | str,
    question_id: uuid.UUID | str,
    request_id: str,
    answer: str | None = None,
    skipped: bool = False,
    expected_state_version: int | None = None,
    redis_client=None,
) -> dict:
    """提交一次回答（或跳过），推进到下一题或结束。"""
    if not request_id or len(request_id) > 64:
        raise RequestConflictError("request_id 缺失或过长（<=64 字符）")

    session = sessions_repo.get_owned(db, session_id, owner_id)
    sessions_repo.assert_can_answer(session)

    question = questions_repo.get(db, question_id)
    if question is None or question.session_id != session.id:
        raise StaleQuestionError("题目不属于该会话")

    input_hash = idempotency.input_fingerprint(answer=answer, skipped=skipped)
    check = idempotency.classify_submission(
        answers_repo.get_by_request(db, session.id, request_id),
        input_hash=input_hash,
        question_id=question.id,
    )
    if check.action == "conflict":
        raise RequestConflictError(check.reason)
    if check.action == "replay":
        return _replay_response(db, session.id, request_id, redis_client)

    lock = locks.acquire(str(session.id))
    lock.start_auto_renew()
    try:
        # 锁内重读：等锁期间别人可能已经推进过（§9.2 第 3 条）
        db.refresh(session)
        sessions_repo.assert_can_answer(session)
        if expected_state_version is not None and expected_state_version != session.state_version:
            raise VersionConflictError()

        existing = answers_repo.get_by_request(db, session.id, request_id)
        check = idempotency.classify_submission(
            existing, input_hash=input_hash, question_id=question.id
        )
        if check.action == "conflict":
            raise RequestConflictError(check.reason)
        if check.action == "replay":
            return _replay_response(db, session.id, request_id, redis_client)

        answer_row = (
            existing
            if check.action == "resume"
            else _register_new_answer(
                db,
                session=session,
                question=question,
                request_id=request_id,
                input_hash=input_hash,
                answer=answer,
                skipped=skipped,
                expected_state_version=expected_state_version,
            )
        )
        if check.action == "resume":
            answer_row.processing_attempt = (answer_row.processing_attempt or 0) + 1
            db.commit()

        return _run_and_persist(
            db=db,
            graph=graph,
            lock=lock,
            session=session,
            question=question,
            answer_row=answer_row,
            answer=answer,
            skipped=skipped,
            is_retry=check.action == "resume",
            redis_client=redis_client,
        )
    finally:
        lock.release()


def _register_new_answer(
    db: OrmSession,
    *,
    session: InterviewSession,
    question,
    request_id: str,
    input_hash: str,
    answer: str | None,
    skipped: bool,
    expected_state_version: int | None,
) -> InterviewAnswer:
    """登记新回答（短事务）。两道防线：先读后写 + 唯一约束兜底。"""
    prev = answers_repo.get_by_question(db, session.id, question.id)
    if prev is not None:
        raise StaleQuestionError("该题已提交过（首期一题一次），请刷新获取当前题")
    if session.current_question_id != question.id:
        raise StaleQuestionError("提交的不是当前题，请刷新获取当前题")

    try:
        row = answers_repo.create(
            db,
            session_id=session.id,
            question_id=question.id,
            request_id=request_id,
            input_hash=input_hash,
            submission_kind="skip" if skipped else "answer",
            answer_content=None if skipped else (answer or None),
            expected_state_version=expected_state_version,
        )
        db.commit()
    except IntegrityError as exc:
        # 并发窗口：另一个请求刚用同一 request_id/同一题登记成功
        db.rollback()
        again = answers_repo.get_by_request(db, session.id, request_id)
        if again is not None:
            if again.input_hash == input_hash:
                raise StateRecoveryRequiredError(
                    "该请求已被另一个请求处理，请稍后用同一 request_id 重试"
                ) from exc
            raise RequestConflictError() from exc
        raise StaleQuestionError("该题已被提交，请刷新获取当前题") from exc
    return row


def _run_and_persist(
    *,
    db: OrmSession,
    graph: CompiledStateGraph,
    lock: locks.SessionLock,
    session: InterviewSession,
    question,
    answer_row: InterviewAnswer,
    answer: str | None,
    skipped: bool,
    is_retry: bool,
    redis_client,
) -> dict:
    """跑图 + 落阶段结果 + CAS 推进版本。模型调用不放在数据库事务里。"""
    if not graph_runtime.has_checkpoint(graph, session.id):
        answers_repo.fail(
            db, answer_row, code="STATE_RECOVERY_REQUIRED",
            message="执行状态缺失，需要从记录恢复",
        )
        db.commit()
        raise StateRecoveryRequiredError()

    answers_repo.mark_stage(db, answer_row, "GRAPH_RUNNING")
    db.commit()

    _, _, waiting = graph_runtime.snapshot_state(graph, session.id)
    if not waiting and not is_retry:
        # 图不在等待点却来了新回答：执行位置与记录已不一致，
        # 继续跑只会把回答丢掉，不如明确要求恢复（§9.4）。
        answers_repo.fail(
            db, answer_row, code="STATE_RECOVERY_REQUIRED",
            message="执行位置异常，需要恢复会话",
        )
        db.commit()
        raise StateRecoveryRequiredError("会话执行位置异常，请先恢复会话")

    try:
        if waiting:
            payload_in = {"skipped": True} if skipped else {"answer": answer or ""}
            result = graph_runtime.resume_answer(graph, session.id, payload_in)
        else:
            # 该请求上次在节点里失败（没有 pending interrupt）：从断点继续，
            # 已完成的评分不会被重跑（LangGraph 只重跑失败节点）。
            result = graph_runtime.continue_run(graph, session.id)
    except AppError as exc:
        _persist_partial(db, graph, session, answer_row, exc)
        raise

    return _persist_success(
        db=db, graph=graph, lock=lock, session=session, answer_row=answer_row,
        result=result, redis_client=redis_client,
    )


def _persist_partial(
    db: OrmSession,
    graph: CompiledStateGraph,
    session: InterviewSession,
    answer_row: InterviewAnswer,
    exc: AppError,
) -> None:
    """失败也要把已成功的阶段写下来，供重试/恢复复用（§9.3 第 6 条）。"""
    values, _, _ = graph_runtime.snapshot_state(graph, session.id)
    _save_stage_results(db, answer_row, values)
    answers_repo.fail(db, answer_row, code=exc.code, message=exc.message)
    try:
        sessions_repo.bump_state_version(db, session)
        db.commit()
    except Exception:  # noqa: BLE001 —— 版本冲突时以记录为准，别掩盖原始错误
        db.rollback()
    log.warning(
        "会话 %s 提交处理失败 code=%s answer=%s", session.id, exc.code, answer_row.id
    )


def _save_stage_results(db: OrmSession, answer_row: InterviewAnswer, values: dict) -> None:
    """把图状态里的评分/裁决写进回答行（幂等：只写最近一条）。"""
    evaluations = values.get("evaluations") or []
    decisions = values.get("followup_decisions") or []
    evaluation = evaluations[-1] if evaluations else None
    decision = decisions[-1] if decisions else None

    if evaluation is not None or answer_row.evaluation is None:
        score = evaluation.get("score") if isinstance(evaluation, dict) else None
        answers_repo.save_evaluation(db, answer_row, evaluation=evaluation, score=score)
    if decision is not None:
        answers_repo.save_decision(db, answer_row, decision=decision)


def _persist_success(
    *,
    db: OrmSession,
    graph: CompiledStateGraph,
    lock: locks.SessionLock,
    session: InterviewSession,
    answer_row: InterviewAnswer,
    result: Any,
    redis_client,
) -> dict:
    """成功路径：阶段结果落库 → 下一题落库 → CAS → 快照 → 返回。"""
    values = result if isinstance(result, dict) else {}
    payload = graph_runtime.interrupt_payload(result)
    finished = payload is None

    _save_stage_results(db, answer_row, values)
    next_view = _next_question_payload(db, session.id, payload, values)

    # 写库前检查锁：租约失效说明可能已有别的请求接管，不能带病写入（§9.2）
    if lock.lost:
        db.rollback()
        raise VersionConflictError("会话锁已失效，本次结果未写入，请用同一 request_id 重试")

    new_version = sessions_repo.bump_state_version(db, session)
    sessions_repo.touch(
        db,
        session,
        status="FINISHED" if finished else "IN_PROGRESS",
        current_question_id=uuid.UUID(next_view["question_id"]) if next_view else None,
        progress=_progress(values, payload, finished=finished),
        completion_reason="ALL_ANSWERED" if finished else None,
        finished=finished,
    )

    decision = (values.get("followup_decisions") or [None])[-1]
    evaluation = (values.get("evaluations") or [None])[-1]
    response = {
        "answer_id": str(answer_row.id),
        "question_id": str(answer_row.question_id),
        "evaluation": public_evaluation(evaluation),
        "followup_decision": public_decision(decision),
        "next_question": next_view,
        "finished": finished,
        "state_version": new_version,
        "replayed": False,
    }
    answers_repo.complete(db, answer_row, response_snapshot=response, result_state_version=new_version)
    db.commit()

    idempotency.put_cached(redis_client, session.id, answer_row.request_id, response)
    log.info(
        "会话 %s 提交完成 answer=%s next=%s finished=%s v=%d",
        session.id, answer_row.id,
        (next_view or {}).get("main_index"), finished, new_version,
    )
    return response


def _replay_response(
    db: OrmSession, session_id: uuid.UUID, request_id: str, redis_client
) -> dict:
    """同 request_id 重试且已完成：原样返回上次响应，不推进、不调模型。"""
    cached = idempotency.get_cached(redis_client, session_id, request_id)
    if cached:
        return {**cached, "replayed": True}
    row = answers_repo.get_by_request(db, session_id, request_id)
    if row is None or row.response_snapshot is None:
        raise StateRecoveryRequiredError("该请求已登记但结果缺失，请稍后重试")
    return {**row.response_snapshot, "replayed": True}


# ---------------------------------------------------------------- 查询投影


def build_progress_view(
    *, db: OrmSession, graph: CompiledStateGraph, session: InterviewSession
) -> dict:
    """查询接口的进度投影（只读，不推进题号，§6.2 末条）。"""
    current = None
    if session.current_question_id is not None:
        row = questions_repo.get(db, session.current_question_id)
        if row is not None:
            current = questions_repo.public_dict(row)

    completed = [
        a for a in answers_repo.list_for_session(db, session.id)
        if a.processing_status == "COMPLETED"
    ]
    finished = session.status in sessions_repo.TERMINAL_STATUSES
    return {
        "session_id": str(session.id),
        "status": session.status,
        "state_version": session.state_version,
        "current_question": current,
        "answered_count": len(completed),
        "question_count": session.question_count,
        "finished": finished,
        # 已结束的会话"可恢复"没有意义（也不该允许接着答），别给前端错误暗示
        "recoverable": (not finished) and graph_runtime.has_checkpoint(graph, session.id),
        "progress": session.progress or {},
    }
