"""追问规则链单元测试（开发说明 §7.2）。

规则链是纯函数：每种 reason_code 一条用例，外加参数开关的行为。
"""

from __future__ import annotations

import pytest

from app.rules.followup_rules import RULE_VERSION, decide_followup
from app.schemas.evaluation import Evaluation
from app.schemas.followup import (
    REASON_DUPLICATE_TARGET,
    REASON_FOLLOWUP_LIMIT,
    REASON_NO_TARGET,
    REASON_NOT_SUGGESTED,
    REASON_SKIPPED,
    REASON_SUGGESTED,
)


def _eval(*, suggested=True, target="项目并发量级") -> Evaluation:
    return Evaluation(
        score=72,
        feedback="回答覆盖主线，但缺少量化细节",
        follow_up_suggested=suggested,
        follow_up_target=target if suggested else None,
    )


def test_skipped_stops_first():
    d = decide_followup(evaluation=None, skipped=True, followup_count=0,
                        asked_targets=[], max_followups=2)
    assert d.decision == "stop"
    assert d.reason_code == REASON_SKIPPED


def test_missing_evaluation_stops_as_skipped():
    d = decide_followup(evaluation=None, skipped=False, followup_count=0,
                        asked_targets=[], max_followups=2)
    assert d.decision == "stop"
    assert d.reason_code == REASON_SKIPPED


def test_followup_limit_beats_model_suggestion():
    """次数限制优先于模型建议：就算模型想追问，到上限也得停。"""
    d = decide_followup(evaluation=_eval(), skipped=False, followup_count=2,
                        asked_targets=[], max_followups=2)
    assert d.decision == "stop"
    assert d.reason_code == REASON_FOLLOWUP_LIMIT


def test_not_suggested_stops():
    d = decide_followup(evaluation=_eval(suggested=False), skipped=False,
                        followup_count=0, asked_targets=[], max_followups=2)
    assert d.decision == "stop"
    assert d.reason_code == REASON_NOT_SUGGESTED


def test_no_target_stops():
    d = decide_followup(evaluation=_eval(suggested=True, target="  "),
                        skipped=False, followup_count=0,
                        asked_targets=[], max_followups=2)
    assert d.decision == "stop"
    assert d.reason_code == REASON_NO_TARGET


def test_no_target_allowed_when_requirement_off():
    d = decide_followup(evaluation=_eval(suggested=True, target=""),
                        skipped=False, followup_count=0,
                        asked_targets=[], max_followups=2,
                        require_target=False)
    assert d.decision == "ask"


def test_duplicate_target_stops():
    d = decide_followup(evaluation=_eval(), skipped=False, followup_count=1,
                        asked_targets=["项目并发量级"], max_followups=2)
    assert d.decision == "stop"
    assert d.reason_code == REASON_DUPLICATE_TARGET


def test_all_clear_asks():
    d = decide_followup(evaluation=_eval(), skipped=False, followup_count=1,
                        asked_targets=["别的方向"], max_followups=2)
    assert d.decision == "ask"
    assert d.reason_code == REASON_SUGGESTED


def test_every_decision_carries_rule_version():
    for d in [
        decide_followup(evaluation=None, skipped=True, followup_count=0,
                        asked_targets=[], max_followups=2),
        decide_followup(evaluation=_eval(), skipped=False, followup_count=0,
                        asked_targets=[], max_followups=2),
    ]:
        assert d.rule_version == RULE_VERSION
        assert d.rule_version  # 非空
