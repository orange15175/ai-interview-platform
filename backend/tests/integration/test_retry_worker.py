"""失败提交的后台补偿（§5.3 / §5.4 / §5.5）。

用 SQLite + MemorySaver + mock 模型跑真实链路，证明四件事：
1. 模型超时失败后，回答被保留、标记 retryable、算出退避时间；
2. worker 到点扫到它，用**原 request_id 与原回答**跑完，分数只产生一份；
3. 两个 worker 抢同一条任务时只有一个能执行（条件更新 rowcount 断言）；
4. 不可重试错误与已达上限的任务不会进队列 / 会退出队列。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import ModelCallError
from app.graph import build_interview_graph
from app.models import Base
from app.repositories import answers as answers_repo
from app.repositories import sessions as sessions_repo
from app.repositories import visitors as visitors_repo
from app.schemas.questions import GeneratedQuestion, RubricPoint
from app.services import compensation, graph_runtime, interview_workflow, retry_worker


class FakeLock:
    """只保留工作流会用到的三个成员。"""

    def __init__(self, *, busy: bool = False) -> None:
        self.lost = False
        self.busy = busy

    def start_auto_renew(self, *args, **kwargs) -> None:
        return None

    def release(self) -> bool:
        return True


def _questions(n: int) -> list[GeneratedQuestion]:
    return [
        GeneratedQuestion(
            main_index=i,
            category="项目经验",
            difficulty="medium",
            content=f"第 {i} 题：请结合项目说明你做过的第 {i} 个技术决策的完整权衡过程。",
            material_evidence_ids=["ev-1"],
            evaluation_rubric=[RubricPoint(point="讲清权衡", weight=1.0)],
        )
        for i in range(1, n + 1)
    ]


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    visitor, _ = visitors_repo.create(db)
    db.commit()
    session = sessions_repo.create(db, owner_id=visitor.id, question_count=2)
    session.status = "READY"
    db.commit()

    monkeypatch.setattr(interview_workflow.locks, "acquire", lambda *a, **k: FakeLock())
    graph = build_interview_graph(checkpointer=MemorySaver())
    started = interview_workflow.start_interview(
        db=db, graph=graph, session=session, questions=_questions(2)
    )
    try:
        yield type("Env", (), {
            "db": db, "factory": factory, "graph": graph,
            "session": session, "started": started, "visitor": visitor,
        })()
    finally:
        db.close()
        engine.dispose()


def _submit(env, *, question_id=None, request_id="req-1", answer="我做了取舍并给出量化结果。"):
    return interview_workflow.submit_answer(
        db=env.db,
        graph=env.graph,
        session_id=env.session.id,
        owner_id=env.visitor.id,
        question_id=question_id or env.started["question"]["question_id"],
        request_id=request_id,
        answer=answer,
        redis_client=None,
    )


def _fail_first_answer(env, monkeypatch, *, code_error: Exception | None = None):
    """让评分模型失败一次，返回那条 FAILED 的回答。"""
    import app.graph.nodes as nodes

    def boom(*args, **kwargs):
        raise code_error or ModelCallError()

    monkeypatch.setattr(nodes, "evaluate_answer", boom)
    with pytest.raises(ModelCallError):
        _submit(env)
    env.db.expire_all()
    row = answers_repo.get_by_request(env.db, env.session.id, "req-1")
    return row


# ---------------------------------------------------------------- 入队


def test_model_failure_is_kept_and_scheduled(env, monkeypatch):
    row = _fail_first_answer(env, monkeypatch)

    assert row.processing_status == "FAILED"
    assert row.answer_content  # 用户原始回答必须保留，重试要用它
    assert row.score is None  # 没有分数，绝不写 0
    assert row.last_error_code == "MODEL_TIMEOUT"
    assert row.retryable is True
    assert row.attempt_count == 0
    assert row.next_retry_at is not None
    # 首次失败 30 秒后由后台接手
    delay = (row.next_retry_at.replace(tzinfo=timezone.utc)
             - datetime.now(timezone.utc)).total_seconds()
    assert 0 < delay <= 30
    # 评分失败是核心结果缺失，单独记一级降级
    assert row.degradation_level == compensation.DEGRADATION_EVALUATION_FAILED
    # 会话没有被推进：当前题还是第一题
    env.db.refresh(env.session)
    assert str(env.session.current_question_id) == env.started["question"]["question_id"]


def test_non_retryable_error_never_enters_queue(env, monkeypatch):
    from app.core.errors import ModelOutputInvalidError

    row = answers_repo.get_by_request(env.db, env.session.id, "req-none")
    # 直接构造一条"不可重试"的失败（模拟材料不可读这类错误）
    row = _fail_first_answer(env, monkeypatch)
    answers_repo.fail(env.db, row, code="UNREADABLE_MATERIAL", message="读不出来")
    env.db.commit()

    assert row.retryable is False
    assert row.next_retry_at is None
    assert answers_repo.due_for_retry(env.db, limit=10) == []


def test_reconciled_row_is_not_retryable(env, monkeypatch):
    """恢复对账关掉的提交不能进自动补偿队列（否则会反复重跑同一条死记录）。"""
    row = _fail_first_answer(env, monkeypatch)
    answers_repo.fail(
        env.db, row, code="RECONCILED_ON_RECOVERY", message="恢复时对账关闭"
    )
    env.db.commit()

    assert row.retryable is False
    assert answers_repo.due_for_retry(env.db, limit=10) == []


# ---------------------------------------------------------------- 抢占


def test_only_one_worker_can_claim(env, monkeypatch):
    row = _fail_first_answer(env, monkeypatch)
    later = datetime.now(timezone.utc) + timedelta(seconds=60)

    first = answers_repo.claim_for_retry(env.db, row.id, claim_token="token-a", now=later)
    second = answers_repo.claim_for_retry(env.db, row.id, claim_token="token-b", now=later)

    assert first is True
    assert second is False  # 已被抢走：状态不再是 FAILED
    env.db.expire_all()
    stored = answers_repo.get(env.db, row.id)
    assert stored.claim_token == "token-a"
    assert stored.attempt_count == 1


def test_stale_claim_can_be_taken_over(env, monkeypatch):
    """worker 崩溃后，超过 claimed_at 超时的任务允许被再次接管。"""
    row = _fail_first_answer(env, monkeypatch)
    later = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert answers_repo.claim_for_retry(env.db, row.id, claim_token="token-a", now=later)

    # 模拟 worker A 崩了：把抢占时间调到很久以前
    env.db.expire_all()
    stored = answers_repo.get(env.db, row.id)
    stored.claimed_at = later - timedelta(minutes=10)
    stored.processing_status = "FAILED"
    env.db.commit()

    assert answers_repo.claim_for_retry(
        env.db, row.id, claim_token="token-b", now=later
    ) is True


# ---------------------------------------------------------------- 执行


def test_worker_completes_failed_answer_with_original_request_id(env, monkeypatch):
    row = _fail_first_answer(env, monkeypatch)
    monkeypatch.undo()  # 模型恢复可用

    later = datetime.now(timezone.utc) + timedelta(seconds=90)
    stats = retry_worker.run_once(
        db_factory=env.factory, graph=env.graph, limit=5, now=later,
        lock_factory=lambda sid: FakeLock(),
    )

    assert stats["scanned"] == 1
    assert stats["claimed"] == 1
    assert stats["completed"] == 1

    env.db.expire_all()
    stored = answers_repo.get(env.db, row.id)
    assert stored.processing_status == "COMPLETED"
    assert stored.request_id == "req-1"  # 沿用原请求编号，不产生第二条记录
    assert stored.score is not None
    assert stored.retryable is False and stored.next_retry_at is None
    assert len(answers_repo.list_for_session(env.db, env.session.id)) == 1

    # 图只被推进一次：不跳题、不重复评分
    values, _, waiting = graph_runtime.snapshot_state(env.graph, env.session.id)
    assert waiting is True
    assert len(values["evaluations"]) == 1

    # 会话推进到第二题
    env.db.refresh(env.session)
    assert env.session.current_question_id is not None
    assert str(env.session.current_question_id) != env.started["question"]["question_id"]


def test_worker_does_not_reprocess_completed_answer(env, monkeypatch):
    row = _fail_first_answer(env, monkeypatch)
    monkeypatch.undo()

    later = datetime.now(timezone.utc) + timedelta(seconds=90)
    retry_worker.run_once(
        db_factory=env.factory, graph=env.graph, limit=5, now=later,
        lock_factory=lambda sid: FakeLock(),
    )
    stats = retry_worker.run_once(
        db_factory=env.factory, graph=env.graph, limit=5,
        now=later + timedelta(seconds=60), lock_factory=lambda sid: FakeLock(),
    )

    assert stats["scanned"] == 0  # 完成的行已退出队列
    env.db.expire_all()
    stored = answers_repo.get(env.db, row.id)
    assert stored.processing_status == "COMPLETED"


def test_busy_session_is_deferred_not_lost(env, monkeypatch):
    """抢不到会话锁：把任务放回队列（延后），不能吞掉也不能热循环。"""
    row = _fail_first_answer(env, monkeypatch)
    later = datetime.now(timezone.utc) + timedelta(seconds=90)

    stats = retry_worker.run_once(
        db_factory=env.factory, graph=env.graph, limit=5, now=later,
        lock_factory=lambda sid: None,  # 模拟锁在别人手里
    )

    assert stats["busy"] == 1
    env.db.expire_all()
    stored = answers_repo.get(env.db, row.id)
    assert stored.processing_status == "FAILED"
    assert stored.retryable is True
    assert stored.claim_token is None
    # SQLite 读回来是 naive（存的就是 UTC），比较前补时区
    assert stored.next_retry_at.replace(tzinfo=timezone.utc) > later


def test_attempt_limit_marks_task_exhausted(env, monkeypatch):
    row = _fail_first_answer(env, monkeypatch)
    env.db.expire_all()
    stored = answers_repo.get(env.db, row.id)
    stored.attempt_count = compensation.MAX_ATTEMPTS
    env.db.commit()

    later = datetime.now(timezone.utc) + timedelta(seconds=90)
    stats = retry_worker.run_once(
        db_factory=env.factory, graph=env.graph, limit=5, now=later,
        lock_factory=lambda sid: FakeLock(),
    )

    # 已到上限的行不再被扫描（fail() 时就会把 retryable 置假）
    assert stats["scanned"] == 0
    assert stats["completed"] == 0
    env.db.expire_all()
    assert answers_repo.get(env.db, row.id).processing_status == "FAILED"


def test_finished_session_is_not_retried(env, monkeypatch):
    row = _fail_first_answer(env, monkeypatch)
    env.db.expire_all()
    stored_session = sessions_repo.get(env.db, env.session.id)
    stored_session.status = "FINISHED"
    env.db.commit()

    later = datetime.now(timezone.utc) + timedelta(seconds=90)
    stats = retry_worker.run_once(
        db_factory=env.factory, graph=env.graph, limit=5, now=later,
        lock_factory=lambda sid: FakeLock(),
    )

    assert stats["skipped"] == 1
    env.db.expire_all()
    stored = answers_repo.get(env.db, row.id)
    assert stored.retryable is False  # 退出队列，不再反复扫
    assert stored.degradation_reason and "已结束" in stored.degradation_reason
