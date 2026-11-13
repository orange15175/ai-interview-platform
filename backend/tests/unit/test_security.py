"""匿名访客凭证的安全属性（开发说明 §11）。"""

from __future__ import annotations

import uuid

from app.core.security import (
    TOKEN_HASH_LEN,
    hash_visitor_token,
    looks_like_token,
    new_visitor_token,
    token_matches,
)


def test_token_is_unpredictable_and_unique():
    tokens = {new_visitor_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(looks_like_token(t) for t in tokens)
    # uuid4 字符串化是常见错误做法：会带连字符且熵分布不同，这里明确排除
    assert not any(isinstance(t, uuid.UUID) for t in tokens)


def test_hash_is_stable_and_fixed_length():
    token = new_visitor_token()
    digests = {hash_visitor_token(token) for _ in range(3)}
    assert len(digests) == 1
    assert len(digests.pop()) == TOKEN_HASH_LEN
    # 明文绝不等于哈希，且哈希里不含明文
    assert token not in hash_visitor_token(token)


def test_token_matches_is_constant_time_compare():
    token = new_visitor_token()
    assert token_matches(token, hash_visitor_token(token)) is True
    assert token_matches(token, hash_visitor_token(new_visitor_token())) is False
    assert token_matches("", hash_visitor_token(token)) is False


def test_looks_like_token_rejects_garbage():
    assert looks_like_token(None) is False
    assert looks_like_token("") is False
    assert looks_like_token("short") is False
    assert looks_like_token("x" * 43) is True
    assert looks_like_token("has space in the middle of token") is False
    assert looks_like_token("y" * 200) is False  # 超长直接拒绝，别拿去查库
