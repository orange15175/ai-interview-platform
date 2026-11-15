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
from app.services import graph_runtime, interview_workflow


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


def _answer_all(env):
    """答完所有题，返回 [(question, response, request_id)]。"""
    current = env.started["question"]
    seen = []
    for i in range(12):
        req = f"req-all-{i}"
        res = _submit(env, question_id=current["question_id"], request_id=req)
        seen.append((current, res, req))
        if res["finished"]:
            return seen
        current = res["next_question"]
    raise AssertionError("提交次数超过上限，疑似死循环")


def test_replay_after_finished_still_returns_original_response(env):
    """最后一题提交成功、响应丢了：重试必须回放，不能回 SESSION_FINISHED。

    这是"终态检查排在幂等检查之前"会造成的真实故障：会话已经 FINISHED，
    用户手里那条评分就永远拿不回来了。
    """
    seen = _answer_all(env)
    last_question, last_res, last_req = seen[-1]
    assert last_res["finished"] is True

    env.db.refresh(env.session)
    assert env.session.status == "FINISHED"

    again = interview_workflow.submit_answer(
        db=env.db,
        graph=env.graph,
        session_id=env.session.id,
        owner_id=env.visitor.id,
        question_id=last_question["question_id"],
        request_id=last_req,
        answer="我做了取舍并给出量化结果。",
        redis_client=None,
    )
    assert again["replayed"] is True
    assert again["answer_id"] == last_res["answer_id"]
    assert again["evaluation"]["score"] == last_res["evaluation"]["score"]
    assert again["state_version"] == last_res["state_version"]

    # 只有**新的** request_id 才允许拿到 SESSION_FINISHED
    from app.core.errors import SessionFinishedError

    with pytest.raises(SessionFinishedError):
        _submit(env, question_id=last_question["question_id"])


def test_graph_advanced_but_db_write_lost_does_not_skip_question(env):
    """图已推进、数据库没写上：同一 request_id 重试只能补写，不能跳题。

    以前这里会把旧回答当成下一位等待点的输入喂进去，结果一题被"吃"掉：
    候选人看到的是第 3 题，实际已经答到第 4 题。
    """
    res = _submit(env, request_id="req-lost")
    q1 = env.started["question"]
    assert res["next_question"]["question_id"] != q1["question_id"]

    # 还原成"完成写入整段丢了"：行还是 PENDING、无评分、无快照，会话没推进
    row = answers_repo.get_by_request(env.db, env.session.id, "req-lost")
    row.processing_status = "PENDING"
    row.processing_stage = "GRAPH_RUNNING"
    row.evaluation = None
    row.rule_decision = None
    row.score = None
    row.response_snapshot = None
    row.completed_at = None
    env.session.state_version = 1
    env.session.current_question_id = uuid.UUID(q1["question_id"])
    env.session.status = "IN_PROGRESS"
    env.db.commit()

    retry = _submit(env, question_id=q1["question_id"], request_id="req-lost")

    assert retry["replayed"] is False
    assert retry.get("rebuilt") is True
    assert retry["evaluation"]["score"] == res["evaluation"]["score"]
    # 关键：下一题还是原来那道，没有被"吃掉"一题
    assert retry["next_question"]["question_id"] == res["next_question"]["question_id"]

    row = answers_repo.get_by_request(env.db, env.session.id, "req-lost")
    assert row.processing_status == "COMPLETED"
    assert row.response_snapshot["next_question"]["question_id"] == retry["next_question"]["question_id"]

    # 图没有被再喂一遍：评分只追加过一次
    values, _, waiting = graph_runtime.snapshot_state(env.graph, env.session.id)
    assert waiting is True
    assert len(values["evaluations"]) == 1


def test_lock_lost_before_write_does_not_persist(env, monkeypatch):
    """锁租约失效：结果不许写入，且必须能用同一个 request_id 重试回来。"""
    lock = FakeLock()

    def acquire(*args, **kwargs):
        lock.lost = True  # 模型跑完时租约已经丢了
        return lock

    monkeypatch.setattr(interview_workflow.locks, "acquire", acquire)

    with pytest.raises(VersionConflictError):
        _submit(env, request_id="req-lock")

    row = answers_repo.get_by_request(env.db, env.session.id, "req-lock")
    assert row is not None and row.processing_status != "COMPLETED"
    assert row.response_snapshot is None
    env.db.refresh(env.session)
    assert env.session.state_version == 1  # 版本没被推进

    # 恢复真正的锁后重试：图已推进，只能补写（不跳题）
    monkeypatch.setattr(interview_workflow.locks, "acquire", lambda *a, **k: FakeLock())
    retry = _submit(env, request_id="req-lock")
    assert retry["replayed"] is False
    assert retry.get("rebuilt") is True
    assert answers_repo.get(env.db, retry["answer_id"]).processing_status == "COMPLETED"


