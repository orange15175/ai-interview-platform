"""Redis checkpoint 丢失后，从 PostgreSQL 显式重建到等待点（开发说明 §9.4）。

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
    """第一道没有"已完成回答"的题；全部答完返回 None。

    questions 必须已按 (main_index, follow_up_index) 升序——顺序就是他答题的顺序。
    这里不推断"本该有但没落库的追问"：宁可少问一次追问，也不重复计分。
    """
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
    """把数据库记录翻译成图能直接消费的初始 state（restore 变体用）。"""
    evaluations: list[dict | None] = []
    decisions: list[dict] = []
    asked: list[str] = []

    for q in questions:
        # 已问过 = 顺序上不晚于待答点的题（含待答点本身，它即将被问）
        if (q.main_index, q.follow_up_index) <= (pending.main_index, pending.follow_up_index):
            asked.append(q.content)
        row = answers.get(q.id)
        if row is None or row.processing_status != "COMPLETED":
            continue
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
        "finished": False,
    }


def _reconcile_unfinished(
    db: OrmSession, answers: list[InterviewAnswer]
) -> list[dict]:
    """对账：把卡在 PENDING/PROCESSING 的提交标成 FAILED（留痕，不删除）。"""
    reconciled: list[dict] = []
    for row in answers:
        if row.processing_status in ("PENDING", "PROCESSING"):
            answers_repo.fail(
                db, row, code="RECONCILED_ON_RECOVERY",
                message="该次提交在恢复时被对账关闭，请重新提交本题",
            )
            reconciled.append({"answer_id": str(row.id), "stage": row.processing_stage})
    return reconciled


def _drop_empty_attempt(db: OrmSession, row: InterviewAnswer | None) -> bool:
    """清掉待答题上的空壳提交（无评分、无裁决），否则这题永远提交不了。"""
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
    """执行一次显式恢复。调用方必须先做归属校验（get_owned）。"""
    lock = locks.acquire(str(session.id))
    lock.start_auto_renew()
    try:
        db.refresh(session)
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
        mains = [q for q in questions if q.follow_up_index == 0]
        if not mains:
            raise StateRecoveryRequiredError("会话没有题目记录，无法恢复，请重新创建面试")

        all_answers = answers_repo.list_for_session(db, session.id)
        by_question = {a.question_id: a for a in all_answers}
        reconciled = _reconcile_unfinished(db, all_answers)

        pending = safe_wait_point(questions, by_question)
        if pending is None:
            sessions_repo.touch(
                db, session, status="FINISHED", current_question_id=None,
                completion_reason="ALL_ANSWERED", finished=True,
                progress={"stage": "FINISHED"},
            )
            sessions_repo.bump_state_version(db, session)
            db.commit()
            return {
                "session_id": str(session.id),
                "status": "FINISHED",
                "state_version": session.state_version,
                "question": None,
                "finished": True,
                "recovered": False,
                "reconciled": reconciled,
            }

        dropped = _drop_empty_attempt(db, by_question.get(pending.id))
        by_question.pop(pending.id, None)
        db.commit()  # 先把对账结果落地，再动图（跨存储不能指望一个事务）

        # 清掉残留的 Redis 状态：带着半截检查点灌初始 state 会被 LangGraph 当成
        # 对已有 thread 的新输入，行为不可预期。
        saver = getattr(restore_graph, "checkpointer", None)
        if saver is not None:
            graph_runtime.delete_thread(saver, session.id)

        seed = _seed_state(session, mains, questions, by_question, pending)
        result = restore_graph.invoke(seed, graph_runtime.thread_config(session.id))
        payload = graph_runtime.interrupt_payload(result)
        if payload is None:
            raise StateRecoveryRequiredError("恢复未停在等待点，请稍后重试")

        if lock.lost:
            raise VersionConflictError("恢复过程中锁已失效，请重试")

        new_version = sessions_repo.bump_state_version(db, session)
        sessions_repo.touch(
            db, session, status="IN_PROGRESS",
            current_question_id=pending.id,
            progress={
                "stage": "WAITING_ANSWER",
                "current_main_index": pending.main_index,
                "current_followup_index": pending.follow_up_index,
                "answered_count": sum(
                    1 for a in by_question.values()
                    if a.processing_status == "COMPLETED" and a.evaluation is not None
                ),
                "recovered_at": True,
            },
            started=True,
        )
        db.commit()

        log.info(
            "会话 %s 已从记录恢复：停在 main=%s follow=%s，对账 %d 条，清理空壳 %s",
            session.id, pending.main_index, pending.follow_up_index, len(reconciled), dropped,
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
            "dropped_empty_attempts": 1 if dropped else 0,
        }
    finally:
        lock.release()
