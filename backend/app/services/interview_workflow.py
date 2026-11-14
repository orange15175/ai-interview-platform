"""面试流程编排：启动、提交回答（开发说明 §9.3 逐条落地）。

这一层不实现 HTTP，只实现"一次业务动作要做完哪些事、按什么顺序、失败怎么收场"。
API 层（阶段 5）拿到 session_id/visitor_id 后调用这里；CLI 与测试也调这里。

提交回答的 8 步（对应 §9.3）：
1. 归属与题目校验；2. **幂等判定优先**（replay/conflict）——即使会话已结束也要
   先回放；3. 终态检查；4. 拿会话锁，锁内重新读取并再次幂等检查；
5. 短事务登记回答（PENDING）并记下图当前位置；6. 恢复图执行评分/规则/追问
   （模型调用不在事务里）；7. 阶段结果幂等写库（评分→裁决→响应快照）；
8. CAS 递增 state_version，写 Redis 幂等缓存，释放锁。

跨存储不一致的处理口径（§9.3 末段，必须诚实）：
- 图已推进、DB 未写 → 同一 request_id 重试**只补写**数据库与快照，绝不把旧
  回答再喂给下一题（那会直接吃掉一道题）；
- 节点失败（无 pending interrupt）→ continue_run，只重跑失败节点；
- DB 已写、checkpoint 丢 → 走恢复入口从 PG 重建（recovery.py）；
- 锁在写库前丢失 → 抛 VERSION_CONFLICT，绝不带病写入，交给恢复对账；
- 对不上账（图里找不到这次回答的结果）→ STATE_RECOVERY_REQUIRED，不猜分数。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
    user_id: uuid.UUID | str | None = None,
    redis_client=None,
) -> dict:
    """提交一次回答（或跳过），推进到下一题或结束。

    user_id：登录用户身份（可选）。带上它就允许"换浏览器重新登录后继续答"，
    不带则只认匿名访客 Cookie，行为与之前完全一致。

    **顺序是有意的**（开发说明 §一 第 2 条）：
    归属校验 → 题目校验 → 幂等判定（回放/冲突）→ 终态检查。
    幂等回放必须排在终态检查**之前**：最后一题提交成功但响应丢失时，会话
    已经是 FINISHED，若先查终态就会把"我要拿回上一次的结果"变成
    SESSION_FINISHED，用户永远拿不回那份评分。
    """
    if not request_id or len(request_id) > 64:
        raise RequestConflictError("request_id 缺失或过长（<=64 字符）")

    session = sessions_repo.get_owned(db, session_id, owner_id, user_id=user_id)

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

    # 到这里一定是"要真正处理"（新提交或续跑），终态检查才有意义
    sessions_repo.assert_can_answer(session)

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


@dataclass(frozen=True)
class AnswerPosition:
    """这条回答在整场面试题序里的位置（对账用）。

    index：第几条回答（0 基，按题目顺序）。图的 evaluations 列表就是按这个
    顺序追加的，所以 index 是"从图状态里取回这次评分"的唯一键——不能像以前
    那样盲取 evaluations[-1]：图可能已经推进到下一题，最后一个元素是别人的。
    """

    main_index: int
    follow_up_index: int
    index: int
    completed_before: int

    @property
    def coords(self) -> tuple[int, int]:
        return (self.main_index, self.follow_up_index)


def _answer_position(
    db: OrmSession, session_id: uuid.UUID, question
) -> AnswerPosition:
    """按题序算出这条回答的位置，以及它之前已经完成的回答条数。"""
    ordered = questions_repo.list_for_session(db, session_id)
    ordered.sort(key=lambda q: (q.main_index, q.follow_up_index))
    index = next(
        (i for i, q in enumerate(ordered) if q.id == question.id), None
    )
    if index is None:
        raise StateRecoveryRequiredError("题目不在该会话的题序里，无法对账")

    answers = {a.question_id: a for a in answers_repo.list_for_session(db, session_id)}
    completed_before = sum(
        1
        for q in ordered[:index]
        if (a := answers.get(q.id)) is not None and a.processing_status == "COMPLETED"
    )
    return AnswerPosition(
        main_index=question.main_index,
        follow_up_index=question.follow_up_index,
        index=index,
        completed_before=completed_before,
    )


def _stage_result(values: dict, index: int) -> tuple[dict | None, dict | None]:
    """从图状态里按位置取本次回答的评分与裁决（越界返回 None，不猜）。"""
    evaluations = values.get("evaluations") or []
    decisions = values.get("followup_decisions") or []
    evaluation = evaluations[index] if 0 <= index < len(evaluations) else None
    decision = decisions[index] if 0 <= index < len(decisions) else None
    return evaluation, decision


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
    """跑图 + 落阶段结果 + CAS 推进版本。模型调用不放在数据库事务里。

    这里最要紧的一件事：**先对账，再决定跑不跑**。
    以前只要"没有 pending interrupt"就 continue_run，于是"图已经推进到下一题、
    数据库没写上"的重试会把旧回答喂给下一题（跳题）。现在按图的真实位置分四种：
    - 停在等待点且等的就是这一题  → 正常 resume；
    - 停在等待点但等的是别的题    → 图已消费过这次回答，只补写，绝不重投；
    - 节点失败（无 interrupt）    → continue_run，只重跑失败节点（评分不重算）；
    - 图已跑完                    → 只补写最终响应；
    - 没有 checkpoint             → STATE_RECOVERY_REQUIRED。
    """
    snap = graph_runtime.snapshot(graph, session.id)
    if snap.stage == graph_runtime.GRAPH_MISSING:
        answers_repo.fail(
            db, answer_row, code="STATE_RECOVERY_REQUIRED",
            message="执行状态缺失，需要从记录恢复",
        )
        db.commit()
        raise StateRecoveryRequiredError()

    position = _answer_position(db, session.id, question)

    answers_repo.mark_stage(db, answer_row, "GRAPH_RUNNING")
    # 记下"处理前图在哪"：这次重试能不能重投，全靠它和当前快照对比
    answers_repo.record_graph_position(
        db,
        answer_row,
        stage=snap.stage,
        wait_question_id=_wait_question_id(db, session.id, snap),
    )
    db.commit()

    if snap.stage == graph_runtime.GRAPH_WAITING:
        verdict = snap.waits_for(*position.coords)
        if verdict is True:
            payload_in = {"skipped": True} if skipped else {"answer": answer or ""}
            try:
                result = graph_runtime.resume_answer(graph, session.id, payload_in)
            except AppError as exc:
                _persist_partial(db, graph, session, answer_row, position, exc)
                raise
            return _persist_success(
                db=db, graph=graph, lock=lock, session=session,
                answer_row=answer_row, position=position, result=result,
                redis_client=redis_client,
            )

        if verdict is False and is_retry:
            # 图已经推进过：这次回答早被消费了，只能把数据库和快照补齐
            log.warning(
                "会话 %s 检测到图已推进（等 %s.%s，本条为 %s.%s），改为补写不重投",
                session.id, snap.wait_main_index, snap.wait_follow_up_index,
                position.main_index, position.follow_up_index,
            )
            return _rebuild_from_state(
                db=db, graph=graph, lock=lock, session=session,
                answer_row=answer_row, position=position, snap=snap,
                redis_client=redis_client,
            )

        # 首次提交却与图位置不符（或 payload 缺坐标）：不猜，交恢复流程
        answers_repo.fail(
            db, answer_row, code="STATE_RECOVERY_REQUIRED",
            message="执行位置与记录不一致，需要恢复会话",
        )
        db.commit()
        raise StateRecoveryRequiredError("会话执行位置与记录不一致，请先恢复会话")

    if snap.stage == graph_runtime.GRAPH_NODE_FAILED:
        # 节点失败：从断点继续。LangGraph 只重跑失败节点，已完成的评分不会重算。
        try:
            result = graph_runtime.continue_run(graph, session.id)
        except AppError as exc:
            _persist_partial(db, graph, session, answer_row, position, exc)
            raise
        return _persist_success(
            db=db, graph=graph, lock=lock, session=session,
            answer_row=answer_row, position=position, result=result,
            redis_client=redis_client,
        )

    if is_retry:
        # 图已跑完（无 next、无 interrupt）：只补写数据库与快照
        return _rebuild_from_state(
            db=db, graph=graph, lock=lock, session=session,
            answer_row=answer_row, position=position, snap=snap,
            redis_client=redis_client,
        )

    answers_repo.fail(
        db, answer_row, code="STATE_RECOVERY_REQUIRED",
        message="执行位置异常，需要恢复会话",
    )
    db.commit()
    raise StateRecoveryRequiredError("会话执行位置异常，请先恢复会话")


def _wait_question_id(
    db: OrmSession, session_id: uuid.UUID, snap: graph_runtime.GraphSnapshot
) -> uuid.UUID | None:
    """把中断里的 (主问题, 追问) 坐标翻成数据库题目 id（查不到返回 None）。"""
    if snap.wait_main_index is None or snap.wait_follow_up_index is None:
        return None
    row = questions_repo.get_by_index(
        db, session_id, snap.wait_main_index, snap.wait_follow_up_index
    )
    return row.id if row is not None else None


def _rebuild_from_state(
    *,
    db: OrmSession,
    graph: CompiledStateGraph,
    lock: locks.SessionLock,
    session: InterviewSession,
    answer_row: InterviewAnswer,
    position: AnswerPosition,
    snap: graph_runtime.GraphSnapshot,
    redis_client,
) -> dict:
    """图已消费过这次回答，但数据库没写全：从图状态补齐，**不重投旧回答**。

    校验很硬：图状态里必须真的存在"第 index 条评分"。缺了就是不匹配，
    宁可要求恢复，也不凭空编一个分数。
    """
    values = snap.values
    evaluations = values.get("evaluations") or []
    if position.completed_before >= len(evaluations):
        answers_repo.fail(
            db, answer_row, code="STATE_RECOVERY_REQUIRED",
            message="图状态里找不到本次回答的结果，需要恢复会话",
        )
        db.commit()
        raise StateRecoveryRequiredError("图状态与记录无法对账，请先恢复会话")

    payload = snap.interrupt if snap.stage == graph_runtime.GRAPH_WAITING else None
    return _finalize(
        db=db, graph=graph, lock=lock, session=session,
        answer_row=answer_row, position=position, values=values, payload=payload,
        redis_client=redis_client, rebuilt=True,
    )


def _persist_partial(
    db: OrmSession,
    graph: CompiledStateGraph,
    session: InterviewSession,
    answer_row: InterviewAnswer,
    position: AnswerPosition,
    exc: AppError,
) -> None:
    """失败也要把已成功的阶段写下来，供重试/恢复复用（§9.3 第 6 条）。"""
    values, _, _ = graph_runtime.snapshot_state(graph, session.id)
    evaluation, decision = _stage_result(values, position.index)
    _save_stage_results(db, answer_row, evaluation=evaluation, decision=decision)
    answers_repo.fail(db, answer_row, code=exc.code, message=exc.message)
    try:
        sessions_repo.bump_state_version(db, session)
        db.commit()
    except Exception:  # noqa: BLE001 —— 版本冲突时以记录为准，别掩盖原始错误
        db.rollback()
    log.warning(
        "会话 %s 提交处理失败 code=%s answer=%s", session.id, exc.code, answer_row.id
    )


def _save_stage_results(
    db: OrmSession,
    answer_row: InterviewAnswer,
    *,
    evaluation: dict | None,
    decision: dict | None,
) -> None:
    """把图状态里的评分/裁决写进回答行（幂等：同样的值重复写不会变）。"""
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
    position: AnswerPosition,
    result: Any,
    redis_client,
) -> dict:
    """跑图成功：交给 _finalize 落库（与"补写"共用同一条收尾路径）。"""
    values = result if isinstance(result, dict) else {}
    payload = graph_runtime.interrupt_payload(result)
    return _finalize(
        db=db, graph=graph, lock=lock, session=session,
        answer_row=answer_row, position=position, values=values, payload=payload,
        redis_client=redis_client, rebuilt=False,
    )


def _finalize(
    *,
    db: OrmSession,
    graph: CompiledStateGraph,
    lock: locks.SessionLock,
    session: InterviewSession,
    answer_row: InterviewAnswer,
    position: AnswerPosition,
    values: dict,
    payload: dict | None,
    redis_client,
    rebuilt: bool,
) -> dict:
    """成功收尾：阶段结果 → 下一题落库 → 锁检查 → CAS → 快照 → 缓存。"""
    finished = payload is None
    evaluation, decision = _stage_result(values, position.index)

    _save_stage_results(db, answer_row, evaluation=evaluation, decision=decision)
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
    if rebuilt:
        response["rebuilt"] = True

    answers_repo.complete(
        db, answer_row, response_snapshot=response, result_state_version=new_version
    )
    db.commit()

    idempotency.put_cached(redis_client, session.id, answer_row.request_id, response)
    log.info(
        "会话 %s 提交完成 answer=%s next=%s finished=%s v=%d%s",
        session.id, answer_row.id,
        (next_view or {}).get("main_index"), finished, new_version,
        "（由图状态补写）" if rebuilt else "",
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
