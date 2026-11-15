"""数据库级写入代次（write_epoch）的语义（§四）。

要证明的只有一件事：**旧请求在锁被接管后，一条也写不进去**。
所以每个用例都从"两个持有者"出发——A 拿了栅栏，B 接管并推进，
A 迟到返回，然后检查：
1) A 的写入被拒（WRITE_FENCE_LOST，而不是笼统的版本冲突）；
2) A 已经 flush 的半截写入被回滚，数据库里看不到。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import VersionConflictError, WriteFenceLostError
from app.models import Base, InterviewAnswer, InterviewSession
from app.repositories import answers as answers_repo
from app.repositories import questions as questions_repo
from app.repositories import sessions as sessions_repo
from app.repositories import visitors as visitors_repo
from app.schemas.questions import GeneratedQuestion, RubricPoint


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    visitor, _ = visitors_repo.create(db)
    session = sessions_repo.create(db, owner_id=visitor.id, question_count=1)
    db.commit()
    question = questions_repo.insert_main_questions(
        db, session.id,
        [GeneratedQuestion(
            main_index=1, category="项目经验", difficulty="medium",
            content="请说明你做过的第 1 个技术决策的完整权衡过程。",
            material_evidence_ids=["ev-1"],
            evaluation_rubric=[RubricPoint(point="讲清背景与权衡", weight=1.0)],
        )],
    )[0]
    answer = answers_repo.create(
        db, session_id=session.id, question_id=question.id,
        request_id="req-1", input_hash="h1", submission_kind="answer",
        answer_content="我的回答",
    )
    db.commit()
    try:
        yield type("Env", (), {
            "db": db, "session": session, "question": question, "answer": answer,
        })()
    finally:
        db.close()
        engine.dispose()


def _reload(db, session_id) -> InterviewSession:
    db.expire_all()
    return db.get(InterviewSession, session_id)


# ---------------------------------------------------------------- 基础语义


def test_begin_write_increments_epoch_and_snapshots_version(env):
    fence1 = sessions_repo.begin_write(env.db, env.session)
    assert fence1.epoch == 1
    assert fence1.state_version == 0

    fence2 = sessions_repo.begin_write(env.db, env.session)
    assert fence2.epoch == 2  # 每次拿锁都递增，绝不复用
    assert fence2.state_version == 0

    stored = _reload(env.db, env.session.id)
    assert stored.write_epoch == 2


def test_fenced_bump_moves_both_layers(env):
    fence = sessions_repo.begin_write(env.db, env.session)
    new_version = sessions_repo.bump_state_version(env.db, env.session, fence=fence)
    env.db.commit()

    assert new_version == 1
    stored = _reload(env.db, env.session.id)
    assert stored.state_version == 1
    assert stored.write_epoch == fence.epoch  # 写入者没有换人


# ---------------------------------------------------------------- 迟到写入


def test_late_writer_after_takeover_is_rejected(env):
    """A 拿栅栏 → B 接管（epoch+1）→ A 迟到写入：必须被拒。"""
    fence_a = sessions_repo.begin_write(env.db, env.session)
    fence_b = sessions_repo.begin_write(env.db, env.session)  # B 接管锁
    sessions_repo.bump_state_version(env.db, env.session, fence=fence_b)
    env.db.commit()

    with pytest.raises(WriteFenceLostError) as exc:
        sessions_repo.bump_state_version(env.db, env.session, fence=fence_a)

    assert exc.value.code == "WRITE_FENCE_LOST"
    assert exc.value.retryable is True


def test_late_writer_cannot_write_progress_or_followup(env):
    """不只是版本号：进度、追问题目、回答状态一起拦（同一事务内回滚）。"""
    fence_a = sessions_repo.begin_write(env.db, env.session)
    fence_b = sessions_repo.begin_write(env.db, env.session)
    sessions_repo.touch(
        env.db, env.session, status="IN_PROGRESS", progress={"stage": "WAITING_ANSWER"}
    )
    sessions_repo.bump_state_version(env.db, env.session, fence=fence_b)
    env.db.commit()

    # A 迟到：没有做自检（比如它是在 begin_write 之前就拿到 session 的），
    # 直接 flush 了回答行与进度，最后一步的条件更新才把它拦住
    env.answer.processing_stage = "GRAPH_RUNNING"
    env.db.flush()
    sessions_repo.touch(env.db, env.session, progress={"stage": "FINISHED"})

    with pytest.raises(WriteFenceLostError):
        sessions_repo.bump_state_version(env.db, env.session, fence=fence_a)
    env.db.rollback()

    stored = _reload(env.db, env.session.id)
    assert stored.progress["stage"] == "WAITING_ANSWER"  # A 的进度没落库
    assert stored.state_version == 1
    row = answers_repo.get(env.db, env.answer.id)
    assert row.processing_stage is None  # A 的阶段写入也被回滚


def test_check_fence_fails_fast_when_already_lost(env):
    """自检要能提前炸：别等跑完模型、写完半张表再回滚。"""
    fence_a = sessions_repo.begin_write(env.db, env.session)
    sessions_repo.begin_write(env.db, env.session)  # 别人接管
    env.db.expire(env.session, ["write_epoch"])

    with pytest.raises(WriteFenceLostError):
        sessions_repo.check_fence(env.session, fence_a)


# ---------------------------------------------------------------- 版本冲突


def test_same_epoch_but_stale_version_is_version_conflict(env):
    """epoch 还是我的，但业务状态被推进过 → 报版本冲突，不是栅栏丢失。

    两个错误码必须分得开：前者要重新读状态，后者要重新举手（换锁）。
    """
    fence = sessions_repo.begin_write(env.db, env.session)
    # 同一代次内被别人推进（模拟同一持有者的重放/并发）
    env.db.execute(
        InterviewSession.__table__.update()
        .where(InterviewSession.__table__.c.id == env.session.id)
        .values(state_version=5)
    )
    env.db.commit()
    env.db.expire_all()

    with pytest.raises(VersionConflictError):
        sessions_repo.bump_state_version(env.db, env.session, expected=0, fence=fence)
