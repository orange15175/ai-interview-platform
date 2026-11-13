"""真实 PostgreSQL 上的仓储行为（SQLite 测不出的那部分）。

为什么单独测：SQLite 上 JSON 只是 TEXT，唯一约束的报错信息、行锁语法、
`UPDATE ... WHERE version = ?` 的行为都与 PG 有差异。阶段 4 的所有结论
最终要在 PG 上成立，所以这里直接打真库——但在一个事务里做，最后整体回滚，
不留脏数据。

    docker compose up -d     # 5433 的 PG16
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.errors import VersionConflictError
from app.models import Base
from app.repositories import answers as answers_repo
from app.repositories import questions as questions_repo
from app.repositories import sessions as sessions_repo
from app.repositories import visitors as visitors_repo
from app.schemas.questions import GeneratedQuestion, RubricPoint


def _pg_available() -> bool:
    try:
        engine = create_engine(
            get_settings().database_url, connect_args={"connect_timeout": 3}
        )
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="PostgreSQL 未启动，跳过")


@pytest.fixture()
def pg_db():
    engine = create_engine(get_settings().database_url, connect_args={"connect_timeout": 3})
    conn = engine.connect()
    outer = conn.begin()
    factory = sessionmaker(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    db = factory()
    try:
        yield db
    finally:
        db.close()
        outer.rollback()   # 测试数据一律不留在真库里
        conn.close()
        engine.dispose()


def _q(i: int) -> GeneratedQuestion:
    return GeneratedQuestion(
        main_index=i,
        category="项目经验",
        difficulty="medium",
        content=f"第 {i} 题：请说明你在项目中做过的关键技术取舍与量化结果。",
        material_evidence_ids=["ev-1"],
        evaluation_rubric=[RubricPoint(point="讲清权衡", weight=1.0)],
    )


def test_jsonb_roundtrip_keeps_structure(pg_db):
    visitor, _ = visitors_repo.get_or_create(pg_db, None)
    session = sessions_repo.create(pg_db, owner_id=visitor.id)
    pg_db.commit()
    q = questions_repo.insert_main_questions(pg_db, session.id, [_q(1)])[0]
    pg_db.commit()

    nested = {
        "score": 82,
        "feedback": "结构清晰",
        "rubric_results": [{"point": "讲清权衡", "met": "yes", "note": "有对比"}],
        "dimension_scores": {"correctness": 80, "logic": 85},
    }
    row = answers_repo.create(
        pg_db, session_id=session.id, question_id=q.id, request_id="req-pg",
        input_hash="h", submission_kind="answer", answer_content="回答",
    )
    answers_repo.save_evaluation(pg_db, row, evaluation=nested, score=82)
    pg_db.commit()

    reloaded = answers_repo.get_by_request(pg_db, session.id, "req-pg")
    assert reloaded.evaluation == nested  # JSONB 原样取回，嵌套结构不丢
    assert reloaded.score == 82


def test_unique_constraint_names_are_stable_on_pg(pg_db):
    visitor, _ = visitors_repo.get_or_create(pg_db, None)
    session = sessions_repo.create(pg_db, owner_id=visitor.id)
    pg_db.commit()
    rows = questions_repo.insert_main_questions(pg_db, session.id, [_q(1), _q(2)])
    pg_db.commit()
    answers_repo.create(
        pg_db, session_id=session.id, question_id=rows[0].id, request_id="dup",
        input_hash="h", submission_kind="answer", answer_content="a",
    )
    pg_db.commit()

    with pytest.raises(IntegrityError) as exc:
        answers_repo.create(
            pg_db, session_id=session.id, question_id=rows[1].id, request_id="dup",
            input_hash="h2", submission_kind="answer", answer_content="b",
        )
    assert "uq_answers_session_request" in str(exc.value)
    pg_db.rollback()


def test_cas_update_works_on_pg(pg_db):
    visitor, _ = visitors_repo.get_or_create(pg_db, None)
    session = sessions_repo.create(pg_db, owner_id=visitor.id)
    pg_db.commit()

    assert sessions_repo.bump_state_version(pg_db, session) == 1
    sessions_repo.lock_row(pg_db, session)   # PG 支持 FOR UPDATE
    with pytest.raises(VersionConflictError):
        sessions_repo.bump_state_version(pg_db, session, expected=0)


def test_question_index_uniqueness_on_pg(pg_db):
    visitor, _ = visitors_repo.get_or_create(pg_db, None)
    session = sessions_repo.create(pg_db, owner_id=visitor.id)
    pg_db.commit()
    main = questions_repo.insert_main_questions(pg_db, session.id, [_q(1)])[0]
    pg_db.commit()

    from app.models import InterviewQuestion

    dup = InterviewQuestion(
        id=uuid.uuid4(), session_id=session.id, kind="main", main_index=1,
        follow_up_index=0, content="重复的题", category="c", difficulty="easy",
        evaluation_rubric=[], material_evidence_ids=[],
    )
    pg_db.add(dup)
    with pytest.raises(IntegrityError) as exc:
        pg_db.flush()
    assert "uq_questions_session_index" in str(exc.value)
    pg_db.rollback()
    assert Base.metadata is not None
