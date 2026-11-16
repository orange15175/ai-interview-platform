"""失败提交的补偿策略：错误分类 + 退避 + 抢占（§五）。

这一层全是纯函数与常量，不碰数据库也不碰模型，因为"哪些错误该重试"
是整个补偿机制里最容易想当然写错的地方：

- 把 MODEL_TIMEOUT 判成不可重试 → 用户明明能恢复，却被要求重答一遍；
- 把 UNREADABLE_MATERIAL 判成可重试 → 后台每 30 秒烧一次模型费用，
  重试四次还是同一个"文件读不出来"；
- 把 STATE_RECOVERY_REQUIRED 直接重投 → 盲跑模型，评分记到错题上。

所以分类先于实现：先给每个错误码一个归宿，再谈调度。
"""

from __future__ import annotations

from typing import Literal

# 后台补偿最多尝试次数：4 次之后一律 retryable=False，等用户手动重试。
# 上限的意义不是"省钱"，而是"别让一个永远失败的会话在队列里转一辈子"。
MAX_ATTEMPTS = 4

# 退避档位（秒）：第 1 次失败 30 秒后重试，第 2 次 2 分钟，第 3 次 10 分钟，
# 第 4 次 30 分钟。档位按 attempt_count 取，不用随机抖动——单人会话不存在
# 惊群问题，可预测的节奏更好排查。
BACKOFF_SECONDS: tuple[int, ...] = (30, 120, 600, 1800)

FailureKind = Literal["retryable", "recoverable", "fatal", "conflict"]

# 可重试：模型侧的瞬时问题。保留回答、写 FAILED、进补偿队列。
RETRYABLE_CODES = frozenset({
    "MODEL_TIMEOUT",
    "MODEL_UNAVAILABLE",
    "MODEL_OUTPUT_INVALID",
    "MODEL_REPAIR_FAILED",
})

# 需恢复：状态对不上，必须先恢复会话或重新对账，**不能**盲目重投模型。
RECOVERABLE_CODES = frozenset({
    "STATE_RECOVERY_REQUIRED",
    "WRITE_FENCE_LOST",
    "VERSION_CONFLICT",
})

# 业务冲突：客户端拿着过期题目/改过的请求来提交。重试只会得到同一个答案。
CONFLICT_CODES = frozenset({
    "REQUEST_CONFLICT",
    "STALE_QUESTION",
    "SESSION_FINISHED",
    "SESSION_EXPIRED",
    "SESSION_NOT_FOUND",
})

# 其余（INVALID_REQUEST / UNREADABLE_MATERIAL / MATERIAL_INSUFFICIENT /
# INTERNAL_ERROR …）一律按不可重试处理：未知错误进重试队列，等于把一个 bug
# 变成四次模型费用，还可能掩盖真正的故障。

# 降级级别（写进 interview_answers.degradation_level）
DEGRADATION_EVALUATION_FAILED = "EVALUATION_FAILED"   # 核心结果缺失：无分数、不推进
DEGRADATION_FOLLOWUP_FAILED = "FOLLOWUP_FAILED"       # 已有评分，追问没生成出来（会重试）
DEGRADATION_FOLLOWUP_SKIPPED = "FOLLOWUP_SKIPPED"     # 追问彻底失败，放弃追问继续主流程


def classify(code: str | None) -> FailureKind:
    """把错误码分成可重试、需要恢复、冲突和致命四类，未知错误不自动重试。"""
    if not code:
        return "fatal"
    if code in RETRYABLE_CODES:
        return "retryable"
    if code in RECOVERABLE_CODES:
        return "recoverable"
    if code in CONFLICT_CODES:
        return "conflict"
    return "fatal"


def is_retryable(code: str | None) -> bool:
    """判断错误是否属于适合自动重试的模型瞬时故障。"""
    return classify(code) == "retryable"


def backoff_seconds(attempt_count: int) -> int | None:
    """按已尝试次数选择下一次等待秒数，达到次数上限就返回 None。

    attempt_count 是**后台已经试过**的次数：0 表示只在用户请求里失败过一次
    （30 秒后由后台接手），4 表示已经自动试满（不再自动试）。"""
    if attempt_count >= MAX_ATTEMPTS:
        return None
    return BACKOFF_SECONDS[min(max(attempt_count, 0), len(BACKOFF_SECONDS) - 1)]


def next_attempt_allowed(attempt_count: int) -> bool:
    """检查剩余重试次数，决定是否还允许后台再试一次。"""
    return attempt_count < MAX_ATTEMPTS


def degradation_level_for_failure(code: str | None, *, has_evaluation: bool) -> str | None:
    """按失败阶段标出降级层级，让用户知道评分或追问受到了什么影响。

    评分失败与追问失败必须分开：前者是核心结果缺失（没有分数、不许推进下一题），
    后者只是少问一句（已经有分数了，重试只重跑追问）。混成同一个级别，
    前端就没法决定"是让用户重答，还是静默续跑"。"""
    if classify(code) not in ("retryable", "recoverable"):
        return None
    return (
        DEGRADATION_FOLLOWUP_FAILED if has_evaluation else DEGRADATION_EVALUATION_FAILED
    )
