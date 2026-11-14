"""用户仓储（账号 + 密码）。

约定与其它仓储一致：不自己开事务，由调用方决定提交边界。

两个实现要点：
1. **账号唯一由数据库唯一约束兜底**，不做"先查再插"——两个并发注册请求
   会同时查到"不存在"，双双插入成功。这里捕获 IntegrityError 转成
   AccountTakenError（409），让客户端拿到确定的语义。
2. 账号统一小写存储，登录也按小写查：这样 "Alice" 与 "alice" 是同一个账号，
   唯一约束才真正等于"账号唯一"。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.core.errors import AccountTakenError
from app.core.security import hash_password
from app.models import User
from app.models.base import utcnow


def normalize_account(account: str) -> str:
    """账号归一化：去空白 + 转小写（大小写不敏感的唯一性）。"""
    return (account or "").strip().lower()


def get_by_account(db: OrmSession, account: str) -> User | None:
    return db.scalars(
        select(User).where(User.account == normalize_account(account))
    ).one_or_none()


def get(db: OrmSession, user_id: uuid.UUID | str) -> User | None:
    try:
        return db.get(User, uuid.UUID(str(user_id)))
    except (ValueError, AttributeError):
        return None


def create(db: OrmSession, *, account: str, password: str) -> User:
    """注册。账号已存在 → AccountTakenError(409)。"""
    user = User(
        id=uuid.uuid4(),
        account=normalize_account(account),
        password_hash=hash_password(password),
        created_at=utcnow(),
    )
    try:
        # SAVEPOINT：撞唯一约束只回滚这一小段，不牵连调用方事务里已完成的写
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError as exc:
        raise AccountTakenError() from exc
    return user


def touch_login(db: OrmSession, user: User) -> User:
    """记录最近登录时间（只用于展示/排查，不参与鉴权）。"""
    user.last_login_at = datetime.now(timezone.utc)
    db.flush()
    return user
