"""幂等判定纯函数（开发说明 §9.3 第 1~2 条）。

这些判定决定「重试 / 冲突 / 继续 / 正常处理」，是可靠性里最该被测死的部分：
判错一个分支，要么重复计分，要么把用户锁死在提交页。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.idempotency import classify_submission, input_fingerprint


def _row(*, input_hash: str, question_id, status: str):
    return SimpleNamespace(
        input_hash=input_hash, question_id=question_id, processing_status=status
    )


def test_no_existing_row_proceeds():
    qid = uuid.uuid4()
    check = classify_submission(None, input_hash="h", question_id=qid)
    assert check.action == "proceed"


def test_same_request_id_same_input_completed_replays():
    qid = uuid.uuid4()
    row = _row(input_hash="h", question_id=qid, status="COMPLETED")
    assert classify_submission(row, input_hash="h", question_id=qid).action == "replay"


def test_same_request_id_different_input_conflicts():
    qid = uuid.uuid4()
    row = _row(input_hash="h1", question_id=qid, status="COMPLETED")
    assert classify_submission(row, input_hash="h2", question_id=qid).action == "conflict"


def test_same_request_id_different_question_conflicts():
    row = _row(input_hash="h", question_id=uuid.uuid4(), status="COMPLETED")
    assert classify_submission(row, input_hash="h", question_id=uuid.uuid4()).action == "conflict"


def test_unfinished_same_request_resumes():
    """上一次处理失败（评分成功但追问失败等）→ 复用已存阶段结果继续。"""
    qid = uuid.uuid4()
    for status in ("PENDING", "PROCESSING", "FAILED"):
        row = _row(input_hash="h", question_id=qid, status=status)
        assert classify_submission(row, input_hash="h", question_id=qid).action == "resume"


def test_fingerprint_ignores_whitespace_but_not_content():
    a = input_fingerprint(answer="我用了  Redis\n做缓存", skipped=False)
    b = input_fingerprint(answer="我用了Redis 做缓存  ", skipped=False)
    c = input_fingerprint(answer="我用了 Memcached 做缓存", skipped=False)
    assert a == b
    assert a != c


def test_fingerprint_distinguishes_skip_from_empty_answer():
    """空回答与"明确跳过"是两件事（§必测边界）。"""
    assert input_fingerprint(answer="", skipped=True) != input_fingerprint(answer="", skipped=False)
