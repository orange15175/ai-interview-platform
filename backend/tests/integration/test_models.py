"""ORM 模型行为测试（SQLite 内存库）。

重点验证开发说明 §8 的三条约束：
1. (session_id, request_id) 唯一 → 幂等底线
2. (session_id, question_id) 唯一 → 首期每题只许一次提交
3. (session_id, main_index, follow_up_index) 唯一 → 题目不重复
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    AnonymousVisitor,
    InterviewAnswer,
    InterviewMaterial,
    InterviewQuestion,
    InterviewSession,
)


@pytest.fixture()
def visitor(db_session):
    v = AnonymousVisitor(token_hash="h" * 64,
                         expires_at=datetime.now() + timedelta(days=30))
    db_session.add(v)
    db_session.commit()
    return v


@pytest.fixture()
def session_(db_session, visitor):
    s = InterviewSession(owner_id=visitor.id)
    db_session.add(s)
    db_session.commit()
    return s


def test_full_chain_insert(db_session, session_):
    """访客 → 会话 → 材料 → 题目 → 回答，外键链路完整可写。"""
    material = InterviewMaterial(
        session_id=session_.id,
        resume_profile={"skills": ["Python"]},
        parse_model="qwen3.8-omni-flash",
    )
    question = InterviewQuestion(
        session_id=session_.id, kind="main", main_index=1, follow_up_index=0,
        content="介绍一个项目", category="项目经验", difficulty="medium",
        evaluation_rubric=[{"point": "清晰", "weight": 1.0}],
    )
    db_session.add_all([material, question])
    db_session.commit()

    answer = InterviewAnswer(
        session_id=session_.id, question_id=question.id, request_id="req-001",
        input_hash="abc", submission_kind="answer", answer_content="我的回答",
    )
    db_session.add(answer)
    db_session.commit()

    # 跳过的回答 score 允许为空（不给兜底 0 分，§7.1）
    assert answer.score is None


def test_unique_request_id(db_session, session_):
    """同一 request_id 第二次落库必须被数据库挡下。"""
    q = InterviewQuestion(
        session_id=session_.id, kind="main", main_index=1, follow_up_index=0,
        content="q1", category="c", difficulty="easy",
    )
    db_session.add(q)
    db_session.flush()

    for rid in ("req-same", "req-same"):
        db_session.add(InterviewAnswer(
            session_id=session_.id, question_id=q.id, request_id=rid,
            input_hash="x", submission_kind="answer",
        ))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_unique_question_per_session(db_session, session_):
    """同会话重复插入 (main_index=1, follow_up_index=0) 会被拒绝。"""
    for _ in range(2):
        db_session.add(InterviewQuestion(
            session_id=session_.id, kind="main", main_index=1, follow_up_index=0,
            content="dup", category="c", difficulty="easy",
        ))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_followup_allowed_alongside_main(db_session, session_):
    """主问题 + 两次追问共存合法（unique 约束按序号区分）。"""
    db_session.add_all([
        InterviewQuestion(session_id=session_.id, kind="main", main_index=1, follow_up_index=0,
                          content="main", category="c", difficulty="easy"),
        InterviewQuestion(session_id=session_.id, kind="follow_up", main_index=1, follow_up_index=1,
                          content="f1", category="c", difficulty="easy"),
        InterviewQuestion(session_id=session_.id, kind="follow_up", main_index=1, follow_up_index=2,
                          content="f2", category="c", difficulty="easy"),
    ])
    db_session.commit()
    assert db_session.query(InterviewQuestion).count() == 3


def test_material_one_per_session(db_session, session_):
    """一场会话只能有一份材料。"""
    for _ in range(2):
        db_session.add(InterviewMaterial(
            session_id=session_.id, resume_profile={}, parse_model="m",
        ))
    with pytest.raises(IntegrityError):
        db_session.commit()
