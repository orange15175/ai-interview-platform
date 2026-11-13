"""追问裁决契约（开发说明 §7.2）。

评分模型只给「建议」，最终要不要追问由 Python 规则链裁决。
裁决结果必须留痕：decision + reason_code + rule_version，
reason_code 是稳定枚举（前端与日志都依赖它），rule_version 用于
日后调整规则参数后区分历史决策是按哪一版规则做出的。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 保留 reason：规则链判断"值得追问"
REASON_SUGGESTED = "SUGGESTED"
# 各拦截分支的停止原因（顺序即规则链的检查顺序）
REASON_SKIPPED = "SKIPPED"            # 候选人跳过了本题，无评分可依据
REASON_FOLLOWUP_LIMIT = "FOLLOWUP_LIMIT"  # 该主问题的追问次数已用完
REASON_NOT_SUGGESTED = "NOT_SUGGESTED"     # 评分模型明确不建议追问
REASON_NO_TARGET = "NO_TARGET"        # 建议追问但没有说清追问什么方向
REASON_DUPLICATE_TARGET = "DUPLICATE_TARGET"  # 建议的追问方向已经问过了
REASON_FOLLOWUP_FAILED = "FOLLOWUP_FAILED"  # 追问生成失败且修复无效（图节点写入）


class FollowupDecision(BaseModel):
    decision: Literal["ask", "stop"]
    reason_code: str = Field(pattern="^(SUGGESTED|SKIPPED|FOLLOWUP_LIMIT|NOT_SUGGESTED|NO_TARGET|DUPLICATE_TARGET|FOLLOWUP_FAILED)$")
    # 规则版本号：参数（次数上限/是否要求 target 等）调整时必须 bump
    rule_version: str = Field(min_length=1, max_length=32)
    detail: str = Field(default="", max_length=300)
