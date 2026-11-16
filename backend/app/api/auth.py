"""账号注册 / 登录 / 登出 / 当前用户（首期：账号 + 密码，账号唯一）。

接口契约：
- POST /api/auth/register {account, password} → 201 {user} + 直接登录（下发 Cookie）
- POST /api/auth/login    {account, password} → 200 {user} + 下发 Cookie
- POST /api/auth/logout                        → 200，清 Cookie（幂等：没登录也返回 200）
- GET  /api/auth/me                            → 200 {user} 或 {user: null}（不报错）

安全口径（都是刻意的，别"优化"掉）：
- 账号或密码错误一律 INVALID_CREDENTIALS(401)，不区分是哪个错 → 否则这个接口
  就是账号枚举器；
- 密码只存 PBKDF2 哈希，任何情况下不回显、不进日志；
- 登录态是 HMAC 签名的 HttpOnly Cookie（见 core/security.py），JS 读不到；
- 所有写接口都过 require_same_origin（CSRF），Cookie 自动携带的场景必须有这层。
"""

from __future__ import annotations

import re
from datetime import timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import get_current_user, get_db, require_same_origin
from app.core.config import get_settings
from app.core.errors import InvalidCredentialsError
from app.core.logging import get_logger
from app.core.security import (
    PASSWORD_MAX_LEN,
    PASSWORD_MIN_LEN,
    issue_login_token,
    verify_password,
)
from app.models import User
from app.repositories import users as users_repo

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# 账号规则：3~64 位，字母数字与 _ . @ + -（够用且不会与未来路径/URL 冲突）
ACCOUNT_PATTERN = re.compile(r"^[a-z0-9_.@+-]{3,64}$")


class RegisterRequest(BaseModel):
    # 长度上限不交给 Field：pydantic 的约束错误是英文（"String should have at
    # least 3 characters"），会盖掉下面校验器里手写的中文提示（约束先于
    # field_validator(after) 执行）。长度统一在 ACCOUNT_PATTERN / 校验器里判。
    account: str
    password: str

    @field_validator("account")
    @classmethod
    def _account_charset(cls, v: str) -> str:
        """去掉账号首尾空白并转小写，再检查长度和允许的字符。"""
        v = (v or "").strip().lower()
        if not ACCOUNT_PATTERN.match(v):
            raise ValueError("账号只能是 3~64 位字母、数字或 _ . @ + -")
        return v

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        """检查密码长度、首尾空白及字符组合，不符合要求就拒绝注册。"""
        if len(v) > PASSWORD_MAX_LEN:
            raise ValueError(f"密码不能超过 {PASSWORD_MAX_LEN} 位")
        if len(v) < PASSWORD_MIN_LEN:
            raise ValueError(f"密码至少 {PASSWORD_MIN_LEN} 位")
        if v.strip() != v:
            raise ValueError("密码首尾不能是空白字符")
        if v.isdigit() or v.isalpha():
            raise ValueError("密码不能是纯数字或纯字母，请混合使用")
        return v


class LoginRequest(BaseModel):
    # 登录不校验强度：规则可能变过，老密码必须还能登进来（判错由 verify_password 负责）
    account: str
    password: str

    @field_validator("account")
    @classmethod
    def _normalize(cls, v: str) -> str:
        """把登录账号转成与注册时相同的格式，避免大小写导致找不到账号。"""
        return (v or "").strip().lower()

    @field_validator("password")
    @classmethod
    def _length(cls, v: str) -> str:
        """拒绝空密码和过长密码；登录时不重复要求密码强度。"""
        if not v:
            raise ValueError("请输入密码")
        if len(v) > PASSWORD_MAX_LEN:
            raise ValueError("账号或密码不正确")
        return v


def _iso_utc(value) -> str | None:
    """把数据库时间统一转成 UTC 字符串，供浏览器正确解析。"""
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite/旧数据可能读回 naive，按 UTC 解释
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _public_user(user: User) -> dict:
    """只挑出可公开的账号信息，防止密码哈希进入响应。"""
    return {
        "user_id": str(user.id),
        "account": user.account,
        "created_at": _iso_utc(user.created_at),
        "last_login_at": _iso_utc(user.last_login_at),
    }


def _set_login_cookie(response: Response, user: User) -> None:
    """签发带有效期的登录令牌，并写入浏览器脚本无法直接读取的 Cookie。"""
    settings = get_settings()
    token, max_age = issue_login_token(
        user.id, secret=settings.auth_secret, ttl_hours=settings.auth_ttl_hours
    )
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,                  # JS 读不到
        samesite="lax",                 # 跨站写请求不带 Cookie
        secure=settings.cookie_secure,  # 生产 HTTPS 必须开
        path="/",
    )


def _clear_login_cookie(response: Response) -> None:
    """让浏览器删除登录 Cookie；服务端不会撤销已经签发的令牌。"""
    settings = get_settings()
    response.delete_cookie(key=settings.auth_cookie_name, path="/")


@router.post("/register", status_code=201, dependencies=[Depends(require_same_origin)])
def register(
    payload: RegisterRequest,
    response: Response,
    db: OrmSession = Depends(get_db),
) -> dict:
    """创建账号并提交数据库，然后下发登录 Cookie，实现注册后直接登录。"""
    user = users_repo.create(db, account=payload.account, password=payload.password)
    db.commit()
    _set_login_cookie(response, user)
    log.info("新用户注册 user=%s account=%s", user.id, user.account)
    return {"user": _public_user(user)}


@router.post("/login", dependencies=[Depends(require_same_origin)])
def login(
    payload: LoginRequest,
    response: Response,
    db: OrmSession = Depends(get_db),
) -> dict:
    """验证账号密码，成功后记录登录时间并下发 Cookie；账号不存在也做密码校验。"""
    user = users_repo.get_by_account(db, payload.account)
    # 账号不存在也要走一次哈希校验，避免"响应快慢"泄露账号是否存在
    stored = user.password_hash if user else _DUMMY_HASH
    ok = verify_password(payload.password, stored)
    if user is None or not ok:
        raise InvalidCredentialsError()

    users_repo.touch_login(db, user)
    db.commit()
    _set_login_cookie(response, user)
    log.info("用户登录 user=%s", user.id)
    return {"user": _public_user(user)}


@router.post("/logout", dependencies=[Depends(require_same_origin)])
def logout(response: Response) -> dict:
    """删除浏览器的登录 Cookie，重复调用也可以正常返回。"""
    _clear_login_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(user: User | None = Depends(get_current_user)) -> dict:
    """返回当前登录用户；没有有效登录态时返回 user=null，供前端显示登录页。"""
    return {"user": _public_user(user) if user else None}


# 用于"账号不存在"时也走一次同样耗时的哈希校验（防时序侧信道）。
# 内容是一个随机密码的合法哈希，永远不会匹配任何输入。
_DUMMY_HASH = (
    "pbkdf2_sha256$600000$"
    "00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
