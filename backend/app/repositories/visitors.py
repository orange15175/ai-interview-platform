"""匿名访客仓储：token 只存哈希，找回身份靠 Cookie 明文（开发说明 §8/§11）。

调用约定：所有函数接收调用方的 Session，不自己开事务——
一次 HTTP 请求里的多处写操作要能放进同一个短事务，由服务层决定提交边界。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.core.config import get_settings
from app.core.security import hash_visitor_token, looks_like_token, new_visitor_token
from app.models import AnonymousVisitor
from app.models.base import utcnow


def _as_utc(dt: datetime | None) -> datetime | None:
    """给测试库读回的无时区时间补上 UTC，避免过期比较报错。"""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_expired(visitor: AnonymousVisitor) -> bool:
    """比较访客凭证的固定截止时间与当前时间，不会自动延长有效期。"""
    expires = _as_utc(visitor.expires_at)
    return expires is not None and expires <= datetime.now(timezone.utc)


def find_by_token(db: OrmSession, token: str) -> AnonymousVisitor | None:
    """先粗查凭证格式，再按哈希找访客，明文 token 不参与数据库存储。"""
    if not looks_like_token(token):
        return None
    stmt = select(AnonymousVisitor).where(
        AnonymousVisitor.token_hash == hash_visitor_token(token)
    )
    return db.scalars(stmt).one_or_none()


def create(
    db: OrmSession, *, ttl_hours: int | None = None
) -> tuple[AnonymousVisitor, str]:
    """生成随机访客凭证并存其哈希，返回访客对象和仅供下发 Cookie 的明文。"""
    ttl = ttl_hours if ttl_hours is not None else get_settings().business.session_ttl_hours
    token = new_visitor_token()
    visitor = AnonymousVisitor(
        id=uuid.uuid4(),
        token_hash=hash_visitor_token(token),
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(hours=ttl),
    )
    db.add(visitor)
    db.flush()
    return visitor, token


def get_or_create(db: OrmSession, token: str | None) -> tuple[AnonymousVisitor, str | None]:
    """复用未过期访客；凭证缺失、无效或过期时创建新访客并要求下发新 Cookie。

    三种情况：
    - 有效 Cookie → 直接返回原访客，不需要重新下发；
    - Cookie 过期 → 新建访客并下发新 token（旧记录不删，但已无法访问）；
    - 无 Cookie / 伪造的 token → 新建访客并下发新 token。

    刻意不"信任客户端给的 token 并写库"：那样攻击者可以预置一个自己知道的
    token 再诱导别人使用，等于共享身份。"""
    if token:
        visitor = find_by_token(db, token)
        if visitor is not None and not is_expired(visitor):
            return visitor, None
    visitor, new_token = create(db)
    return visitor, new_token
