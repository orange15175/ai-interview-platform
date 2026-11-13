"""阶段 5 API 契约测试（真 PostgreSQL + 真 Redis + LLM_MOCK=1，不产生模型费用）。

覆盖开发说明 §10 / §13 阶段 5 验收项：
- 6 个接口全流程：创建(202) → 轮询 → 当前题 → 提交 → 追问 → 跳过 → finish 幂等 → records；
- 匿名访客隔离：别的访客（另一个 Cookie jar）读同一会话一律 404；
- CSRF：跨站 Origin 的写请求 403；
- 幂等与冲突：同 request_id 回放 / 换输入 REQUEST_CONFLICT / 同题换 id STALE_QUESTION；
- 恢复：删掉 checkpoint 后 GET current-question 自动重建到当前题；
- 准备孤儿：进程内任务登记丢失（模拟重启）→ 轮询发现并标记 PREPARATION_FAILED；
- 不泄题：对外 evaluation 剔除 rubric_results。

每个测试自建访客（独立 TestClient = 独立 Cookie jar），结束清理 PG 与 Redis。
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update as sa_update

from app.api import interviews as interviews_api
from app.infrastructure.checkpoint import get_checkpointer
from app.infrastructure.db import get_session_factory
from app.main import app
from app.models import (
    InterviewAnswer,
    InterviewMaterial,
    InterviewQuestion,
    InterviewSession,
)
from app.repositories import visitors as visitors_repo

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ---------------------------------------------------------------- 工具


def _resume_docx() -> bytes:
    """构造一份内容合法的简历 DOCX（校验要能真正打开文件）。"""
    doc = Document()
    doc.add_paragraph("工作经历：测试公司 后端开发 2023-2025。")
    doc.add_paragraph("负责订单系统重构，QPS 从 500 提升到 5000。")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _create_interview(client: TestClient, *, count: str = "2", resume=..., **extra_data):
    files = None
    if resume is ... or resume is not None:
        data = resume if isinstance(resume, bytes) else _resume_docx()
        files = {"resume": ("resume.docx", data, DOCX_MIME)}
    r = client.post(
        "/api/interviews",
        files=files,
        data={"question_count": count, **extra_data},
    )
    return r


def _cleanup(client: TestClient) -> None:
    """按访客彻底清理：回答 → 题目 → 材料 → 会话 → 访客 + Redis checkpoint。"""
    token = client.cookies.get("aiiv_visitor")
    if not token:
        return
    saver = get_checkpointer()
    db = get_session_factory()()
    try:
        visitor = visitors_repo.find_by_token(db, token)
        if visitor is None:
            return
        sids = list(db.scalars(
            select(InterviewSession.id).where(InterviewSession.owner_id == visitor.id)
        ))
        for sid in sids:
            db.execute(sa_delete(InterviewAnswer).where(InterviewAnswer.session_id == sid))
            db.execute(sa_delete(InterviewQuestion).where(InterviewQuestion.session_id == sid))
            db.execute(sa_delete(InterviewMaterial).where(InterviewMaterial.session_id == sid))
            try:
                saver.delete_thread(str(sid))
            except Exception:  # noqa: BLE001 —— 清理失败不阻断
                pass
        db.execute(sa_delete(InterviewSession).where(InterviewSession.owner_id == visitor.id))
        db.execute(sa_delete(type(visitor)).where(type(visitor).id == visitor.id))
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def client():
    c = TestClient(app)
    try:
        yield c
    finally:
        _cleanup(c)


@pytest.fixture()
def stranger():
    """第二个访客：独立 Cookie jar，用于越权验证。"""
    c = TestClient(app)
    try:
        yield c
    finally:
        _cleanup(c)


def _answer_body(question_id: str, *, request_id: str | None = None, answer: str | None = None,
                 skip: bool = False) -> dict:
    body: dict = {
        "question_id": question_id,
        "request_id": request_id or f"req-{uuid.uuid4()}",
    }
    if skip:
        body["submission_kind"] = "skip"
    else:
        body["answer"] = answer or "我做了技术取舍并给出量化结果：QPS 提升 10 倍。"
    return body


def _submit(client: TestClient, session_id: str, body: dict):
    return client.post(f"/api/interviews/{session_id}/answers", json=body)


# ---------------------------------------------------------------- 创建与全流程


def test_create_returns_202_and_prepares(client):
    r = _create_interview(client, count="2")
    assert r.status_code == 202
    data = r.json()
    sid = data["session_id"]
    assert data["status"] == "PREPARING"

    # TestClient 同步执行后台任务：此时准备应已完成
    view = client.get(f"/api/interviews/{sid}").json()
    assert view["status"] == "IN_PROGRESS", view
    assert view["finished"] is False
    assert view["current_question"] is not None
    # 题目投影不带评分要点
    assert "evaluation_rubric" not in view["current_question"]


def test_full_flow_until_finish_and_records(client):
    sid = _create_interview(client, count="2").json()["session_id"]

    current = client.get(f"/api/interviews/{sid}/current-question").json()
    assert current["recovered"] is False
    q = current["question"]
    assert q is not None

    answered: list[str] = []
    for _ in range(8):  # 2 主问题 × (1+2 追问) 上限
        r = _submit(client, sid, _answer_body(q["question_id"]))
        assert r.status_code == 200, r.text
        out = r.json()
        assert "rubric_results" not in (out["evaluation"] or {})
        answered.append(out["answer_id"])

        if out["finished"]:
            assert out["next_question"] is None
            break
        assert out["next_question"] is not None
        q = out["next_question"]
    else:
        pytest.fail("答题循环未在预期步数内结束")

    # finish 幂等：已结束的会话重复调用返回同一事实
    r1 = client.post(f"/api/interviews/{sid}/finish")
    assert r1.status_code == 200 and r1.json()["finished"] is True
    r2 = client.post(f"/api/interviews/{sid}/finish")
    assert r2.json()["status"] == r1.json()["status"]

    records = client.get(f"/api/interviews/{sid}/records").json()
    assert records["record_count"] == len(answered)
    assert records["record_count"] >= 2
    first = records["records"][0]
    assert first["main_index"] == 1
    assert "evaluation_rubric" not in first  # 记录同样不泄评分要点
    assert first["score"] is not None or first["skipped"] is True


def test_skip_is_a_valid_submission(client):
    sid = _create_interview(client, count="2").json()["session_id"]
    q = client.get(f"/api/interviews/{sid}/current-question").json()["question"]

    r = _submit(client, sid, _answer_body(q["question_id"], skip=True))
    assert r.status_code == 200
    out = r.json()
    assert out["evaluation"] is None  # 跳过不评分
    assert out["next_question"] is not None


# ---------------------------------------------------------------- 身份与安全


def test_error_response_still_sets_visitor_cookie(client):
    """回归：首次访客的第一个请求恰好失败（404/422）时，Cookie 也必须下发。

    踩过的坑：依赖里 response.set_cookie 设置的头会被异常处理器新建的
    JSONResponse 静默丢弃 → 之后每个请求都变成新访客，评分与归属全乱。
    """
    r = client.get("/api/interviews/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert "aiiv_visitor" in client.cookies

    token = client.cookies.get("aiiv_visitor")
    # 同一客户端第二次请求复用身份：不再创建新访客
    client.get("/api/interviews/00000000-0000-0000-0000-000000000000")
    assert client.cookies.get("aiiv_visitor") == token

    db = get_session_factory()()
    try:
        visitor = visitors_repo.find_by_token(db, token)
        assert visitor is not None
    finally:
        db.close()


def test_visitor_isolation(stranger, client):
    sid = _create_interview(client, count="2").json()["session_id"]
    for path in (
        f"/api/interviews/{sid}",
        f"/api/interviews/{sid}/current-question",
        f"/api/interviews/{sid}/records",
    ):
        r = stranger.get(path)
        assert r.status_code == 404
        assert r.json()["code"] == "SESSION_NOT_FOUND"

    r = stranger.post(f"/api/interviews/{sid}/finish")
    assert r.status_code == 404
    r = stranger.post(
        f"/api/interviews/{sid}/answers",
        json=_answer_body("00000000-0000-0000-0000-000000000001"),
    )
    assert r.status_code == 404


def test_csrf_origin_rejected(client):
    sid = _create_interview(client, count="2").json()["session_id"]
    q = client.get(f"/api/interviews/{sid}/current-question").json()["question"]
    r = _submit(client, sid, _answer_body(q["question_id"]))
    assert r.status_code == 200  # 同源（无 Origin 头）放行

    r2 = client.post(
        f"/api/interviews/{sid}/finish",
        headers={"Origin": "http://evil.example"},
    )
    assert r2.status_code == 403
    assert r2.json()["code"] == "CSRF_REJECTED"


# ---------------------------------------------------------------- 幂等与冲突


def test_same_request_id_replays_same_answer(client):
    sid = _create_interview(client, count="2").json()["session_id"]
    q = client.get(f"/api/interviews/{sid}/current-question").json()["question"]
    body = _answer_body(q["question_id"])

    first = _submit(client, sid, body).json()
    second = _submit(client, sid, body).json()

    assert second["replayed"] is True
    assert second["answer_id"] == first["answer_id"]
    assert second["state_version"] == first["state_version"]


def test_same_request_id_different_input_conflict(client):
    sid = _create_interview(client, count="2").json()["session_id"]
    q = client.get(f"/api/interviews/{sid}/current-question").json()["question"]
    body = _answer_body(q["question_id"], answer="第一个回答版本")
    _submit(client, sid, body)

    r = _submit(client, sid, {**body, "answer": "偷偷改口的内容"})
    assert r.status_code == 409
    assert r.json()["code"] == "REQUEST_CONFLICT"


def test_same_question_new_request_id_stale(client):
    sid = _create_interview(client, count="2").json()["session_id"]
    q = client.get(f"/api/interviews/{sid}/current-question").json()["question"]
    _submit(client, sid, _answer_body(q["question_id"]))

    r = _submit(client, sid, _answer_body(q["question_id"]))  # 新 request_id 交旧题
    assert r.status_code == 409
    assert r.json()["code"] == "STALE_QUESTION"


def test_empty_answer_rejected_without_advancing(client):
    sid = _create_interview(client, count="2").json()["session_id"]
    q = client.get(f"/api/interviews/{sid}/current-question").json()["question"]
    r = _submit(client, sid, _answer_body(q["question_id"], answer="   "))
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"

    # 没有推进：当前题不变，还能正常作答
    r2 = _submit(client, sid, _answer_body(q["question_id"]))
    assert r2.status_code == 200


# ---------------------------------------------------------------- 恢复


def test_current_question_recovers_after_checkpoint_loss(client):
    sid = _create_interview(client, count="2").json()["session_id"]
    q = client.get(f"/api/interviews/{sid}/current-question").json()["question"]
    out = _submit(client, sid, _answer_body(q["question_id"])).json()
    next_q = out["next_question"]
    assert next_q is not None

    # 模拟 Redis checkpoint 丢失
    get_checkpointer().delete_thread(sid)

    view = client.get(f"/api/interviews/{sid}").json()
    assert view["recoverable"] is False

    r = client.get(f"/api/interviews/{sid}/current-question")
    assert r.status_code == 200
    data = r.json()
    assert data["recovered"] is True
    assert data["question"]["question_id"] == next_q["question_id"]

    # 恢复后还能继续答题
    r2 = _submit(client, sid, _answer_body(data["question"]["question_id"]))
    assert r2.status_code == 200


# ---------------------------------------------------------------- 准备阶段边界


def test_orphan_preparing_session_marked_failed(client, monkeypatch):
    """重启留下的 PREPARING 会话，轮询必须收口成失败，不能永远转圈。

    判定口径是"创建时间早于本进程启动"——这比"不在我的登记表里"可靠：
    多 worker 部署时，别的 worker 正在准备的会话不在本进程登记表里，
    直接判死会把它误杀（该任务随后发现状态已变，静默跳过 → 永久失败）。
    """
    monkeypatch.setattr(interviews_api, "_run_preparation", lambda **kwargs: None)
    sid = _create_interview(client, count="2").json()["session_id"]

    # 还在登记表里：如实返回 PREPARING
    assert client.get(f"/api/interviews/{sid}").json()["status"] == "PREPARING"

    # 模拟真实重启：登记表空了，且这份会话是上一个进程创建的
    interviews_api._preparing.discard(sid)
    db = get_session_factory()()
    try:
        db.execute(
            sa_update(InterviewSession)
            .where(InterviewSession.id == uuid.UUID(sid))
            .values(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        db.commit()
    finally:
        db.close()

    view = client.get(f"/api/interviews/{sid}").json()
    assert view["status"] == "PREPARATION_FAILED"
    assert "重新上传" in view["sanitized_error"]

    # 终态：当前题接口返回 finished 事实
    cq = client.get(f"/api/interviews/{sid}/current-question").json()
    assert cq["finished"] is True


def test_preparing_session_is_not_killed_while_other_worker_may_run_it(client, monkeypatch):
    """本进程创建、登记表却没有的会话：超时前不能判孤儿（可能是别的 worker 在跑）。"""
    monkeypatch.setattr(interviews_api, "_run_preparation", lambda **kwargs: None)
    sid = _create_interview(client, count="2").json()["session_id"]

    # 模拟"登记表在别的 worker 上"：本进程登记表里没有它
    interviews_api._preparing.discard(sid)

    # 刚创建、还在准备超时窗口内 → 必须继续如实返回 PREPARING
    assert client.get(f"/api/interviews/{sid}").json()["status"] == "PREPARING"

    # 超过准备超时（任务多半真的丢了）→ 才允许判孤儿
    monkeypatch.setattr(interviews_api, "_PREPARATION_TIMEOUT_SECONDS", -1)
    assert client.get(f"/api/interviews/{sid}").json()["status"] == "PREPARATION_FAILED"


def test_oversized_upload_rejected_at_once(client):
    """超限文件必须当场拒绝：不能先整个读进内存再报错（那样能被内存炸弹打挂）。"""
    big = b"\x00" * (11 * 1024 * 1024)  # 默认上限 10 MiB
    r = client.post(
        "/api/interviews",
        files={"resume": ("resume.docx", big, DOCX_MIME)},
        data={"question_count": "2"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "FILE_TOO_LARGE"
    assert "上限" in body["message"]


def test_invalid_file_rejected_immediately(client):
    # 伪造扩展名：内容不是 docx
    r = client.post(
        "/api/interviews",
        files={"resume": ("resume.docx", b"not a docx at all", DOCX_MIME)},
        data={"question_count": "2"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_FILE"

    # 缺简历
    r2 = client.post("/api/interviews", data={"question_count": "2"})
    assert r2.status_code == 422
    assert r2.json()["code"] == "INVALID_REQUEST"

    # 题数越界
    r3 = _create_interview(client, count="99")
    assert r3.status_code == 422


def test_jd_file_and_text_mutex(client):
    r = _create_interview(
        client,
        count="2",
        jd_text="负责高并发后端服务的设计与优化，要求熟悉分布式系统。",
    )
    assert r.status_code == 202
    sid = r.json()["session_id"]

    # JD 文件 + JD 文字同时给 → 当场拒绝
    r2 = client.post(
        "/api/interviews",
        files={
            "resume": ("resume.docx", _resume_docx(), DOCX_MIME),
            "jd": ("jd.docx", _resume_docx(), DOCX_MIME),
        },
        data={
            "question_count": "2",
            "jd_text": "负责高并发后端服务的设计与优化，要求熟悉分布式系统。",
        },
    )
    assert r2.status_code == 422
