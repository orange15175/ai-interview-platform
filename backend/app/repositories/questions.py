"""题目仓储（开发说明 §8 interview_questions / §5 一题一次）。

关键点：
- 唯一约束 (session_id, main_index, follow_up_index) 是"不重复生成同一题"的
  数据库底线；仓储层先查后插，撞约束时回读已有记录（并发下的幂等兜底）。
- evaluation_rubric 只进数据库、只给评分 Agent 用，**永不出现在任何对外 dict 里**。
- 追问的 parent_question_id 指向主问题，便于恢复时按主问题回溯。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.models import InterviewQuestion
from app.schemas.questions import GeneratedQuestion


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def list_for_session(db: OrmSession, session_id: uuid.UUID | str) -> list[InterviewQuestion]:
    """按 (主问题序号, 追问序号) 升序——恢复时的推进顺序就是它。"""
    stmt = (
        select(InterviewQuestion)
        .where(InterviewQuestion.session_id == _as_uuid(session_id))
        .order_by(InterviewQuestion.main_index, InterviewQuestion.follow_up_index)
    )
    return list(db.scalars(stmt))


def get(db: OrmSession, question_id: uuid.UUID | str) -> InterviewQuestion | None:
    return db.get(InterviewQuestion, _as_uuid(question_id))


def get_by_index(
    db: OrmSession,
    session_id: uuid.UUID | str,
    main_index: int,
    follow_up_index: int = 0,
) -> InterviewQuestion | None:
    stmt = select(InterviewQuestion).where(
        InterviewQuestion.session_id == _as_uuid(session_id),
        InterviewQuestion.main_index == main_index,
        InterviewQuestion.follow_up_index == follow_up_index,
    )
    return db.scalars(stmt).one_or_none()


def insert_main_questions(
    db: OrmSession, session_id: uuid.UUID | str, questions: list[GeneratedQuestion]
) -> list[InterviewQuestion]:
    """批量落主问题，幂等：已存在的 (main_index, 0) 跳过并复用。

    幂等的意义：解析/出题重试、或创建接口被重复调用时，不会因为多插一批题
    把同一场面试变成 30 道题。
    """
    sid = _as_uuid(session_id)
    existing = {q.main_index: q for q in list_for_session(db, sid) if q.follow_up_index == 0}

    for q in sorted(questions, key=lambda x: x.main_index):
        if q.main_index in existing:
            continue
        db.add(
            InterviewQuestion(
                id=uuid.uuid4(),
                session_id=sid,
                kind="main",
                main_index=q.main_index,
                follow_up_index=0,
                parent_question_id=None,
                content=q.content,
                category=q.category,
                difficulty=q.difficulty,
                evaluation_rubric=[p.model_dump() for p in q.evaluation_rubric],
                material_evidence_ids=list(q.material_evidence_ids),
            )
        )
    db.flush()
    return list_for_session(db, sid)


def add_followup(
    db: OrmSession,
    *,
    session_id: uuid.UUID | str,
    parent: InterviewQuestion,
    follow_up_index: int,
    content: str,
    rubric: list[dict] | None = None,
    evidence_ids: list[str] | None = None,
) -> InterviewQuestion:
    """落一条追问；同一 (main_index, follow_up_index) 已存在则直接复用。

    并发/重试时靠唯一约束兜底：IntegrityError 说明另一路径已写入，回读即可。
    """
    sid = _as_uuid(session_id)
    row = get_by_index(db, sid, parent.main_index, follow_up_index)
    if row is not None:
        return row

    row = InterviewQuestion(
        id=uuid.uuid4(),
        session_id=sid,
        kind="follow_up",
        main_index=parent.main_index,
        follow_up_index=follow_up_index,
        parent_question_id=parent.id,
        content=content,
        category=parent.category,
        difficulty=parent.difficulty,
        evaluation_rubric=list(rubric if rubric is not None else parent.evaluation_rubric),
        material_evidence_ids=list(evidence_ids if evidence_ids is not None else parent.material_evidence_ids),
    )
    try:
        # SAVEPOINT：撞唯一约束只回滚这一条插入，不牵连调用方已写的内容。
        # 这里绝不能 db.rollback()——那会把外层事务里刚登记的 answer 一起抹掉。
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = get_by_index(db, sid, parent.main_index, follow_up_index)
        if existing is None:
            raise
        return existing
    return row


def public_dict(q: InterviewQuestion) -> dict:
    """对外投影：**剔除 evaluation_rubric**（泄露标准等于泄题，§5/§10）。"""
    return {
        "question_id": str(q.id),
        "kind": q.kind,
        "main_index": q.main_index,
        "follow_up_index": q.follow_up_index,
        "content": q.content,
        "category": q.category,
        "difficulty": q.difficulty,
    }
