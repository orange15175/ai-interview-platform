"""账号接口的契约测试（真 PostgreSQL + 真 Redis，模型不参与）。

覆盖：
- 注册 → 直接登录（Cookie 生效）→ /me 能拿到用户；
- 账号唯一：大小写不同也算同一个账号，第二次注册 409；
- 登录：密码错 / 账号不存在都返回同一句 401（不是账号枚举器）；
- 登出后 /me 变 null；伪造 Cookie 当未登录；
- 登录用户跨 Cookie 访问自己创建的会话；未登录访客仍然拿不到（404）；
- 密码错误信息里绝不出现明文密码或哈希。

每个测试自建账号，结束时删除（含其会话），不留脏数据。
"""

from __future__ import annotations

import io
import uuid

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.infrastructure.db import get_session_factory
from app.main import app
from app.models import InterviewSession, User
from app.repositories import visitors as visitors_repo

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PASSWORD = "Passw0rd!"


def _account() -> str:
    return f"user_{uuid.uuid4().hex[:10]}"


def _resume_docx() -> bytes:
    doc = Document()
    doc.add_paragraph("工作经历：测试公司 后端开发 2023-2025。")
    doc.add_paragraph("负责订单系统重构，QPS 从 500 提升到 5000。")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _cleanup(client: TestClient) -> None:
    """按访客 + 账号双向清理（含会话级联删除）。"""
    token = client.cookies.get("aiiv_visitor")
    db = get_session_factory()()
    try:
        accounts = {r for r in db.scalars(select(User.account))}
        for account in accounts:
            if account.startswith("user_"):
                db.execute(sa_delete(User).where(User.account == account))
        from app.models import AnonymousVisitor

        if token:
            visitor = visitors_repo.find_by_token(db, token)
            if visitor is not None:
                sids = list(db.scalars(
                    select(InterviewSession.id).where(InterviewSession.owner_id == visitor.id)
                ))
                for sid in sids:
                    db.execute(sa_delete(InterviewSession).where(InterviewSession.id == sid))
                db.execute(sa_delete(AnonymousVisitor).where(AnonymousVisitor.id == visitor.id))
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


# ---------------------------------------------------------------- 注册 / 登录


def test_register_logs_you_in(client):
    account = _account()
    r = client.post("/api/auth/register", json={"account": account, "password": PASSWORD})
    assert r.status_code == 201, r.text
    assert r.json()["user"]["account"] == account
    assert "aiiv_user" in client.cookies, "注册后必须直接是登录态"

    me = client.get("/api/auth/me").json()
    assert me["user"]["account"] == account
    # 敏感字段一律不外泄
    assert "password" not in r.text and "pbkdf2" not in r.text


def test_account_is_unique_case_insensitive(client):
    account = _account()
    assert client.post(
        "/api/auth/register", json={"account": account.upper(), "password": PASSWORD}
    ).status_code == 201
    dup = client.post("/api/auth/register", json={"account": account, "password": PASSWORD})
    assert dup.status_code == 409
    assert dup.json()["code"] == "ACCOUNT_TAKEN"


def test_login_and_wrong_password(client):
    account = _account()
    client.post("/api/auth/register", json={"account": account, "password": PASSWORD})
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["user"] is None

    bad = client.post("/api/auth/login", json={"account": account, "password": "wrong-pass"})
    assert bad.status_code == 401 and bad.json()["code"] == "INVALID_CREDENTIALS"
    assert "aiiv_user" not in client.cookies

    ok = client.post("/api/auth/login", json={"account": account, "password": PASSWORD})
    assert ok.status_code == 200
    assert client.get("/api/auth/me").json()["user"]["account"] == account


def test_unknown_account_same_error_as_wrong_password(client):
    """不区分"账号不存在"与"密码错"，否则就是账号枚举接口。"""
    r = client.post("/api/auth/login", json={"account": _account(), "password": PASSWORD})
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"
    assert r.json()["message"] == "账号或密码不正确"


def test_forged_cookie_is_guest(client):
    account = _account()
    client.post("/api/auth/register", json={"account": account, "password": PASSWORD})
    token = client.cookies.get("aiiv_user")
    forged = token[:-3] + "abc"  # 改掉签名最后 3 位

    # 用全新的客户端带上伪造 Cookie（httpx 的 CookieJar 覆盖同名 Cookie 不可靠）
    other = TestClient(app, cookies={"aiiv_user": forged})
    try:
        assert other.get("/api/auth/me").json()["user"] is None
    finally:
        _cleanup(other)


@pytest.mark.parametrize(
    "payload",
    [
        {"account": "ab", "password": PASSWORD},             # 账号太短
        {"account": "has space", "password": PASSWORD},      # 非法字符
        {"account": "ok_account", "password": "12345678"},   # 纯数字
        {"account": "ok_account", "password": "short"},      # 太短
    ],
)
def test_invalid_register_payload_rejected(client, payload):
    r = client.post("/api/auth/register", json=payload)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- 会话归属


def test_logged_in_session_is_accessible_from_another_cookie(client):
    """登录后创建面试 → 换一个"浏览器"重新登录 → 还能读到同一场面试。"""
    account = _account()
    client.post("/api/auth/register", json={"account": account, "password": PASSWORD})

    r = client.post(
        "/api/interviews",
        files={"resume": ("resume.docx", _resume_docx(), DOCX_MIME)},
        data={"question_count": "2"},
    )
    assert r.status_code == 202, r.text
    sid = r.json()["session_id"]

    # 另一个浏览器：全新 Cookie jar，先登录再访问
    other = TestClient(app)
    try:
        assert other.get(f"/api/interviews/{sid}").status_code == 404  # 未登录看不到
        assert other.post(
            "/api/auth/login", json={"account": account, "password": PASSWORD}
        ).status_code == 200
        assert other.get(f"/api/interviews/{sid}").status_code == 200
    finally:
        _cleanup(other)


def test_anonymous_flow_still_works(client):
    """没登录也能正常创建面试（旧流程不能被账号体系绑死）。"""
    r = client.post(
        "/api/interviews",
        files={"resume": ("resume.docx", _resume_docx(), DOCX_MIME)},
        data={"question_count": "2"},
    )
    assert r.status_code == 202
    db = get_session_factory()()
    try:
        session = db.get(InterviewSession, uuid.UUID(r.json()["session_id"]))
        assert session is not None and session.user_id is None
    finally:
        db.close()
