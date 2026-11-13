"""匿名访客凭证（开发说明 §1.1 第 3 条 / §11）。

设计要点，别改错：
1. 明文 token 只存在于浏览器的 HttpOnly Cookie 里，数据库只存 SHA-256 哈希。
   拖库者拿到哈希也无法伪造 Cookie（SHA-256 单向；token 本身是 32 字节随机，
   不需要加盐也不能用快速哈希以外的方案——它本来就是高熵随机串，不是密码）。
2. token 必须不可预测（secrets，不是 uuid4 的字符串化——uuid4 只有 122 位且
   习惯上会暴露时间/机器信息，随机串更干净）。
3. Cookie 属性：HttpOnly（JS 读不到，防 XSS 偷身份）、SameSite=Lax（跨站写请求
   不带 Cookie，配合 Origin 校验防 CSRF）、生产 HTTPS 下 Secure。
4. 清理 Cookie = 丢失身份。系统不提供"按手机号找回"，也不提供全局记录列表
   （§11：UUID 不等于访问权限）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# 32 字节随机 → token_urlsafe 后约 43 个字符，远超暴力枚举可能
TOKEN_BYTES = 32
# 与 models/tables.py 的 token_hash 字段长度（String(64)）一致：sha256 hex 正好 64
TOKEN_HASH_LEN = 64


def new_visitor_token() -> str:
    """生成一个新的访客明文 token（只在 Set-Cookie 时出现一次）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_visitor_token(token: str) -> str:
    """明文 token → 入库用的哈希。入参为空时抛错，避免把空串哈希入库。"""
    if not token:
        raise ValueError("访客 token 为空")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, token_hash: str) -> bool:
    """恒定时间比较；用于需要显式核对的场景（正常查询直接用哈希做 where 条件）。"""
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_visitor_token(token), token_hash)


def looks_like_token(token: str | None) -> bool:
    """粗筛客户端传来的 Cookie 值，挡掉明显垃圾（正常 token 43 字符、无空白）。

    这不是安全边界（真正的边界是哈希查库），只是别拿它去查库、别写脏数据。
    """
    if not token or not isinstance(token, str):
        return False
    if not 20 <= len(token) <= 128:
        return False
    return not any(ch.isspace() for ch in token)
