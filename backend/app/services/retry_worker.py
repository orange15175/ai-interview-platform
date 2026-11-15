"""失败提交的后台补偿执行器（§5.3 / §5.4）。

为什么不是"每个 Uvicorn worker 起一个扫描线程"：那样 N 个进程各自扫、各自抢，
退避和上限只能靠数据库条件更新兜底，白白放大无效查询；而且 Uvicorn 的 --reload
会让线程随进程重启反复起停。所以做成**独立进程**（scripts/retry_worker.py），
用数据库抢占保证多实例安全——将来起两个也不会重复执行。

一轮做的事：
1. 扫到期 FAILED（processing_status / retryable / next_retry_at 三列条件）；
2. 逐条条件更新抢占，rowcount == 1 才拥有执行权；
3. 拿会话锁 → 重读数据库 → 确认会话没结束、这条没完成、题还对得上；
4. 用**原 request_id、原回答内容**续跑（交给 interview_workflow 统一收尾）；
5. 失败由 _persist_partial 算好 retryable / next_retry_at；

诚实边界：这是"至多一次有效结果"的补偿，不是 exactly-once。模型超时后
如果供应商其实已经算完，重试会多花一次钱；但不会产生两份评分——写入端
靠 (session_id, question_id) 唯一约束、write_epoch 与 state_version 三道拦住。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session as OrmSession

from app.core.config import get_settings
from app.core.errors import AppError, SessionBusyError
from app.core.logging import get_logger
from app.models import InterviewAnswer
from app.repositories import answers as answers_repo
from app.repositories import sessions as sessions_repo
from app.services import compensation, graph_runtime, interview_workflow, locks

log = get_logger(__name__)

# 抢不到锁时把任务放回队列的延迟：立刻放回会在下一轮被同一批扫到，
# 变成"扫描—抢锁失败—再扫描"的热循环。
_BUSY_BACKOFF_SECONDS = 30


def _acquire_lock(session_id) -> "locks.SessionLock | None":
    """拿会话锁；拿不到返回 None（不抛错，交给调用方决定延后）。"""
    try:
        return locks.acquire(str(session_id), wait_ms=0)
    except SessionBusyError:
        return None


def _defer(db: OrmSession, answer: InterviewAnswer, now: datetime | None = None) -> None:
    """抢不到会话锁：把任务原样放回队列，只把到期时间往后挪。

    必须把 processing_status 从 PROCESSING 还原成 FAILED——抢占是条件更新，
    不改回来这行就永远不再被扫描（状态不是 FAILED），一次锁竞争等于吞掉一次重试。
    """
    answer.processing_status = "FAILED"
    answer.next_retry_at = (now or datetime.now(timezone.utc)) + timedelta(
        seconds=_BUSY_BACKOFF_SECONDS
    )
    answer.claimed_at = None
    answer.claim_token = None
    db.commit()


def _return_to_queue(db: OrmSession, answer_id: uuid.UUID, now: datetime | None) -> None:
    """未预期异常后把抢占还原：状态回到 FAILED，到期时间延后一轮。

    只做"还原"，不写错误码：这种情况下失败原因可能是进程级问题
    （数据库瞬断等），记成业务错误码会误导复盘。
    """
    fresh = answers_repo.get(db, answer_id)
    if fresh is None or fresh.processing_status != "PROCESSING":
        return
    fresh.processing_status = "FAILED"
    fresh.claimed_at = None
    fresh.claim_token = None
    fresh.next_retry_at = (now or datetime.now(timezone.utc)) + timedelta(
        seconds=_BUSY_BACKOFF_SECONDS
    )
    try:
        db.commit()
    except Exception:  # noqa: BLE001 —— 还原失败也别把原始异常盖掉
        db.rollback()


def process_claimed(
    db: OrmSession,
    *,
    answer_id: uuid.UUID,
    graph,
    redis_client=None,
    lock_factory: Callable = _acquire_lock,
    now: datetime | None = None,
) -> str:
    """处理一条已抢占的失败提交，返回结果标签（便于统计与断言）。

    返回：completed / retried_failed / skipped（会话结束、题不匹配等）/
          busy（没抢到会话锁，已延后）
    """
    answer = answers_repo.get(db, answer_id)
    if answer is None:
        return "skipped"
    session = sessions_repo.get(db, answer.session_id)
    if session is None:
        answers_repo.mark_retry_exhausted(db, answer, reason="会话已不存在")
        db.commit()
        return "skipped"
    if answer.session_id != session.id:  # 防御：理论上不可能，但错配代价太大
        answers_repo.mark_retry_exhausted(db, answer, reason="回答与会话不匹配")
        db.commit()
        return "skipped"

    lock = lock_factory(session.id)
    if lock is None:
        _defer(db, answer, now)
        log.info("补偿任务等待会话锁，延后 %ss answer=%s", _BUSY_BACKOFF_SECONDS, answer.id)
        return "busy"

    lock.start_auto_renew()
    try:
        # 锁内重读：等锁期间用户可能已经手动重试成功，或会话已经结束
        db.refresh(session)
        db.refresh(answer)
        if answer.processing_status == "COMPLETED":
            db.rollback()
            return "skipped"
        if session.status in sessions_repo.TERMINAL_STATUSES:
            answers_repo.mark_retry_exhausted(
                db, answer, reason=f"会话已结束（{session.status}），不再自动重试"
            )
            db.commit()
            return "skipped"

        try:
            interview_workflow.resume_claimed_answer(
                db=db, graph=graph, session=session, answer_row=answer,
                lock=lock, redis_client=redis_client,
            )
        except AppError as exc:
            # _persist_partial 已按错误码算过 retryable / next_retry_at
            db.refresh(answer)
            if (answer.attempt_count or 0) >= compensation.MAX_ATTEMPTS:
                answers_repo.mark_retry_exhausted(
                    db, answer,
                    reason=f"已自动重试 {answer.attempt_count} 次仍未成功（{exc.code}）",
                )
                db.commit()
            log.warning(
                "补偿执行失败 answer=%s code=%s retryable=%s attempt=%s",
                answer.id, exc.code, answer.retryable, answer.attempt_count,
            )
            return "retried_failed"
        except Exception:  # noqa: BLE001 —— 未预期异常也得先离开 PROCESSING
            # 不处理的话这行会永久停在 PROCESSING：扫描只认 FAILED，
            # 一次偶发异常就等于把这条提交从补偿队列里彻底抹掉。
            db.rollback()
            _return_to_queue(db, answer_id, now)
            raise
        return "completed"
    finally:
        lock.release()


def run_once(
    *,
    db_factory,
    graph=None,
    limit: int | None = None,
    now: datetime | None = None,
    redis_client=None,
    lock_factory: Callable = _acquire_lock,
) -> dict:
    """扫一轮并处理，返回统计。供 CLI 与测试共用（测试里 db_factory 指向内存库）。"""
    graph = graph or graph_runtime.get_compiled_graph()
    limit = limit or 5
    now = now or datetime.now(timezone.utc)

    # 第一步只做只读扫描，且用一个独立短会话：抢占用条件更新，
    # 不需要在同一事务里既扫描又执行。
    scan_db = db_factory()
    try:
        due = answers_repo.due_for_retry(scan_db, limit=limit, now=now)
        answer_ids = [row.id for row in due]
    finally:
        scan_db.close()

    stats = {"scanned": len(answer_ids), "claimed": 0,
             "completed": 0, "retried_failed": 0, "skipped": 0, "busy": 0}

    for answer_id in answer_ids:
        claim_db = db_factory()
        try:
            token = uuid.uuid4().hex  # 随机 token，绝不用 worker 名当 token
            if not answers_repo.claim_for_retry(
                claim_db, answer_id, claim_token=token, now=now
            ):
                # 已被别的 worker 或用户的重试接管：跳过，不重复执行
                continue
            stats["claimed"] += 1
            outcome = process_claimed(
                claim_db, answer_id=answer_id, graph=graph,
                redis_client=redis_client, lock_factory=lock_factory, now=now,
            )
            stats[outcome] = stats.get(outcome, 0) + 1
        except Exception as exc:  # noqa: BLE001 —— 单条失败不能拖垮整轮
            log.exception("补偿执行异常 answer=%s: %s", answer_id, type(exc).__name__)
            try:
                claim_db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            claim_db.close()

    return stats


def worker_interval_seconds() -> int:
    """扫描间隔（默认 15 秒）。太密只是空转，太疏会让"30 秒后重试"名不副实。"""
    raw = (get_settings().retry_worker_interval_seconds or 15)
    return max(int(raw), 1)


__all__ = ["run_once", "process_claimed", "worker_interval_seconds"]
