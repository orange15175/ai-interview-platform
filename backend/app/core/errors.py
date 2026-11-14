"""领域错误与稳定错误码（开发说明 §10 错误码清单）。

所有对外错误都带 code：前端按 code 分支，message 直接展示给用户。
不要把堆栈或模型原始输出塞进 message（§11 日志与隐私）。
"""

from __future__ import annotations


class AppError(Exception):
    """业务错误基类：code 稳定、http_status 由 API 层沿用。"""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(self, message: str, *, code: str | None = None,
                 http_status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        self.retryable = retryable

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class MaterialInsufficientError(AppError):
    """材料读出来了，但内容不足以出题（没有教育/经历/项目）。

    要求用户补充，而不是硬编题目。
    """

    code = "MATERIAL_INSUFFICIENT"
    http_status = 422

    def __init__(self, message: str = "简历内容不足，无法出题，请换一份内容完整的简历"):
        super().__init__(message)


class UnreadableMaterialError(MaterialInsufficientError):
    """材料根本读不出来（空文档 / 损坏 / 完全没有可识别内容）。

    与 MATERIAL_INSUFFICIENT 的区别必须保留：一个是"文件没内容"，一个是
    "读出来了但没有经历可问"。混成一条错误码，用户不知道该换文件还是补内容。
    继承同一个基类，是为了让"材料有问题"的统一捕获仍然有效。
    """

    code = "UNREADABLE_MATERIAL"

    def __init__(self, message: str = "没有可解析的材料，请重新上传简历"):
        super().__init__(message)


class ModelCallError(AppError):
    """模型调用失败（超时/网络/供应商 5xx）。可重试，绝不当成"答错"。"""

    code = "MODEL_TIMEOUT"
    http_status = 502

    def __init__(self, message: str = "模型服务暂时不可用，请稍后重试",
                 *, code: str | None = None):
        super().__init__(message, retryable=True, code=code)


class ModelTimeoutError(ModelCallError):
    """模型调用超时（客户端 read timeout / 请求超时）。"""

    code = "MODEL_TIMEOUT"


class ModelUnavailableError(ModelCallError):
    """模型服务不可用（连接失败 / 限流 / 供应商 5xx）。"""

    code = "MODEL_UNAVAILABLE"


def classify_model_exception(exc: BaseException, *, message: str) -> ModelCallError:
    """把供应商/网络异常归类成稳定错误码。

    超时与"服务不可用"对用户是两件事：前者重试往往就好，后者要等运维。
    但对调用方都必须是可重试错误，绝不能变成 0 分。
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    timeout_markers = ("timeout", "timedout", "readtimeout", "apitimeouterror")
    if any(m in name for m in timeout_markers) or "timed out" in text or "超时" in text:
        return ModelTimeoutError(message)
    return ModelUnavailableError(message)


class ModelOutputInvalidError(AppError):
    """模型输出结构不合法（校验失败）。可重试，不写入任何正式记录。"""

    code = "MODEL_OUTPUT_INVALID"
    http_status = 502

    def __init__(self, message: str = "模型输出格式异常，请重试"):
        super().__init__(message, retryable=True)


# ---------------------------------------------------------------------------
# 阶段 4：归属、并发与一致性（§9 / §10 错误码）
# ---------------------------------------------------------------------------


class SessionNotFoundError(AppError):
    """会话不存在，或不属于当前访客。

    两种情况刻意合并：分开返回等于告诉攻击者"这个 ID 是存在的"（§11 归属隔离）。
    """

    code = "SESSION_NOT_FOUND"
    http_status = 404

    def __init__(self, message: str = "会话不存在，或无权访问"):
        super().__init__(message)


class SessionFinishedError(AppError):
    """会话已结束（含正常完成、主动结束、取消、过期），不再接受答题。"""

    code = "SESSION_FINISHED"
    http_status = 409

    def __init__(self, message: str = "会话已结束，不能再提交回答"):
        super().__init__(message)


class SessionExpiredError(AppError):
    """匿名访客凭证或会话超过留存期。"""

    code = "SESSION_EXPIRED"
    http_status = 410

    def __init__(self, message: str = "会话已过期，请重新创建面试"):
        super().__init__(message)


class SessionBusyError(AppError):
    """同一会话有另一个请求正在推进（会话锁未拿到）。

    可重试：客户端稍后用**同一个 request_id** 重试即可，不会产生重复记录。
    """

    code = "SESSION_BUSY"
    http_status = 409

    def __init__(self, message: str = "该会话正在处理上一条提交，请稍后重试"):
        super().__init__(message, retryable=True)


class RequestConflictError(AppError):
    """同一 request_id 携带了不同输入（客户端 bug 或重放）。

    绝不能"以最后一次为准"——那等于允许客户端改写已提交的记录（§9.3 第 2 条）。
    """

    code = "REQUEST_CONFLICT"
    http_status = 409

    def __init__(self, message: str = "该请求编号已用于另一次提交，请刷新后重试"):
        super().__init__(message)


class StaleQuestionError(AppError):
    """提交的题目已不是当前题，或该题已提交过（首期一题一次）。"""

    code = "STALE_QUESTION"
    http_status = 409

    def __init__(self, message: str = "提交的题目已过期，请刷新获取当前题"):
        super().__init__(message)


class VersionConflictError(AppError):
    """state_version 不匹配：锁租约失效后旧请求想写入（§9.2 CAS）。

    可重试，但必须重新读取当前状态——直接重放旧输入会覆盖别人的推进。
    """

    code = "VERSION_CONFLICT"
    http_status = 409

    def __init__(self, message: str = "会话状态已被更新，请刷新后重试"):
        super().__init__(message, retryable=True)


class StateRecoveryRequiredError(AppError):
    """Redis checkpoint 缺失/损坏，需要用恢复入口从 PostgreSQL 重建（§9.4）。

    明确报错，绝不假装成功或从第 1 题重开（会重复计分）。
    """

    code = "STATE_RECOVERY_REQUIRED"
    http_status = 409

    def __init__(self, message: str = "会话执行状态缺失，需要从记录恢复后才能继续"):
        super().__init__(message, retryable=True)


class LockUnavailableError(AppError):
    """Redis 不可用：拿不到会话锁时**不允许**降级为无锁写入（§9.2）。

    宁可返回 503 让客户端重试，也不要两个请求同时推进同一场面试。
    """

    code = "LOCK_UNAVAILABLE"
    http_status = 503

    def __init__(self, message: str = "会话锁服务暂时不可用，请稍后重试"):
        super().__init__(message, retryable=True)


class CsrfRejectedError(AppError):
    """写操作的 Origin/Referer 与服务端不一致（§11 CSRF 防护）。

    Cookie 是自动携带的，没有这层校验，第三方页面就能替用户提交答案。
    """

    code = "CSRF_REJECTED"
    http_status = 403

    def __init__(self, message: str = "请求来源校验失败"):
        super().__init__(message)


# ---------------------------------------------------------------------------
# 账号与登录（首期：账号唯一 + 密码登录）
# ---------------------------------------------------------------------------


class AccountTakenError(AppError):
    """账号已被占用（唯一约束冲突）。"""

    code = "ACCOUNT_TAKEN"
    http_status = 409

    def __init__(self, message: str = "该账号已被注册，请换一个"):
        super().__init__(message)


class InvalidCredentialsError(AppError):
    """账号或密码错误。

    刻意不区分"账号不存在"与"密码不对"：两种情况返回同一句人话，
    否则这个接口就成了账号枚举器。
    """

    code = "INVALID_CREDENTIALS"
    http_status = 401

    def __init__(self, message: str = "账号或密码不正确"):
        super().__init__(message)


class AuthRequiredError(AppError):
    """需要登录后才能访问（未登录 / 令牌过期 / 令牌被篡改）。"""

    code = "AUTH_REQUIRED"
    http_status = 401

    def __init__(self, message: str = "请先登录"):
        super().__init__(message)
