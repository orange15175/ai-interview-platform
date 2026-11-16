"""从 PostgreSQL 正式记录重建面试图。

Redis checkpoint 只代表“图停在哪里”，PostgreSQL 才是题目和正式回答的事实来源。
recover_session 会先加锁，对未完成提交做对账，再生成 restore 图，并核对当前题、
评分数量和追问序号；任何一项对不上都要求再次恢复，不凭空猜结果。

边界先讲清楚，避免过度承诺：
- 能恢复：材料、题目、已完成回答及其评分/裁决（都已落库），以及"当前该问哪一题"。
- 不能恢复：未完成的模型调用、未落库的追问草稿。可重调或退回上一安全点。
- 绝不"把数据库字段塞进空 thread 就以为恢复了"：这里用的是**真正的恢复变体图**
  （builder 的 restore 入口 → wait_answer），重建后依然走 interrupt/resume。

一次恢复做的事（顺序不能乱）：
1. 拿会话锁（恢复本身也是修改操作，必须串行）。
2. 对账未完成提交：PENDING/PROCESSING → FAILED（保留记录，不重复计分）。
3. 找出安全等待点：按 (主问题, 追问) 顺序的第一道"没有已完成回答"的题。
4. 清掉该题上从未产生结果的空壳提交，让候选人能重新作答（否则唯一约束锁死）。
5. 清掉 Redis 里该 thread 的残留状态，用重建的初始 state 真正跑一次 resume 前的图。
6. 落当前题、进度、CAS 版本；释放锁。
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.orm import Session as OrmSession

from app.core.config import get_settings
from app.core.errors import (
    SessionFinishedError,
    StateRecoveryRequiredError,
    VersionConflictError,
)
from app.core.logging import get_logger
from app.models import InterviewAnswer, InterviewQuestion, InterviewSession
from app.repositories import answers as answers_repo
from app.repositories import questions as questions_repo
from app.repositories import sessions as sessions_repo
from app.services import graph_runtime, locks

log = get_logger(__name__)


def safe_wait_point(
    questions: list[InterviewQuestion], answers: dict[uuid.UUID, InterviewAnswer]
) -> InterviewQuestion | None:
    """按题序找到第一道没有完成回答的题，全部完成时返回 None。

    questions 必须已按 (main_index, follow_up_index) 升序——顺序就是他答题的顺序。
    这里不推断"本该有但没落库的追问"：宁可少问一次追问，也不重复计分。"""
    for q in questions:
        row = answers.get(q.id)
        if row is None or row.processing_status != "COMPLETED":
            return q
    return None


def _seed_state(
    session: InterviewSession,
    mains: list[InterviewQuestion],
    questions: list[InterviewQuestion],
    answers: dict[uuid.UUID, InterviewAnswer],
    pending: InterviewQuestion,
) -> dict[str, Any]:
    """把数据库题目、历史评分和待答位置转换成恢复图需要的初始状态。"""
    evaluations: list[dict | None] = []
    decisions: list[dict] = []
    asked: list[str] = []
    history: list[dict] = []

    for q in questions:
        # 已问过 = 顺序上不晚于待答点的题（含待答点本身，它即将被问）
        if (q.main_index, q.follow_up_index) <= (pending.main_index, pending.follow_up_index):
            asked.append(q.content)
        row = answers.get(q.id)
        if row is None or row.processing_status != "COMPLETED":
            continue
        if (q.main_index == pending.main_index
                and q.follow_up_index < pending.follow_up_index
                and row.submission_kind != "skip"):
            history.append({"question": q.content, "answer": row.answer_content or ""})
        evaluations.append(row.evaluation)  # skipped 的题存 None，保持顺序对齐
        if row.rule_decision:
            decisions.append(row.rule_decision)

    return {
        "session_id": str(session.id),
        "questions": [
            {
                "main_index": q.main_index,
                "kind": "main",
                "category": q.category,
                "difficulty": q.difficulty,
                "content": q.content,
                "material_evidence_ids": list(q.material_evidence_ids or []),
                "evaluation_rubric": list(q.evaluation_rubric or []),
            }
            for q in mains
        ],
        "current_main_index": pending.main_index,
        "current_followup_index": pending.follow_up_index,
        "pending_question": {
            "main_index": pending.main_index,
            "kind": pending.kind,
            "follow_up_index": pending.follow_up_index,
            "content": pending.content,
            "category": pending.category,
            "difficulty": pending.difficulty,
            "evaluation_rubric": list(pending.evaluation_rubric or []),
            "material_evidence_ids": list(pending.material_evidence_ids or []),
        },
        "evaluations": evaluations,
        "followup_decisions": decisions,
        "asked_contents": asked,
        "answer_history": history,
        "finished": False,
    }


def _reconcile_unfinished(
    db: OrmSession, answers: list[InterviewAnswer]
) -> list[dict]:
    """把中断的 PENDING/PROCESSING 提交记成失败，供后续决定续跑或清理。

    只处理"确实没有可复用结果"的行。带评分的行走 _complete_evaluated，
    否则会把已经花过钱、也已经算出来的评分当成未作答重来一遍。"""
    reconciled: list[dict] = []
    for row in answers:
        if row.processing_status in ("PENDING", "PROCESSING"):
            answers_repo.fail(
                db, row, code="RECONCILED_ON_RECOVERY",
                message="该次提交在恢复时被对账关闭，请重新提交本题",
            )
            reconciled.append({"answer_id": str(row.id), "stage": row.processing_stage})
    return reconciled


def _decision_record(question: InterviewQuestion, evaluation: dict | None,
                     skipped: bool, asked_targets: list[str]) -> dict:
    """根据已有评分重新计算追问规则结果，不调用模型重新评分。"""
    from app.schemas.evaluation import Evaluation
    from app.rules.followup_rules import decide_followup

    parsed = Evaluation.model_validate(evaluation) if evaluation else None
    decision = decide_followup(
        evaluation=parsed,
        skipped=skipped,
        followup_count=question.follow_up_index,
        asked_targets=asked_targets,
        max_followups=get_settings().business.max_followups,
    )
    return {
        "main_index": question.main_index,
        "decision": decision.decision,
        "reason_code": decision.reason_code,
        "rule_version": decision.rule_version,
        "detail": decision.detail,
        "target": (parsed.follow_up_target if parsed else "") or "",
    }


def _complete_evaluated(
    db: OrmSession,
    session: InterviewSession,
    ordered: list[InterviewQuestion],
    answers: dict[uuid.UUID, InterviewAnswer],
    row: InterviewAnswer,
    next_view: dict | None,
) -> bool:
    """已有评分但未完成的回答，只补追问决定和响应快照，避免丢掉已算出的分数。

    这正是"checkpoint 丢失 + 已有评分"的收口：把它当成未作答题会导致
    1) 该题被唯一约束永久锁死（评分行删不掉）；2) 已经算出的分数被丢弃。"""
    from app.services.interview_workflow import public_decision, public_evaluation

    question = next((q for q in ordered if q.id == row.question_id), None)
    if question is None:
        return False

    evaluation = row.evaluation if isinstance(row.evaluation, dict) else None
    skipped = row.submission_kind == "skip"

    if row.rule_decision is None:
        asked = [
            a.rule_decision.get("target", "")
            for a in answers.values()
            if a.rule_decision
            and a.question_id != row.question_id
            and a.rule_decision.get("main_index") == question.main_index
            and a.rule_decision.get("decision") == "ask"
        ]
        asked = [t for t in asked if t]
        answers_repo.save_decision(
            db, row,
            decision=_decision_record(question, evaluation, skipped, asked),
        )

    snapshot = {
        "answer_id": str(row.id),
        "question_id": str(row.question_id),
        "evaluation": public_evaluation(evaluation),
        "followup_decision": public_decision(row.rule_decision),
        "next_question": next_view,
        "finished": next_view is None,
        "state_version": session.state_version,
        "replayed": False,
        "rebuilt": True,
    }
    answers_repo.complete(
        db, row, response_snapshot=snapshot, result_state_version=session.state_version,
        epoch=session.write_epoch,
    )
    return True


def _drop_empty_attempt(db: OrmSession, row: InterviewAnswer | None) -> bool:
    """删除当前待答题上没有评分也没有裁决的空壳提交，让这道题可以重新提交。"""
    if row is None or row.processing_status == "COMPLETED":
        return False
    if row.evaluation is not None or row.rule_decision is not None:
        return False
    answers_repo.delete(db, row)
    return True


def recover_session(
    *,
    db: OrmSession,
    restore_graph: CompiledStateGraph,
    session: InterviewSession,
) -> dict:
    """拿锁后对账数据库回答，清理旧检查点并重建图，最后核对并保存新的等待位置。"""
    lock = locks.acquire(str(session.id))
    lock.start_auto_renew()
    try:
        db.refresh(session)
        # 这里只检查已写入的状态，没有按 last_activity_at 再算超时。
        # 如果过期状态未持久化，后续 touch 会刷新活动时间，这是现有生命周期缺口。
        if session.status in ("CANCELLED", "EXPIRED", "PREPARATION_FAILED"):
            raise SessionFinishedError("会话已结束，不能恢复")
        if session.status == "FINISHED":
            return {
                "session_id": str(session.id),
                "status": session.status,
                "state_version": session.state_version,
                "question": None,
                "finished": True,
                "recovered": False,
                "reconciled": [],
            }

        questions = questions_repo.list_for_session(db, session.id)
        ordered = sorted(questions, key=lambda q: (q.main_index, q.follow_up_index))
        mains = [q for q in ordered if q.follow_up_index == 0]
        if not mains:
            raise StateRecoveryRequiredError("会话没有题目记录，无法恢复，请重新创建面试")

        # 恢复同样是写入：取得栅栏。之后所有对会话的写入都带 epoch，
        # 中途若有人接管了锁（比如另一次恢复），本次结果一律不落库。
        fence = sessions_repo.begin_write(db, session)

        all_answers = answers_repo.list_for_session(db, session.id)
        by_question = {a.question_id: a for a in all_answers}

        # 1) 有评分但没走完的回答：补齐裁决与快照，**绝不能**当成未作答重来
        backfilled = 0
        for i, q in enumerate(ordered):
            row = by_question.get(q.id)
            if row is None or row.processing_status == "COMPLETED":
                continue
            if row.evaluation is None:
                continue
            next_q = ordered[i + 1] if i + 1 < len(ordered) else None
            next_view = questions_repo.public_dict(next_q) if next_q is not None else None
            if next_view is not None:
                next_view["total_questions"] = len(mains)
            if _complete_evaluated(db, session, ordered, by_question, row, next_view):
                backfilled += 1

        # 2) 其余卡在半途的提交：留痕关闭（不删除）
        reconciled = _reconcile_unfinished(db, all_answers)

        pending = safe_wait_point(ordered, by_question)
        if pending is None:
            sessions_repo.check_fence(session, fence)
            sessions_repo.touch(
                db, session, status="FINISHED", current_question_id=None,
                completion_reason="ALL_ANSWERED", finished=True,
                progress={"stage": "FINISHED"},
            )
            sessions_repo.bump_state_version(db, session, fence=fence)
            db.commit()
            return {
                "session_id": str(session.id),
                "status": "FINISHED",
                "state_version": session.state_version,
                "question": None,
                "finished": True,
                "recovered": False,
                "reconciled": reconciled,
                "backfilled_evaluated": backfilled,
            }

        dropped = _drop_empty_attempt(db, by_question.get(pending.id))
        by_question.pop(pending.id, None)
        db.commit()  # 先把对账结果落地，再动图（跨存储不能指望一个事务）

        # 清掉残留的 Redis 状态：带着半截检查点灌初始 state 会被 LangGraph 当成
        # 对已有 thread 的新输入，行为不可预期。
        saver = getattr(restore_graph, "checkpointer", None)
        if saver is not None:
            graph_runtime.delete_thread(saver, session.id)

        seed = _seed_state(session, mains, ordered, by_question, pending)
        result = restore_graph.invoke(seed, graph_runtime.thread_config(session.id))
        payload = graph_runtime.interrupt_payload(result)
        if payload is None:
            raise StateRecoveryRequiredError("恢复未停在等待点，请稍后重试")

        _verify_rebuilt(restore_graph, session, pending, by_question, saver)

        if lock.lost:
            raise VersionConflictError("恢复过程中锁已失效，请重试")

        completed_count = sum(
            1 for a in by_question.values() if a.processing_status == "COMPLETED"
        )
        sessions_repo.check_fence(session, fence)
        new_version = sessions_repo.bump_state_version(db, session, fence=fence)
        sessions_repo.touch(
            db, session, status="IN_PROGRESS",
            current_question_id=pending.id,
            progress={
                "stage": "WAITING_ANSWER",
                "current_main_index": pending.main_index,
                "current_followup_index": pending.follow_up_index,
                "answered_count": completed_count,
                "recovered_at": True,
            },
            started=True,
        )
        db.commit()

        log.info(
            "会话 %s 已从记录恢复：停在 main=%s follow=%s，对账 %d 条，补写评分 %d 条，清理空壳 %s",
            session.id, pending.main_index, pending.follow_up_index,
            len(reconciled), backfilled, dropped,
        )
        view = questions_repo.public_dict(pending)
        view["total_questions"] = len(mains)
        return {
            "session_id": str(session.id),
            "status": session.status,
            "state_version": new_version,
            "question": view,
            "finished": False,
            "recovered": True,
            "reconciled": reconciled,
            "backfilled_evaluated": backfilled,
            "dropped_empty_attempts": 1 if dropped else 0,
        }
    finally:
        lock.release()


def _verify_rebuilt(
    graph: CompiledStateGraph,
    session: InterviewSession,
    pending: InterviewQuestion,
    by_question: dict[uuid.UUID, InterviewAnswer],
    saver,
) -> None:
    """核对恢复图的当前题、评分数量和追问号；对不上就清理图并报恢复失败。

    只 invoke 一次就宣布恢复成功是不够的：种子状态写错一个下标，图会安静地
    停到别的题上，用户随后答的每一道题都会被记到错误的题号下。所以这里逐条验：
    - 停在 wait_answer（不是节点失败、不是跑完）；
    - 中断坐标 == 数据库当前题；
    - 图里的评分条数 == 数据库已完成条数（少了说明历史没重建，多了说明重算了）；
    - follow-up 次数没有被清零（清零会让追问次数超上限）。
    任何一条不满足 → 删掉这个不可信的 thread，要求重试恢复。"""
    snap = graph_runtime.snapshot(graph, session.id)
    expected_completed = sum(
        1 for a in by_question.values() if a.processing_status == "COMPLETED"
    )
    evaluations = snap.values.get("evaluations") or []
    followups = snap.values.get("current_followup_index")

    problem = None
    if snap.stage != graph_runtime.GRAPH_WAITING:
        problem = f"未停在等待点（{snap.stage}）"
    elif snap.waits_for(pending.main_index, pending.follow_up_index) is not True:
        problem = (f"中断坐标 ({snap.wait_main_index},{snap.wait_follow_up_index}) "
                   f"与数据库当前题 ({pending.main_index},{pending.follow_up_index}) 不一致")
    elif len(evaluations) != expected_completed:
        problem = f"评分条数 {len(evaluations)} != 数据库已完成 {expected_completed}"
    elif followups != pending.follow_up_index:
        problem = f"追问次数 {followups} != 当前题追问序号 {pending.follow_up_index}"

    if problem:
        if saver is not None:
            graph_runtime.delete_thread(saver, session.id)
        raise StateRecoveryRequiredError(f"恢复结果与记录不一致：{problem}")
