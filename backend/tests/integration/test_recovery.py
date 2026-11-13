"""checkpoint 丢失后的显式重建（开发说明 §9.4）。

用新的 MemorySaver 代表"Redis 数据没了"，验证：
- 能从 PostgreSQL 的材料/题目/回答/评分重建到**真正的等待点**（图真的 interrupt）；
- 重建后继续答题，历史评分不丢、不重复计分；
- 卡在 PENDING/PROCESSING 的提交会被对账关闭，空壳提交被清理（否则该题永久锁死）；
- 全部答完的会话恢复为 FINISHED；
- 没有题目记录时明确报错，不伪造成功。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import StateRecoveryRequiredError
from app.graph import build_interview_graph
from app.models import Base
from app.repositories import answers as answers_repo
from app.repositories import sessions as sessions_repo
from app.repositories import visitors as visitors_repo
from app.schemas.questions import GeneratedQuestion, RubricPoint
from app.services import graph_runtime, interview_workflow, recovery


class FakeLock:
    def __init__(self) -> None:
        self.lost = False

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
            content=f"第 {i} 题：请说明你在项目里做过的第 {i} 个技术取舍及量化结果。",
            material_evidence_ids=["ev-1"],
            evaluation_rubric=[
                RubricPoint(point="讲清权衡", weight=0.5),
                RubricPoint(point="量化结果", weight=0.5),
            ],
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
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    visitor, _ = visitors_repo.create(db)
    db.commit()
    session = sessions_repo.create(db, owner_id=visitor.id, question_count=3)
    session.status = "READY"
    db.commit()

    monkeypatch.setattr(interview_workflow.locks, "acquire", lambda *a, **k: FakeLock())
    monkeypatch.setattr(recovery.locks, "acquire", lambda *a, **k: FakeLock())

    saver = MemorySaver()
    graph = build_interview_graph(checkpointer=saver)
    started = interview_workflow.start_interview(
        db=db, graph=graph, session=session, questions=_questions(3)
    )
    try:
        yield SimpleNamespace(
            db=db, graph=graph, saver=saver, session=session,
            visitor=visitor, started=started,
        )
    finally:
        db.close()
        engine.dispose()


def _submit(env, graph, question_id, request_id=None):
    return interview_workflow.submit_answer(
        db=env.db,
        graph=graph,
        session_id=env.session.id,
        owner_id=env.visitor.id,
        question_id=question_id,
        request_id=request_id or f"req-{uuid.uuid4()}",
        answer="我做了取舍，并给出了量化结果。",
        redis_client=None,
    )


def _lost_checkpoint_graphs():
    """模拟 Redis 丢失：全新 saver，正常图 + 恢复变体共用它。"""
    saver = MemorySaver()
    return build_interview_graph(checkpointer=saver), build_interview_graph(checkpointer=saver, restore=True), saver


def test_recover_stops_at_followup_and_keeps_history(env):
    first = _submit(env, env.graph, env.started["question"]["question_id"])
    assert first["next_question"]["kind"] == "follow_up"  # mock 会建议追问一次

    graph2, restore_graph, saver2 = _lost_checkpoint_graphs()
    assert graph_runtime.has_checkpoint(graph2, env.session.id) is False

    result = recovery.recover_session(db=env.db, restore_graph=restore_graph, session=env.session)
    assert result["recovered"] is True
    assert result["question"]["main_index"] == 1
    assert result["question"]["follow_up_index"] == 1
    assert result["question"]["content"] == first["next_question"]["content"]
    assert "evaluation_rubric" not in result["question"]

    # 图真的停在等待点（不是"塞了字段就宣布恢复成功"）
    values, next_nodes, waiting = graph_runtime.snapshot_state(graph2, env.session.id)
    assert waiting is True and next_nodes == ("wait_answer",)
    assert len(values["evaluations"]) == 1  # 历史评分从数据库重建
    assert values["current_followup_index"] == 1  # 追问次数不清零（否则会超过上限）

    # 继续答题：用同一个 request 之外的正常提交，历史不丢
    res = _submit(env, graph2, result["question"]["question_id"])
    assert res["evaluation"] is not None
    values, _, _ = graph_runtime.snapshot_state(graph2, env.session.id)
    assert len(values["evaluations"]) == 2
    assert saver2 is not None


def test_recover_reconciles_unfinished_and_unblocks_question(env):
    # 先答完第 1 题的主问题与追问，让等待点落到第 2 题
    r1 = _submit(env, env.graph, env.started["question"]["question_id"])
    r2 = _submit(env, env.graph, r1["next_question"]["question_id"])
    assert r2["next_question"]["main_index"] == 2

    q2 = r2["next_question"]
    # 伪造一条"提交到一半"的记录（模拟进程被杀）
    ghost = answers_repo.create(
        env.db,
        session_id=env.session.id,
        question_id=q2["question_id"],
        request_id="req-ghost",
        input_hash="deadbeef",
        submission_kind="answer",
        answer_content="这次提交没有跑完",
    )
    answers_repo.mark_stage(env.db, ghost, "GRAPH_RUNNING")
    env.db.commit()

    graph2, restore_graph, _ = _lost_checkpoint_graphs()
    result = recovery.recover_session(db=env.db, restore_graph=restore_graph, session=env.session)

    assert result["question"]["question_id"] == q2["question_id"]
    assert result["reconciled"] and result["reconciled"][0]["answer_id"] == str(ghost.id)
    # 空壳提交被清理，这道题才不会被 (session_id, question_id) 唯一约束锁死
    assert result["dropped_empty_attempts"] == 1
    assert answers_repo.get(env.db, ghost.id) is None

    res = _submit(env, graph2, q2["question_id"], request_id="req-new")
    assert res["answer_id"] != str(ghost.id)
    assert res["evaluation"] is not None


def test_recover_all_answered_marks_finished(env):
    current = env.started["question"]
    for _ in range(10):
        res = _submit(env, env.graph, current["question_id"])
        if res["finished"]:
            break
        current = res["next_question"]
    else:
        pytest.fail("未能走到结束")

    # 模拟"答完但状态没落好"的窗口：结论仍应由记录推出
    env.session.status = "IN_PROGRESS"
    env.db.commit()

    _, restore_graph, _ = _lost_checkpoint_graphs()
    result = recovery.recover_session(db=env.db, restore_graph=restore_graph, session=env.session)
    assert result["finished"] is True
    assert result["question"] is None
    assert env.db.get(type(env.session), env.session.id).status == "FINISHED"


def test_recover_without_questions_fails_loudly(env):
    empty = sessions_repo.create(env.db, owner_id=env.visitor.id)
    env.db.commit()
    _, restore_graph, _ = _lost_checkpoint_graphs()
    with pytest.raises(StateRecoveryRequiredError):
        recovery.recover_session(db=env.db, restore_graph=restore_graph, session=empty)


def test_recover_after_finish_is_noop(env):
    env.session.status = "FINISHED"
    env.db.commit()
    _, restore_graph, _ = _lost_checkpoint_graphs()
    result = recovery.recover_session(db=env.db, restore_graph=restore_graph, session=env.session)
    assert result["finished"] is True and result["recovered"] is False
