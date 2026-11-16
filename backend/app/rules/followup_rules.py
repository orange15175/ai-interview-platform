"""追问规则链（开发说明 §7.2）——纯 Python，不调模型。

链的顺序（文档规定，不得随意调换）：
    1. 跳过检查   —— 候选人跳过本题 → 直接停止（且上一节点根本不会调评分模型）
    2. 次数限制   —— 本主问题的追问次数已达上限 → 停止
    3. 模型建议   —— 评分模型说不需要追问 → 停止
    4. 目标有效   —— 建议追问但没给出方向（follow_up_target）→ 停止
    5. 重复检查   —— 建议的方向此前已追问过 → 停止
    6. 放行       —— ask

为什么用规则链而不是"模型说追问就追问"：
- 防死循环：模型永远可以说"再问问"，规则链是硬闸；
- 可解释：每次停止都有 reason_code，前端和日志能说清"为什么没追问"；
- 可测试：纯函数，不依赖任何模型行为。

参数集中可配（BusinessDefaults / 调用方覆盖），改参数不改代码。
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.schemas.evaluation import Evaluation
from app.schemas.followup import (
    REASON_DUPLICATE_TARGET,
    REASON_FOLLOWUP_LIMIT,
    REASON_NO_TARGET,
    REASON_NOT_SUGGESTED,
    REASON_SKIPPED,
    REASON_SUGGESTED,
    FollowupDecision,
)

log = get_logger(__name__)

# 当前规则链的版本号。修改链顺序、增删规则、改默认参数时必须改这里。
RULE_VERSION = "followup-rules-v1"


def decide_followup(
    *,
    evaluation: Evaluation | None,
    skipped: bool,
    followup_count: int,
    asked_targets: list[str],
    max_followups: int,
    require_target: bool = True,
    rule_version: str = RULE_VERSION,
) -> FollowupDecision:
    """按固定顺序检查跳过、次数、分数和重复方向，决定是否追问；不调用模型。

    参数：
        evaluation:     本题评分结果；skipped=True 时为 None
        skipped:        候选人是否跳过了本题
        followup_count: 本主问题已追问的次数（0 起）
        asked_targets:  本主问题此前追问已覆盖的方向（follow_up_target 列表）
        max_followups:  次数上限（业务默认 2，
        require_target: True 时无 target 的建议一律拒绝（防"为追问而追问"）"""
    # 1. 跳过：没有评分就没有依据，不追问
    if skipped or evaluation is None:
        return _stop(REASON_SKIPPED, "候选人跳过本题，无评分依据，不追问")

    # 2. 次数硬限制：这是防死循环的主闸门，永远先于模型建议
    if followup_count >= max_followups:
        return _stop(
            REASON_FOLLOWUP_LIMIT,
            f"本主问题已追问 {followup_count} 次，达到上限 {max_followups}",
        )

    # 3. 模型建议：模型说不需要就停
    if not evaluation.follow_up_suggested:
        return _stop(REASON_NOT_SUGGESTED, "评分未建议追问")

    # 4. 目标有效：说不出追问什么方向的，不放行
    target = (evaluation.follow_up_target or "").strip()
    if require_target and not target:
        return _stop(REASON_NO_TARGET, "建议追问但未给出方向，不生成追问")

    # 5. 重复检查：同方向的问题问第二遍没有信息增量
    if target and target in asked_targets:
        return _stop(REASON_DUPLICATE_TARGET, f"追问方向「{target}」已覆盖过")

    # 6. 放行
    return FollowupDecision(
        decision="ask",
        reason_code=REASON_SUGGESTED,
        rule_version=rule_version,
        detail=f"建议追问方向：{target}" if target else "建议追问（无方向标注）",
    )


def _stop(reason: str, detail: str) -> FollowupDecision:
    """统一构造停止追问的结果，附上原因码和当前规则版本。"""
    log.info("追问规则链停止：%s - %s", reason, detail)
    return FollowupDecision(
        decision="stop",
        reason_code=reason,
        rule_version=RULE_VERSION,
        detail=detail,
    )