def test_lock_unavailable_writes_nothing(env, monkeypatch):
    """Redis 不可用：宁可 503，也绝不无锁写入。"""
    from app.core.errors import LockUnavailableError

    def boom(*args, **kwargs):
        raise LockUnavailableError()

    monkeypatch.setattr(interview_workflow.locks, "acquire", boom)

    with pytest.raises(LockUnavailableError):
        _submit(env, request_id="req-nolock")

    assert answers_repo.get_by_request(env.db, env.session.id, "req-nolock") is None
    env.db.refresh(env.session)
    assert env.session.state_version == 1


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


# ---------------------------------------------------------------- 分层降级


def test_followup_output_invalid_is_skipped_and_recorded(env, monkeypatch):
    """追问输出非法 → 跳过追问进下一道主问题，并留下降级痕迹。

    这是"可选体验降级"：分已经拿到了，只是少问一句。所以流程必须继续，
    记录必须是 COMPLETED（不是 FAILED），但 degradation_level 要写下来，
    否则"为什么这道题没有追问"永远查不出原因。
    """
    import app.graph.nodes as nodes
    from app.core.errors import ModelOutputInvalidError

    def bad_followup(*args, **kwargs):
        raise ModelOutputInvalidError("追问输出不是合法结构")

    monkeypatch.setattr(nodes, "generate_followup", bad_followup)

    res = _submit(env)

    assert res["followup_decision"]["reason_code"] == "FOLLOWUP_FAILED"
    # 没有追问题，直接推进到下一道主问题（不重复问已回答过的题）
    assert res["next_question"] is not None
    assert res["next_question"]["kind"] == "main"
    assert res["next_question"]["main_index"] == 2
    assert res["degradation"]["level"] == "FOLLOWUP_SKIPPED"

    row = answers_repo.get(env.db, res["answer_id"])
    assert row.processing_status == "COMPLETED"
    assert row.evaluation is not None  # 评分没丢
    assert row.degradation_level == "FOLLOWUP_SKIPPED"
    assert row.degradation_reason
    assert row.retryable is False  # 降级不是失败，不该进补偿队列


def test_evaluation_failure_keeps_answer_and_does_not_advance(env, monkeypatch):
    """评分失败是核心结果缺失：保留回答、不产生分数、不推进下一题。"""
    import app.graph.nodes as nodes
    from app.core.errors import ModelCallError

    def boom(*args, **kwargs):
        raise ModelCallError("模型超时")

    monkeypatch.setattr(nodes, "evaluate_answer", boom)

    with pytest.raises(ModelCallError):
        _submit(env, request_id="req-eval-fail")

    row = answers_repo.get_by_request(env.db, env.session.id, "req-eval-fail")
    assert row is not None
    assert row.answer_content  # 用户原文保留，重试要用
    assert row.processing_status == "FAILED"
    assert row.score is None and row.evaluation is None  # 绝不用 0 分冒充答错
    assert row.response_snapshot["error"]["code"] == "MODEL_TIMEOUT"
    assert row.retryable is True  # 可被后台补偿接手
    assert row.degradation_level == "EVALUATION_FAILED"
    assert row.model_name is None

    env.db.refresh(env.session)
    assert env.session.status == "IN_PROGRESS"
    # 关键：题号没有前进（失败不推进），当前题还是刚才那道
    assert str(env.session.current_question_id) == env.started["question"]["question_id"]


def test_late_write_after_lock_takeover_is_rejected(env, monkeypatch):
    """模型调用期间锁被接管：本次结果一条都不许写（write_epoch 迟到写入）。

    场景：A 拿了锁开始跑模型，租约期间 A 卡住；B 拿到锁推进了会话。
    A 跑完回来时，它算出来的结果**即使是对的也不能落库**——一旦落库，
    用户会看到"第 2 题刚答完，题号却被 B 撤销"的错乱。
    """
    from app.core.errors import WriteFenceLostError
    from app.services import graph_runtime as gr

    original = gr.resume_answer

    def takeover(graph, session_id, payload):
        result = original(graph, session_id, payload)
        # B 接管：epoch 递增。A 的栅栏从此刻起作废。
        sessions_repo.begin_write(env.db, env.session)
        return result

    monkeypatch.setattr(gr, "resume_answer", takeover)

    with pytest.raises(WriteFenceLostError):
        _submit(env, request_id="req-late")

    # 回答没有被标成完成，也没有写响应快照
    row = answers_repo.get_by_request(env.db, env.session.id, "req-late")
    assert row is not None
    assert row.processing_status != "COMPLETED"
    assert row.score is None

    # 会话没有被推进：题号还是第一题
    env.db.refresh(env.session)
    assert str(env.session.current_question_id) == env.started["question"]["question_id"]
    assert env.session.state_version == 1
