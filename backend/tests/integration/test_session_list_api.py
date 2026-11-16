"""面试记录列表接口（GET /api/interviews）。

和 test_report_api.py 同一套做法：get_db 覆写成 SQLite 内存库，直接打 HTTP 层。
重点只有两个：**归属隔离**和**平均分口径**（NULL 分不进分母）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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
        yield factory
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def _session(factory, visitor_id, *, status="FINISHED", scores=(80, 60), minutes_ago=0):
    """造一场会话：scores 里每项对应一道题，None 表示有回答但没有分。"""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
        s = InterviewSession(
            id=uuid.uuid4(), owner_id=visitor_id, status=status,
            question_count=len(scores), state_version=1,
            progress={"answered_count": sum(1 for x in scores if x is not None)},
            last_activity_at=now, created_at=now - timedelta(minutes=30),
        )
        db.add(s)
        db.flush()
        for i, score in enumerate(scores, start=1):
            q = InterviewQuestion(
                id=uuid.uuid4(), session_id=s.id, kind="main", main_index=i,
                follow_up_index=0, content=f"q{i}", category="项目经验",
                difficulty="medium", evaluation_rubric=[{"point": "p", "weight": 1.0}],
            )
            db.add(q)
            db.flush()
            db.add(InterviewAnswer(
                id=uuid.uuid4(), session_id=s.id, question_id=q.id,
                request_id=f"req-{i}", input_hash="h", answer_content="a",
                submission_kind="answer", processing_status="COMPLETED",
                score=score,
                evaluation={"score": score, "feedback": "f"} if score is not None else None,
            ))
        db.commit()
        return str(s.id)
    finally:
        db.close()


def _client(token: str) -> TestClient:
    return TestClient(app, cookies={get_settings().visitor_cookie_name: token})


def _visitor(factory):
    db = factory()
    try:
        visitor, token = visitors_repo.create(db)
        db.commit()
        return visitor.id, token
    finally:
        db.close()


def test_lists_only_current_identity(api):
    me, token = _visitor(api)
    other, _ = _visitor(api)
    mine = _session(api, me)
    _session(api, other)

    body = _client(token).get("/api/interviews").json()
    assert [item["session_id"] for item in body] == [mine]


def test_ordered_by_last_activity(api):
    me, token = _visitor(api)
    newer = _session(api, me, minutes_ago=1)
    older = _session(api, me, minutes_ago=60)

    body = _client(token).get("/api/interviews").json()
    assert [i["session_id"] for i in body] == [newer, older]


def test_average_ignores_missing_scores(api):
    """跳过/失败的题 score 是 NULL，不能当成 0 拉低平均分。"""
    me, token = _visitor(api)
    _session(api, me, scores=(90, None, 30))

    item = _client(token).get("/api/interviews").json()[0]
    assert item["overall_score"] == 60  # (90+30)/2，不是 (90+0+30)/3=40
    assert item["answered_count"] == 2


def test_session_without_any_score_has_null_overall(api):
    me, token = _visitor(api)
    _session(api, me, scores=(None, None))

    item = _client(token).get("/api/interviews").json()[0]
    assert item["overall_score"] is None


def test_status_flags(api):
    me, token = _visitor(api)
    _session(api, me, status="IN_PROGRESS", minutes_ago=1)
    _session(api, me, status="PREPARING", minutes_ago=2)
    _session(api, me, status="CANCELLED", minutes_ago=3)

    body = {i["status"]: i for i in _client(token).get("/api/interviews").json()}
    assert body["IN_PROGRESS"]["resumable"] is True
    assert body["IN_PROGRESS"]["finished"] is False
    assert body["PREPARING"]["resumable"] is True
    assert body["CANCELLED"]["finished"] is True
    assert body["CANCELLED"]["resumable"] is False


def test_limit_caps_result(api):
    me, token = _visitor(api)
    for i in range(5):
        _session(api, me, minutes_ago=i)

    assert len(_client(token).get("/api/interviews?limit=2").json()) == 2


def test_list_is_read_only(api):
    """列表不能顺带改状态：拉两次，状态与版本号都不变。"""
    me, token = _visitor(api)
    sid = _session(api, me, status="IN_PROGRESS")

    client = _client(token)
    client.get("/api/interviews")
    body = client.get("/api/interviews").json()
    assert body[0]["session_id"] == sid
    assert body[0]["status"] == "IN_PROGRESS"
