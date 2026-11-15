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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session as OrmSession

from app.core.config import get_settings
from app.core.errors import (
    SessionFinishedError,
    SessionNotFoundError,
    VersionConflictError,
    WriteFenceLostError,
)
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

    身份**二选一，绝不同时成立**：
    - 已登录（user_id 非空）→ 只认 user_id：这场面试属于我这个账号吗？
    - 匿名（user_id 为空）→ 只认 owner_id **且该会话没被账号认领**
      （user_id IS NULL）：访客只能碰"还没有主人的"会话。

    为什么不能"任一满足即可"（or_）：owner_id 是浏览器粒度、user_id 是账号粒度，
    两者不同权。同一个浏览器上 A 登出、B 登录时访客 Cookie **不变**，若取或，
    B 就能顺着 owner_id 读到 A 的全部面试（题干、简历要点、作答原文全在里面）；
    反过来，A 只是退出登录后，匿名的 owner_id 也能读到 A 已绑账号的会话。
    所以两边都收紧：登录态忽略 owner_id，匿名态排除已认领的会话。

    判定写在 SQL 的 where 里，查不到直接抛错——绝不"先查出来再 if"。
    """
    if user_id is not None:
        identity = InterviewSession.user_id == _as_uuid(user_id)
    else:
        identity = and_(
            InterviewSession.owner_id == _as_uuid(owner_id),
            InterviewSession.user_id.is_(None),
        )
    stmt = select(InterviewSession).where(
        InterviewSession.id == _as_uuid(session_id),
    ).where(identity)
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


def _is_postgres(db: OrmSession) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


@dataclass(frozen=True)
class WriteFence:
    """本次请求的写入资格（拿锁之后、调模型之前取得）。

    epoch：拿到锁时递增到的代次。此后只要有人接管了锁，epoch 就会更大，
    我用旧 epoch 去写就影响 0 行 → 拒绝。
    state_version：取得栅栏那一刻的业务状态版本，写入时一并比对。
    """

    epoch: int
    state_version: int

    def covers(self, session: InterviewSession) -> bool:
        return session.write_epoch == self.epoch


def begin_write(db: OrmSession, session: InterviewSession) -> WriteFence:
    """取得写入资格：行锁内递增 write_epoch，读出 state_version，**立刻提交**。

    为什么必须马上提交：模型调用动辄几十秒，把数据库行锁带进模型调用等于
    让整场面试排队等一个外部 HTTP 请求。这里只做一次极短的读改写，
    真正的写入保护靠后面带条件的 UPDATE（见 bump_state_version 的 fence 参数）。

    也正因为它短，"锁已拿到但 epoch 没来得及递增"的窗口极小；即便撞上，
    条件更新也会让其中一方影响 0 行，不会两份结果都落库。
    """
    if _is_postgres(db):
        # 先拿行锁再读改写，避免两个接管者同时读到同一个 epoch 各自 +1
        db.execute(
            select(InterviewSession.id)
            .where(InterviewSession.id == session.id)
            .with_for_update()
        )
    db.execute(
        update(InterviewSession)
        .where(InterviewSession.id == session.id)
        .values(write_epoch=InterviewSession.write_epoch + 1)
        .execution_options(synchronize_session=False)
    )
    db.expire(session, ["write_epoch", "state_version"])
    epoch = int(session.write_epoch or 0)
    version = int(session.state_version or 0)
    db.commit()  # 短事务到此为止，后面是模型调用，不许再占着行锁
    return WriteFence(epoch=epoch, state_version=version)


def check_fence(session: InterviewSession, fence: WriteFence | None) -> None:
    """写入前的快速自检（真正的闸仍是数据库条件更新）。

    这里拦的是"我自己都知道已经丢了"的情况：早点报错，别等跑完模型、
    写完半张表再回滚。
    """
    if fence is not None and not fence.covers(session):
        raise WriteFenceLostError()


def bump_state_version(
    db: OrmSession,
    session: InterviewSession,
    *,
    expected: int | None = None,
    fence: WriteFence | None = None,
) -> int:
    """CAS 递增版本号：`UPDATE ... WHERE state_version = expected [AND write_epoch = epoch]`。

    返回新版本号；rowcount 为 0 → 拒绝写入并回滚（区分是丢了写入资格还是
    状态被别人推进，见 _reject_write）。
    不要改成"读出来 +1 再写"：两个 worker 会同时读到同一个值，双双写成功。

    fence 非空时同时校验 write_epoch —— 两道条件缺一不可：
    只比 state_version，两个请求都读到 v=1 时会各自写 v=2，后者覆盖前者；
    只比 write_epoch，同一持有者内部的重复推进（重放）拦不住。
    """
    current = session.state_version if expected is None else expected
    new_version = current + 1
    conditions = [
        InterviewSession.id == session.id,
        InterviewSession.state_version == current,
    ]
    if fence is not None:
        conditions.append(InterviewSession.write_epoch == fence.epoch)

    result = db.execute(
        update(InterviewSession)
        .where(*conditions)
        .values(state_version=new_version, last_activity_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _reject_write(db, session, fence)
    session.state_version = new_version
    return new_version


def _reject_write(
    db: OrmSession, session: InterviewSession, fence: WriteFence | None
) -> None:
    """条件更新影响 0 行：判定是哪一层拦的，然后抛错。

    这里**不**自己 rollback：回滚责任在调用方（它才知道这条事务里还有哪些
    写入该一起丢掉）。这里只做一次只读回查——条件更新已经拿到行锁，
    读到的就是当前已提交的值，拿它区分"丢了写入资格"和"状态被别人推进"。
    """
    row = db.execute(
        select(InterviewSession.write_epoch, InterviewSession.state_version)
        .where(InterviewSession.id == session.id)
    ).one_or_none()
    if fence is not None and row is not None and int(row[0] or 0) != fence.epoch:
        raise WriteFenceLostError()
    raise VersionConflictError()


def mark_preparation_failed(
    db: OrmSession,
    session: InterviewSession,
    message: str,
    *,
    stage: str | None = None,
    error_code: str | None = None,
) -> None:
    """解析/出题失败：终态 + 脱敏错误码，允许用户重新创建（不覆盖旧记录）。

    stage / error_code 写进 progress：用户重试时后端要能判断"材料到底存下来了
    没有"——存在解析结果就只重跑出题，不存在才要求重新上传（§十）。
    """
    progress: dict[str, Any] = {
        "stage": "PREPARATION_FAILED",
        "sanitized_error": message[:300],
    }
    if stage:
        progress["failed_stage"] = stage
    if error_code:
        progress["error_code"] = error_code
    touch(
        db,
        session,
        status="PREPARATION_FAILED",
        sanitized_error=message[:500],
        completion_reason="PREPARATION_FAILED",
        progress=progress,
        finished=True,
    )
