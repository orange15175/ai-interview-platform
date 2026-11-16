"""日志：统一格式，预留脱敏钩子（开发说明 §11）。

规则：日志只允许出现 session_id / request_id / 阶段 / 耗时 / token 用量 / 脱敏错误；
禁止记录完整简历、回答内容、Base64、密钥。
"""

import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """给进程安装统一日志格式，重复调用时不再叠加处理器。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx/openai 的调试日志默认关掉，避免输出请求体（含简历内容）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """按模块名取得日志对象，让日志能定位到具体来源。"""
    return logging.getLogger(name)
