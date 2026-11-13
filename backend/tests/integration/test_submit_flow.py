"""提交回答的可靠性契约（SQLite + MemorySaver + mock 模型，不联网、不需要 Redis）。

覆盖阶段 4 的验收项（开发说明 §13）：
- 同 request_id 重试不新增回答，响应原样回放；
- 同 request_id 换输入 / 换题 → REQUEST_CONFLICT；
- 不同 request_id 提交同一题 → STALE_QUESTION（不重复推进）；
- 期望版本过期 → VERSION_CONFLICT；
- 跳过不调评分模型，记录仍是正式记录；
- checkpoint 丢失 → 明确 STATE_RECOVERY_REQUIRED，不假装成功；
- 评分成功但追问失败 → 重试复用已存评分，不重新评分。

锁在这里用 FakeLock 替换：锁自身的分布式语义由 test_session_lock.py 用真实 Redis 验证。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import (
    ModelCallError,
    RequestConflictError,
    StaleQuestionError,
    StateRecoveryRequiredError,
    VersionConflictError,
)
from app.graph import build_interview_graph
from app.models import Base
from app.repositories import answers as answers_repo
from app.repositories import questions as questions_repo
from app.repositories import sessions as sessions_repo
from app.repositories import visitors as visitors_repo
from app.schemas.questions import GeneratedQuestion, RubricPoint
from app.services import interview_workflow


class FakeLock:
    """替身：只保留工作流会用到的三个成员。"""

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
            category=["项目经验", "专业能力", "场景解决"][i % 3],
            difficulty=["easy", "medium", "hard"][i % 3],
            content=f"第 {i} 题：请结合项目说明你做过的第 {i} 个技术决策的完整权衡过程。",
            material_evidence_ids=["ev-1"],
            evaluation_rubric=[
                RubricPoint(point="讲清背景与权衡", weight=0.5),
                RubricPoint(point="有量化结果", weight=0.5),
            ],
        )
        for i in range(1, n + 1)
    ]


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 内存库必须共用一个连接，否则每次 commit 后库就"没了"
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    visitor, _ = visitors_repo.create(db)
    db.commit()
    session = sessions_repo.create(db, owner_id=visitor.id, question_count=3)
    session.status = "READY"
    db.commit()

    monkeypatch.setattr(interview_workflow.locks, "acquire", lambda *a, **k: FakeLock())
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


def _submit(env, *, question_id=None, request_id=None, answer="我做了取舍并给出量化结果。",
            skipped=False, expected_state_version=None, graph=None):
    return interview_workflow.submit_answer(
        db=env.db,
        graph=graph or env.graph,
        session_id=env.session.id,
        owner_id=env.visitor.id,
        question_id=question_id or env.started["question"]["question_id"],
        request_id=request_id or f"req-{uuid.uuid4()}",
        answer=None if skipped else answer,
        skipped=skipped,
        expected_state_version=expected_state_version,
        redis_client=None,
    )


# ---------------------------------------------------------------- 启动


def test_start_persists_projection_and_hides_rubric(env):
    q = env.started["question"]
    assert q["question_id"] and q["total_questions"] == 3
    assert "evaluation_rubric" not in q

    env.db.refresh(env.session)
    assert env.session.status == "IN_PROGRESS"
    assert str(env.session.current_question_id) == q["question_id"]
    assert env.session.state_version == 1
    assert len(questions_repo.list_for_session(env.db, env.session.id)) == 3


# ---------------------------------------------------------------- 正常提交


def test_submit_persists_evaluation_decision_and_next_question(env):
    res = _submit(env)

    assert res["replayed"] is False
    assert isinstance(res["evaluation"]["score"], int)
    # 对外评分绝不能带 rubric_results（那是答案结构）
    assert "rubric_results" not in res["evaluation"]
    assert res["followup_decision"]["decision"] in ("ask", "stop")
    assert res["next_question"] is not None
    assert res["state_version"] == 2

    row = answers_repo.get(env.db, res["answer_id"])
    assert row.processing_status == "COMPLETED"
    assert row.evaluation["score"] == res["evaluation"]["score"]  # 内部记录保留完整评分
    assert row.rule_decision["reason_code"]
    assert row.response_snapshot["next_question"]["question_id"] == res["next_question"]["question_id"]
    assert row.result_state_version == 2

    # 追问同时落库（否则恢复时无题可重建）
    followups = [q for q in questions_repo.list_for_session(env.db, env.session.id) if q.kind == "follow_up"]
    if res["next_question"]["kind"] == "follow_up":
        assert [str(f.id) for f in followups] == [res["next_question"]["question_id"]]


def test_skip_does_not_call_evaluator(env, monkeypatch):
    calls = {"n": 0}
    import app.graph.nodes as nodes
    original = nodes.evaluate_answer

    def spy(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(nodes, "evaluate_answer", spy)
    res = _submit(env, skipped=True)

    assert calls["n"] == 0
    assert res["evaluation"] is None
    assert res["followup_decision"]["reason_code"] == "SKIPPED"
    row = answers_repo.get(env.db, res["answer_id"])
    assert row.submission_kind == "skip" and row.score is None


# ---------------------------------------------------------------- 幂等与冲突


def test_same_request_id_replays_without_new_answer(env):
    first = _submit(env, request_id="req-fixed")
    second = _submit(env, request_id="req-fixed")

    assert second["replayed"] is True
    assert second["answer_id"] == first["answer_id"]
    assert second["state_version"] == first["state_version"]
    rows = answers_repo.list_for_session(env.db, env.session.id)
    assert len(rows) == 1
    env.db.refresh(env.session)
    assert env.session.state_version == 2  # 回放不推进版本


def test_same_request_id_different_input_conflicts(env):
    _submit(env, request_id="req-fixed", answer="第一种回答")
    with pytest.raises(RequestConflictError):
        _submit(env, request_id="req-fixed", answer="完全不同的第二种回答")
    assert len(answers_repo.list_for_session(env.db, env.session.id)) == 1


def test_different_request_id_same_question_is_stale(env):
    _submit(env)
    with pytest.raises(StaleQuestionError):
        _submit(env, question_id=env.started["question"]["question_id"])
    assert len(answers_repo.list_for_session(env.db, env.session.id)) == 1


def test_stale_expected_version_is_rejected(env):
    with pytest.raises(VersionConflictError):
        _submit(env, expected_state_version=0)  # 实际已是 1
    assert len(answers_repo.list_for_session(env.db, env.session.id)) == 0


def test_answer_for_other_session_question_is_stale(env):
    other_visitor, _ = visitors_repo.create(env.db)
    other_session = sessions_repo.create(env.db, owner_id=other_visitor.id)
    env.db.commit()
    other_q = questions_repo.insert_main_questions(env.db, other_session.id, _questions(1))[0]
    env.db.commit()

    with pytest.raises(StaleQuestionError):
        _submit(env, question_id=str(other_q.id))


# ---------------------------------------------------------------- 跨存储不一致


def test_missing_checkpoint_requires_recovery(env):
    fresh_graph = build_interview_graph(checkpointer=MemorySaver())
    with pytest.raises(StateRecoveryRequiredError):
        _submit(env, graph=fresh_graph)

    rows = answers_repo.list_for_session(env.db, env.session.id)
    assert len(rows) == 1
    assert rows[0].processing_status == "FAILED"
    assert rows[0].response_snapshot["error"]["code"] == "STATE_RECOVERY_REQUIRED"


def test_followup_failure_then_retry_reuses_evaluation(env, monkeypatch):
    """评分成功后追问失败：重试必须复用评分（不重复计费、不产生第二个分数）。"""
    import app.graph.nodes as nodes

    calls = {"n": 0}
    original_eval = nodes.evaluate_answer
    original_followup = nodes.generate_followup

    def counting_eval(*args, **kwargs):
        calls["n"] += 1
        return original_eval(*args, **kwargs)

    monkeypatch.setattr(nodes, "evaluate_answer", counting_eval)

    with monkeypatch.context() as m:
        def boom(*args, **kwargs):
            raise ModelCallError()

        m.setattr(nodes, "generate_followup", boom)
        with pytest.raises(ModelCallError):
            _submit(env, request_id="req-retry")

    assert calls["n"] == 1
    row = answers_repo.get_by_request(env.db, env.session.id, "req-retry")
    assert row.processing_status == "FAILED"
    assert row.evaluation is not None  # 评分已落库，等待复用
    saved_score = row.evaluation["score"]

    # 同 request_id 重试：图从失败节点继续，评分不再调用
    res = _submit(env, request_id="req-retry")
    assert res["replayed"] is False
    assert res["evaluation"]["score"] == saved_score
    assert calls["n"] == 1
    assert answers_repo.get(env.db, res["answer_id"]).processing_status == "COMPLETED"


# ---------------------------------------------------------------- 全流程收尾


def test_answers_until_finished(env):
    """3 道主问题 + 每题 1 次 mock 追问，最终必然收尾且不越题。"""
    current = env.started["question"]
    seen = [(current["main_index"], current["follow_up_index"])]

    for _ in range(10):  # 上限保护：超过即说明死循环
        res = _submit(env, question_id=current["question_id"])
        if res["finished"]:
            break
        current = res["next_question"]
        seen.append((current["main_index"], current["follow_up_index"]))
    else:
        pytest.fail("提交次数超过上限，疑似死循环")

    assert res["finished"] is True
    assert res["next_question"] is None
    # 顺序单调：主问题 1..3，每题最多一次追问
    assert [s[0] for s in seen] == sorted(s[0] for s in seen)
    assert all(s[1] in (0, 1) for s in seen)

    env.db.refresh(env.session)
    assert env.session.status == "FINISHED"
    assert env.session.current_question_id is None
    assert env.session.completion_reason == "ALL_ANSWERED"
    completed = [a for a in answers_repo.list_for_session(env.db, env.session.id)
                 if a.processing_status == "COMPLETED"]
    assert len(completed) == len(seen)

    # 已结束的会话不能继续提交
    from app.core.errors import SessionFinishedError

    with pytest.raises(SessionFinishedError):
        _submit(env, question_id=current["question_id"])
