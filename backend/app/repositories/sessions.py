"""面试会话仓储（开发说明 §8 interview_sessions / §9.2 版本校验）。

两个不能省的约束：
1. **归属校验在 SQL 里做**：get_owned 的 where 同时带 session_id 与 owner_id，
   查不到就抛 SessionNotFoundError。绝不"先查出来再 if 判断"——那样容易在某个
   分支里忘了判断，变成越权读取。
2. **state_version 只能 CAS 递增**：锁租约失效后旧请求仍可能跑完模型调用，
   写入前用 `UPDATE ... WHERE state_version = 期望值` 兜住，rowcount=0 即拒绝。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session as OrmSession

from app.core.config import get_settings
from app.core.errors import SessionFinishedError, SessionNotFoundError, VersionConflictError
from app.models import InterviewSession
from app.models.base import utcnow

# 处在这些状态的会话不再接受答题（§6.1 生命周期）
TERMINAL_STATUSES = ("FINISHED", "CANCELLED", "EXPIRED", "PREPARATION_FAILED")

_UNSET = object()


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def create(
    db: OrmSession,
    *,
    owner_id: uuid.UUID,
    question_count: int | None = None,
    user_id: uuid.UUID | None = None,
) -> InterviewSession:
    """创建会话（状态 PREPARING，等解析/出题完成才置 READY）。

    user_id 只在"创建时已登录"的情况下写入：这样换浏览器重新登录还能找回
    这场面试；匿名创建仍是 None，与旧行为完全一致。
    """
    count = question_count if question_count is not None else get_settings().business.question_count
    session = InterviewSession(
        id=uuid.uuid4(),
        owner_id=owner_id,
        user_id=user_id,
        status="PREPARING",
        question_count=count,
        state_version=0,
        progress={},
    )
    db.add(session)
    db.flush()
    return session


def get(db: OrmSession, session_id: uuid.UUID | str) -> InterviewSession | None:
    return db.get(InterviewSession, _as_uuid(session_id))


def get_owned(
    db: OrmSession,
    session_id: uuid.UUID | str,
    owner_id: uuid.UUID | str,
    *,
    user_id: uuid.UUID | str | None = None,
) -> InterviewSession:
    """按归属读取；不存在与无权限合并成同一个 404，不泄露"ID 存在"。

    两种合法身份：
    - 匿名访客本人（owner_id，创建这场会话的那枚 Cookie）；
    - 会话所属账号（user_id，登录后创建、或创建时就绑定了账号）。
    判定写在 SQL 的 where 里，查不到直接抛错——绝不"先查出来再 if"。
    """
    stmt = select(InterviewSession).where(
        InterviewSession.id == _as_uuid(session_id),
    )
    identities = [InterviewSession.owner_id == _as_uuid(owner_id)]
    if user_id is not None:
        identities.append(InterviewSession.user_id == _as_uuid(user_id))
    stmt = stmt.where(or_(*identities))
    session = db.scalars(stmt).one_or_none()
    if session is None:
        raise SessionNotFoundError()
    return session


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        # 非法 UUID 也走"不存在"，不把解析细节抛给客户端
        raise SessionNotFoundError() from exc


def assert_can_answer(session: InterviewSession) -> None:
    """终态检查（§7.2 规则链第 0 条：终态直接拒绝，不进入评分）。"""
    if session.status in TERMINAL_STATUSES:
        raise SessionFinishedError()
    expires = _as_utc(session.last_activity_at)
    ttl = timedelta(hours=get_settings().business.session_ttl_hours)
    if expires is not None and expires + ttl < datetime.now(timezone.utc):
        session.status = "EXPIRED"
        raise SessionFinishedError("会话长时间无活动已过期，请重新创建面试")


def lock_row(db: OrmSession, session: InterviewSession) -> InterviewSession:
    """行级锁（PostgreSQL）。

    这是会话锁之外的**第二道**保护：多 worker 同时通过 Redis 锁的极端情况下
    （如租约刚过期），数据库行锁让两个请求排队，再由 state_version 拒绝旧写入。
    SQLite 不支持 FOR UPDATE，测试环境直接跳过（它的写本来就串行）。
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(
            select(InterviewSession.id)
            .where(InterviewSession.id == session.id)
            .with_for_update()
        )
        db.refresh(session)
    return session


def touch(
    db: OrmSession,
    session: InterviewSession,
    *,
    status: str | None = None,
    current_question_id: uuid.UUID | None | object = _UNSET,
    progress: dict[str, Any] | None = None,
    completion_reason: str | None | object = _UNSET,
    sanitized_error: str | None | object = _UNSET,
    started: bool = False,
    finished: bool = False,
) -> InterviewSession:
    """更新会话的展示投影与进度（不负责版本号，版本只走 bump_state_version）。"""
    if status is not None:
        session.status = status
    if current_question_id is not _UNSET:
        session.current_question_id = current_question_id  # type: ignore[assignment]
    if progress is not None:
        session.progress = {**(session.progress or {}), **progress}
    if completion_reason is not _UNSET:
        session.completion_reason = completion_reason  # type: ignore[assignment]
    if sanitized_error is not _UNSET:
        session.sanitized_error = sanitized_error  # type: ignore[assignment]
    if started and session.started_at is None:
        session.started_at = utcnow()
    if finished and session.finished_at is None:
        session.finished_at = utcnow()
    session.last_activity_at = utcnow()
    db.flush()
    return session


def bump_state_version(
    db: OrmSession, session: InterviewSession, *, expected: int | None = None
) -> int:
    """CAS 递增版本号：`UPDATE ... WHERE state_version = expected`。

    返回新版本号；rowcount 为 0 说明有人抢先推进了 → VersionConflictError。
    不要改成"读出来 +1 再写"：两个 worker 会同时读到同一个值，双双写成功。
    """
    current = session.state_version if expected is None else expected
    new_version = current + 1
    result = db.execute(
        update(InterviewSession)
        .where(
            InterviewSession.id == session.id,
            InterviewSession.state_version == current,
        )
        .values(state_version=new_version, last_activity_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise VersionConflictError()
    session.state_version = new_version
    return new_version


def mark_preparation_failed(db: OrmSession, session: InterviewSession, message: str) -> None:
    """解析/出题失败：终态 + 脱敏错误码，允许用户重新创建（不覆盖旧记录）。"""
    touch(
        db,
        session,
        status="PREPARATION_FAILED",
        sanitized_error=message[:500],
        completion_reason="PREPARATION_FAILED",
        finished=True,
    )
