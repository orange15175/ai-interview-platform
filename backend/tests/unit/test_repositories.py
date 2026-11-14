"""仓储层契约（开发说明 §8/§9）：归属隔离、CAS、唯一约束幂等。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import SessionNotFoundError, VersionConflictError
from app.repositories import answers as answers_repo
from app.repositories import materials as materials_repo
from app.repositories import questions as questions_repo
from app.repositories import sessions as sessions_repo
from app.repositories import users as users_repo
from app.repositories import visitors as visitors_repo
from app.schemas.materials import ParsedMaterials, ResumeProfile
from app.schemas.questions import GeneratedQuestion, RubricPoint


def _q(index: int) -> GeneratedQuestion:
    return GeneratedQuestion(
        main_index=index,
        category="项目经验",
        difficulty="medium",
        content=f"第 {index} 题：请说明你在项目里做过的关键技术取舍，并给出量化结果。",
        material_evidence_ids=["ev-1"],
        evaluation_rubric=[RubricPoint(point="讲清权衡", weight=0.5), RubricPoint(point="量化结果", weight=0.5)],
    )


# ---------------------------------------------------------------- 访客


def test_visitor_cookie_roundtrip_and_forged_token(db_session):
    visitor, token = visitors_repo.get_or_create(db_session, None)
    assert token and len(visitor.token_hash) == 64

    again, new_token = visitors_repo.get_or_create(db_session, token)
    assert again.id == visitor.id
    assert new_token is None  # 有效凭证不重复下发

    forged, forged_token = visitors_repo.get_or_create(db_session, "z" * 43)
    assert forged.id != visitor.id
    assert forged_token is not None


def test_visitor_never_stores_plaintext_token(db_session):
    visitor, token = visitors_repo.get_or_create(db_session, None)
    assert visitor.token_hash != token
    assert token not in visitor.token_hash


# ---------------------------------------------------------------- 会话


def test_session_ownership_is_enforced_in_sql(db_session):
    owner, _ = visitors_repo.get_or_create(db_session, None)
    other, _ = visitors_repo.get_or_create(db_session, None)
    session = sessions_repo.create(db_session, owner_id=owner.id)

    assert sessions_repo.get_owned(db_session, session.id, owner.id).id == session.id
    with pytest.raises(SessionNotFoundError):
        sessions_repo.get_owned(db_session, session.id, other.id)
    with pytest.raises(SessionNotFoundError):
        sessions_repo.get_owned(db_session, "not-a-uuid", owner.id)


def test_logged_in_identity_ignores_visitor_cookie(db_session):
    """同一浏览器 A 登出、B 登录：B 不能顺着**不变的**访客 Cookie 读到 A 的面试。

    这是 or_(owner_id, user_id) 时代的真实越权：owner_id 是浏览器粒度，
    user_id 是账号粒度，取或等于让"换账号"变成"换不了锁"。
    """
    browser, _ = visitors_repo.get_or_create(db_session, None)
    alice = users_repo.create(db_session, account="alice", password="Passw0rd!")
    bob = users_repo.create(db_session, account="bob", password="Passw0rd!")
    db_session.commit()

    session = sessions_repo.create(db_session, owner_id=browser.id, user_id=alice.id)
    db_session.commit()

    # 账号本人：同浏览器或换设备都能读到
    assert sessions_repo.get_owned(db_session, session.id, browser.id, user_id=alice.id).id == session.id
    # 同浏览器上的另一个账号：查不到（对外统一 404，不泄露"这 ID 存在"）
    with pytest.raises(SessionNotFoundError):
        sessions_repo.get_owned(db_session, session.id, browser.id, user_id=bob.id)


def test_anonymous_cannot_read_claimed_session(db_session):
    """A 只是退出登录（访客 Cookie 不变）：匿名身份也不能再读到已绑账号的会话。"""
    browser, _ = visitors_repo.get_or_create(db_session, None)
    alice = users_repo.create(db_session, account="alice2", password="Passw0rd!")
    db_session.commit()

    claimed = sessions_repo.create(db_session, owner_id=browser.id, user_id=alice.id)
    anonymous = sessions_repo.create(db_session, owner_id=browser.id)  # user_id 为空
    db_session.commit()

    with pytest.raises(SessionNotFoundError):
        sessions_repo.get_owned(db_session, claimed.id, browser.id)
    # 匿名时期创建、还没被账号认领的会话，仍归这枚访客 Cookie
    assert sessions_repo.get_owned(db_session, anonymous.id, browser.id).id == anonymous.id


def test_state_version_is_compare_and_swap(db_session):
    owner, _ = visitors_repo.get_or_create(db_session, None)
    session = sessions_repo.create(db_session, owner_id=owner.id)
    db_session.commit()

    assert session.state_version == 0
    assert sessions_repo.bump_state_version(db_session, session) == 1
    assert sessions_repo.bump_state_version(db_session, session) == 2

    # 旧请求拿着过期的期望版本写入 → 必须被拒绝，而不是覆盖
    with pytest.raises(VersionConflictError):
        sessions_repo.bump_state_version(db_session, session, expected=0)
    assert sessions_repo.get(db_session, session.id).state_version == 2


def test_terminal_status_cannot_answer(db_session):
    from app.core.errors import SessionFinishedError

    owner, _ = visitors_repo.get_or_create(db_session, None)
    session = sessions_repo.create(db_session, owner_id=owner.id)
    session.status = "FINISHED"
    with pytest.raises(SessionFinishedError):
        sessions_repo.assert_can_answer(session)


# ---------------------------------------------------------------- 题目


def test_main_questions_insert_is_idempotent_and_hides_rubric(db_session):
    owner, _ = visitors_repo.get_or_create(db_session, None)
    session = sessions_repo.create(db_session, owner_id=owner.id)
    db_session.commit()

    first = questions_repo.insert_main_questions(db_session, session.id, [_q(1), _q(2)])
    second = questions_repo.insert_main_questions(db_session, session.id, [_q(1), _q(2)])
    db_session.commit()

    assert [str(r.id) for r in first] == [str(r.id) for r in second]
    assert len(questions_repo.list_for_session(db_session, session.id)) == 2

    public = questions_repo.public_dict(first[0])
    assert "evaluation_rubric" not in public
    assert public["question_id"] == str(first[0].id)


def test_followup_is_unique_per_index(db_session):
    owner, _ = visitors_repo.get_or_create(db_session, None)
    session = sessions_repo.create(db_session, owner_id=owner.id)
    db_session.commit()
    main = questions_repo.insert_main_questions(db_session, session.id, [_q(1)])[0]

    a = questions_repo.add_followup(
        db_session, session_id=session.id, parent=main, follow_up_index=1,
        content="请具体说明你在这一步是怎么做取舍的？",
    )
    b = questions_repo.add_followup(
        db_session, session_id=session.id, parent=main, follow_up_index=1,
        content="完全不同的追问内容也应该被忽略？",
    )
    db_session.commit()
    assert a.id == b.id
    assert a.kind == "follow_up" and a.parent_question_id == main.id
    # 追问继承主问题的评分要点（同一个考察面，只是更深）
    assert a.evaluation_rubric == main.evaluation_rubric


# ---------------------------------------------------------------- 回答


def test_answer_unique_constraints(db_session):
    owner, _ = visitors_repo.get_or_create(db_session, None)
    session = sessions_repo.create(db_session, owner_id=owner.id)
    db_session.commit()
    rows = questions_repo.insert_main_questions(db_session, session.id, [_q(1), _q(2)])
    db_session.commit()

    answers_repo.create(
        db_session, session_id=session.id, question_id=rows[0].id,
        request_id="req-1", input_hash="h1", submission_kind="answer", answer_content="回答一",
    )
    db_session.commit()

    # 同一 request_id 再插一条 → 唯一约束拦下（幂等的数据库底线）
    with pytest.raises(IntegrityError):
        answers_repo.create(
            db_session, session_id=session.id, question_id=rows[1].id,
            request_id="req-1", input_hash="h2", submission_kind="answer", answer_content="x",
        )
    db_session.rollback()

    # 同一题换 request_id → 一题一次，也被拦下
    with pytest.raises(IntegrityError):
        answers_repo.create(
            db_session, session_id=session.id, question_id=rows[0].id,
            request_id="req-2", input_hash="h3", submission_kind="answer", answer_content="y",
        )
    db_session.rollback()


def test_answer_delete_refuses_rows_with_results(db_session):
    owner, _ = visitors_repo.get_or_create(db_session, None)
    session = sessions_repo.create(db_session, owner_id=owner.id)
    db_session.commit()
    q = questions_repo.insert_main_questions(db_session, session.id, [_q(1)])[0]
    db_session.commit()

    row = answers_repo.create(
        db_session, session_id=session.id, question_id=q.id,
        request_id="req-1", input_hash="h1", submission_kind="answer", answer_content="回答",
    )
    answers_repo.save_evaluation(db_session, row, evaluation={"score": 80}, score=80)
    db_session.commit()

    with pytest.raises(ValueError):
        answers_repo.delete(db_session, row)


# ---------------------------------------------------------------- 材料


def test_material_upsert_keeps_single_row(db_session):
    owner, _ = visitors_repo.get_or_create(db_session, None)
    session = sessions_repo.create(db_session, owner_id=owner.id)
    db_session.commit()

    parsed = ParsedMaterials(resume=ResumeProfile(skills=["Python"]), warnings=["JD 未提供"])
    first = materials_repo.save_parsed(
        db_session, session_id=session.id, parsed=parsed, parse_model="qwen3.8-omni-flash"
    )
    second = materials_repo.save_parsed(
        db_session, session_id=session.id, parsed=parsed, parse_model="qwen3.8-omni-flash"
    )
    db_session.commit()

    assert first.id == second.id
    stored = materials_repo.get(db_session, session.id)
    assert stored.resume_profile["skills"] == ["Python"]
    assert stored.jd_profile is None
