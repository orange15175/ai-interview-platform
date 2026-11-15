"""补偿策略的错误分类与退避（§5.2 / §5.4）。

这些断言看起来琐碎，但它们决定了线上行为：
判错一个错误码，要么让用户白重答一遍，要么让后台每 30 秒烧一次模型费用。
"""

from __future__ import annotations

import pytest

from app.services import compensation as comp


@pytest.mark.parametrize("code", [
    "MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_OUTPUT_INVALID", "MODEL_REPAIR_FAILED",
])
def test_model_failures_are_retryable(code):
    assert comp.classify(code) == "retryable"
    assert comp.is_retryable(code) is True


@pytest.mark.parametrize("code", ["STATE_RECOVERY_REQUIRED", "WRITE_FENCE_LOST", "VERSION_CONFLICT"])
def test_state_mismatch_needs_recovery_not_blind_retry(code):
    assert comp.classify(code) == "recoverable"
    # 关键：不自动重投模型。状态对不上时重投只会把评分记到错题上。
    assert comp.is_retryable(code) is False


@pytest.mark.parametrize("code", [
    "INVALID_REQUEST", "UNREADABLE_MATERIAL", "MATERIAL_INSUFFICIENT",
    "REQUEST_CONFLICT", "STALE_QUESTION", "RECONCILED_ON_RECOVERY",
])
def test_non_retryable_errors_never_enter_queue(code):
    assert comp.is_retryable(code) is False


def test_unknown_code_is_fatal_by_default():
    """未知错误进重试队列 = 把一个 bug 变成四次模型费用，还可能掩盖故障。"""
    assert comp.classify("SOMETHING_NEW") == "fatal"
    assert comp.is_retryable("SOMETHING_NEW") is False
    assert comp.is_retryable(None) is False


def test_backoff_follows_documented_schedule():
    assert comp.backoff_seconds(0) == 30     # 首次失败 30 秒后自动接手
    assert comp.backoff_seconds(1) == 120
    assert comp.backoff_seconds(2) == 600
    assert comp.backoff_seconds(3) == 1800
    assert comp.backoff_seconds(4) is None   # 到达上限，退出队列


def test_attempt_limit_stops_auto_retry():
    assert comp.next_attempt_allowed(0) is True
    assert comp.next_attempt_allowed(3) is True
    assert comp.next_attempt_allowed(4) is False


def test_degradation_level_distinguishes_evaluation_and_followup():
    """评分失败是核心结果缺失，追问失败只是少问一句，不能混成一个级别。"""
    assert comp.degradation_level_for_failure(
        "MODEL_TIMEOUT", has_evaluation=False
    ) == comp.DEGRADATION_EVALUATION_FAILED
    assert comp.degradation_level_for_failure(
        "MODEL_TIMEOUT", has_evaluation=True
    ) == comp.DEGRADATION_FOLLOWUP_FAILED


def test_fatal_error_has_no_degradation_level():
    """材料读不出来不是"降级"，是失败——不该写进降级统计。"""
    assert comp.degradation_level_for_failure(
        "UNREADABLE_MATERIAL", has_evaluation=False
    ) is None
