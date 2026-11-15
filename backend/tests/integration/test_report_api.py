"""复盘报告接口（§3.1 / §3.7）。

不依赖 PostgreSQL/Redis：把 get_db 覆写成 SQLite 内存库，直接调 HTTP 层。
这样即使本机没起容器，也能验证"接口能跑、权限能挡、不调模型、不泄题"。

真实库上的统计口径由 tests/unit/test_reporting.py 覆盖；这里只测接线。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base, InterviewAnswer, InterviewQuestion, InterviewSession
from app.repositories import visitors as visitors_repo


@pytest.fixture()
def api():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        yield type("Api", (), {"factory": factory, "engine": engine})()
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def _make_session(factory, *, status="FINISHED"):
    """造一场"两道主问题 + 一道追问、全部答完"的会话，返回 (cookie, session_id)。"""
    db = factory()
    try:
        visitor, token = visitors_repo.create(db)
        session = InterviewSession(
            id=uuid.uuid4(), owner_id=visitor.id, status=status,
            question_count=2, state_version=3,
        )
        db.add(session)
        db.flush()

        q1 = InterviewQuestion(
            id=uuid.uuid4(), session_id=session.id, kind="main", main_index=1,
            follow_up_index=0, content="请讲一个你做过的技术决策。",
            category="项目经验", difficulty="medium",
            evaluation_rubric=[{"point": "内部要点", "weight": 1.0}],
        )
        q2 = InterviewQuestion(
            id=uuid.uuid4(), session_id=session.id, kind="follow_up", main_index=1,
            follow_up_index=1, parent_question_id=q1.id,
            content="那次取舍中你放弃了什么？", category="项目经验", difficulty="medium",
            evaluation_rubric=[{"point": "内部要点", "weight": 1.0}],
        )
        q3 = InterviewQuestion(
            id=uuid.uuid4(), session_id=session.id, kind="main", main_index=2,
            follow_up_index=0, content="说说你对缓存一致性的理解。",
            category="", difficulty="hard",
            evaluation_rubric=[{"point": "内部要点", "weight": 1.0}],
        )
        db.add_all([q1, q2, q3])

        def answer(question, score, *, dims, missing, content):
            db.add(InterviewAnswer(
                id=uuid.uuid4(), session_id=session.id, question_id=question.id,
                request_id=f"req-{question.main_index}-{question.follow_up_index}",
                input_hash="h", answer_content=content, submission_kind="answer",
                processing_status="COMPLETED", score=score,
                evaluation={
                    "score": score, "feedback": "结构清晰。",
                    "dimension_scores": dims, "missing_points": missing,
                    "rubric_results": [{"point": "内部要点", "met": "yes"}],
                },
                rule_decision={"decision": "stop", "reason_code": "SCORE_OK"},
                response_snapshot={"ok": True},
            ))

        answer(q1, 88, dims={"correctness": 90, "depth": 85},
               missing=["缺少量化结果"], content="我做了一个取舍，QPS 提升 10 倍。")
        answer(q2, 70, dims={"correctness": 70, "depth": 60},
               missing=["缺少量化结果", "没说清代价"], content="放弃了强一致性。")
        answer(q3, 60, dims={"correctness": 55, "depth": 50},
               missing=["概念有偏差"], content="缓存一致性要靠失效策略。")
        db.commit()
        return token, str(session.id)
    finally:
        db.close()


def _client(token: str) -> TestClient:
    return TestClient(app, cookies={get_settings().visitor_cookie_name: token})


# ---------------------------------------------------------------- 正常返回


def test_report_returns_aggregates_without_rubric(api):
    token, sid = _make_session(api.factory)
    r = _client(token).get(f"/api/interviews/{sid}/report")

    assert r.status_code == 200, r.text
    body = r.json()

    assert body["session_id"] == sid
    assert body["status"] == "FINISHED"
    assert body["is_final"] is True

    summary = body["summary"]
    assert summary["question_count"] == 3
    assert summary["completed_count"] == 3
    assert summary["overall_score"] == round((88 + 70 + 60) / 3)
    assert summary["score_coverage"] == 1.0
    assert summary["followup_count"] == 1

    # 五维：只统计有数值的记录
    assert body["dimension_scores"]["correctness"]["sample_count"] == 3
    # 类别：空类别归"未分类"
    categories = {c["category"] for c in body["category_summary"]}
    assert "未分类" in categories and "项目经验" in categories
    # 薄弱点按出现次数排序
    assert body["weak_points"][0]["point"] == "缺少量化结果"
    assert body["weak_points"][0]["count"] == 2
    # 证据：最高分在前、最低分在后
    assert body["evidence"][0]["score"] == 88
    assert body["evidence"][-1]["score"] == 60
    # 不泄题
    assert "rubric" not in r.text


def test_report_does_not_call_any_model(api, monkeypatch):
    """报告必须是纯读：生成过程一次模型调用都不该发生。"""
    import app.agents.adapters as adapters

    def boom(*args, **kwargs):
        raise AssertionError("报告不应调用模型")

    monkeypatch.setattr(adapters, "get_text_adapter", boom)
    monkeypatch.setattr(adapters, "get_vision_adapter", boom)

    token, sid = _make_session(api.factory)
    r = _client(token).get(f"/api/interviews/{sid}/report")
    assert r.status_code == 200
    assert r.json()["summary"]["overall_score"] is not None


# ---------------------------------------------------------------- 权限与边界


def test_report_rejects_other_visitor(api):
    _, sid = _make_session(api.factory)

    db = api.factory()
    try:
        _, other_token = visitors_repo.create(db)
        db.commit()
    finally:
        db.close()

    r = _client(other_token).get(f"/api/interviews/{sid}/report")
    assert r.status_code == 404
    assert r.json()["code"] == "SESSION_NOT_FOUND"


def test_report_rejects_preparing_session(api):
    token, sid = _make_session(api.factory, status="PREPARING")
    r = _client(token).get(f"/api/interviews/{sid}/report")
    assert r.status_code == 409
    assert r.json()["code"] == "PREPARING"


def test_report_without_any_score_returns_null_overall(api):
    db = api.factory()
    try:
        visitor, token = visitors_repo.create(db)
        session = InterviewSession(
            id=uuid.uuid4(), owner_id=visitor.id, status="CANCELLED",
            question_count=1, state_version=1,
        )
        db.add(session)
        db.commit()
        sid = str(session.id)
    finally:
        db.close()

    r = _client(token).get(f"/api/interviews/{sid}/report")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["overall_score"] is None  # 不是 0
    assert body["summary"]["score_coverage"] == 0.0
    assert body["notice"] and "没有足够有效评分数据" in body["notice"]
