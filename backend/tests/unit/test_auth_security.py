"""账号密码与登录令牌的单元测试（不碰数据库）。

要点：
- 同一密码两次哈希必须不同（盐随机）；
- 校验对"改一个字符""格式损坏""空值"都必须安全返回 False，不能抛错；
- 登录令牌必须能挡住篡改 uid、篡改过期时间、换密钥、过期四种伪造。
"""

from __future__ import annotations

import uuid

import pytest

from app.core.security import (
    PASSWORD_ITERATIONS,
    hash_password,
    issue_login_token,
    parse_login_token,
    verify_password,
)

SECRET = "unit-test-secret"


# ---------------------------------------------------------------- 密码


def test_same_password_hashes_differ():
    a = hash_password("Passw0rd!")
    b = hash_password("Passw0rd!")
    assert a != b, "盐必须是随机的，否则同密码同哈希，撞库后果放大"
    assert a.startswith("pbkdf2_sha256$")
    assert verify_password("Passw0rd!", a) and verify_password("Passw0rd!", b)


def test_hash_never_contains_plaintext():
    raw = "SuperSecret123"
    stored = hash_password(raw)
    assert raw not in stored
    assert str(PASSWORD_ITERATIONS) in stored  # 迭代次数要记录下来，便于未来升级


@pytest.mark.parametrize(
    "stored",
    ["", "garbage", "pbkdf2_sha256$abc$def$ghi", "argon2$x$y$z", "pbkdf2_sha256$600000$zz$zz"],
)
def test_verify_handles_broken_hash(stored):
    assert verify_password("Passw0rd!", stored) is False


def test_verify_wrong_password_and_empty():
    stored = hash_password("Passw0rd!")
    assert verify_password("Passw0rd", stored) is False
    assert verify_password("", stored) is False
    assert verify_password("Passw0rd!", "") is False


def test_hash_password_rejects_absurd_length():
    with pytest.raises(ValueError):
        hash_password("x" * 500)


# ---------------------------------------------------------------- 登录令牌


def test_token_roundtrip():
    uid = uuid.uuid4()
    token, max_age = issue_login_token(uid, secret=SECRET, ttl_hours=2)
    assert max_age == 7200
    assert parse_login_token(token, secret=SECRET) == uid


def test_token_rejects_tampered_uid():
    uid = uuid.uuid4()
    token, _ = issue_login_token(uid, secret=SECRET, ttl_hours=2)
    other = str(uuid.uuid4())
    forged = other + token[token.index("."):]
    assert parse_login_token(forged, secret=SECRET) is None


def test_token_rejects_extended_expiry():
    """把过期时间往后改必须验签失败——否则用户自己就能续命。"""
    uid = uuid.uuid4()
    token, _ = issue_login_token(uid, secret=SECRET, ttl_hours=1)
    parts = token.split(".")
    forged = f"{parts[0]}.{int(parts[1]) + 86400}.{parts[2]}"
    assert parse_login_token(forged, secret=SECRET) is None


def test_token_rejects_other_secret():
    token, _ = issue_login_token(uuid.uuid4(), secret=SECRET, ttl_hours=1)
    assert parse_login_token(token, secret="another-secret") is None


def test_token_expired(monkeypatch):
    uid = uuid.uuid4()
    token, _ = issue_login_token(uid, secret=SECRET, ttl_hours=1)
    import app.core.security as security

    real_time = security.time.time
    monkeypatch.setattr(security.time, "time", lambda: real_time() + 7200)
    assert parse_login_token(token, secret=SECRET) is None


@pytest.mark.parametrize("bad", [None, "", "abc", "a.b", "a.b.c.d", "..", "x.y.z"])
def test_token_garbage_is_not_logged_in(bad):
    assert parse_login_token(bad, secret=SECRET) is None


def test_issue_requires_secret():
    with pytest.raises(ValueError):
        issue_login_token(uuid.uuid4(), secret="", ttl_hours=1)
