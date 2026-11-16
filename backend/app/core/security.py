"""匿名访客凭证（开发说明 §1.1 第 3 条 / §11）。

设计要点，别改错：
1. 明文 token 只存在于浏览器的 HttpOnly Cookie 里，数据库只存 SHA-256 哈希。
   拖库者拿到哈希也无法伪造 Cookie（SHA-256 单向；token 本身是 32 字节随机，
   不需要加盐也不能用快速哈希以外的方案——它本来就是高熵随机串，不是密码）。
2. token 用 secrets 生成高随机性字符串；不要把公开的会话 UUID 当作身份凭证。
3. Cookie 属性：HttpOnly（JS 不能直接读取 Cookie，但不能单独防住 XSS）、SameSite=Lax（跨站写请求
   不带 Cookie，配合 Origin 校验防 CSRF）、生产 HTTPS 下 Secure。
4. 清理匿名 Cookie 会丢失匿名身份；已绑定账号的会话仍可用对应账号登录后访问。
   系统不提供"按手机号找回"，也不提供全局记录列表
   （§11：UUID 不等于访问权限）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid

# 32 字节随机 → token_urlsafe 后约 43 个字符，远超暴力枚举可能
TOKEN_BYTES = 32
# 与 models/tables.py 的 token_hash 字段长度（String(64)）一致：sha256 hex 正好 64
TOKEN_HASH_LEN = 64


def new_visitor_token() -> str:
    """生成高随机性的匿名凭证，浏览器持有明文，数据库只存其哈希。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_visitor_token(token: str) -> str:
    """把匿名凭证转成 SHA-256 哈希，供数据库查询和存储。"""
    if not token:
        raise ValueError("访客 token 为空")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, token_hash: str) -> bool:
    """对匿名凭证计算哈希后用恒定时间方式比较，避免普通字符串比较的耗时差异。"""
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_visitor_token(token), token_hash)


def looks_like_token(token: str | None) -> bool:
    """先检查匿名凭证长度和空白，挡掉明显无效输入；最终身份仍靠查库。

    这不是安全边界（真正的边界是哈希查库），只是别拿它去查库、别写脏数据。"""
    if not token or not isinstance(token, str):
        return False
    if not 20 <= len(token) <= 128:
        return False
    return not any(ch.isspace() for ch in token)


# ---------------------------------------------------------------------------
# 账号密码（注册 / 登录）
# ---------------------------------------------------------------------------
#
# 为什么用 PBKDF2 而不是 bcrypt/argon2：
# 标准库直接有，不引入新依赖（部署面越小越好），PBKDF2-HMAC-SHA256 在
# 600k 迭代下对"离线爆破"的代价已经足够。真要升级到 argon2 只需改这两个函数，
# 存储格式里带算法名与迭代次数，老哈希仍可校验、可平滑迁移。

PASSWORD_ALGO = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
_SALT_BYTES = 16
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 128   # 上限不是安全需要，是防止超长输入把 CPU 打满


def hash_password(password: str) -> str:
    """使用随机盐和多轮 PBKDF2 生成密码哈希，数据库不存明文密码。"""
    if not isinstance(password, str) or not password:
        raise ValueError("密码为空")
    if len(password) > PASSWORD_MAX_LEN:
        raise ValueError("密码过长")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"{PASSWORD_ALGO}${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """解析已存哈希并重算密码摘要，再做恒定时间比较；常见格式错误按不匹配处理。"""
    if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX_LEN:
        return False
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != PASSWORD_ALGO:
            return False
        iterations_int = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations_int)
    return hmac.compare_digest(actual, expected)  # 恒定时间，防计时侧信道


# ---------------------------------------------------------------------------
# 登录态令牌（无状态签名 Cookie）
# ---------------------------------------------------------------------------
#
# 格式：`{user_id}.{过期时间戳}.{HMAC-SHA256 签名}`
# - 无状态：不用为每个登录会话在 Redis/PG 建表，重启也不掉线；
# - 签名密钥只在服务端（AUTH_SECRET），客户端改不了 uid/exp；
# - 登出 = 清 Cookie（首期不做"踢下线"，多端共享同一令牌）。


def issue_login_token(user_id: uuid.UUID | str, *, secret: str, ttl_hours: int) -> tuple[str, int]:
    """把用户 ID 和过期时间签名，返回 Cookie 值及浏览器保存时长。"""
    if not secret:
        raise ValueError("AUTH_SECRET 未配置，无法签发登录令牌")
    expires_at = int(time.time()) + max(1, ttl_hours) * 3600
    payload = f"{user_id}.{expires_at}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}", max(1, ttl_hours) * 3600


def parse_login_token(token: str | None, *, secret: str) -> uuid.UUID | None:
    """核验令牌签名、过期时间和用户 ID，失败就返回 None，不能自行信任 Cookie。"""
    if not token or not secret or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    uid_part, exp_part, signature = parts
    payload = f"{uid_part}.{exp_part}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        if int(exp_part) < int(time.time()):
            return None
        return uuid.UUID(uid_part)
    except (ValueError, AttributeError):
        return None

