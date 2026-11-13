"""健康检查：启动自检与联调排障的第一入口。

原则（§11）：返回内容全部脱敏——模型名、配置状态、组件连通性；
绝不包含 API key、连接串密码、简历内容。
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.db import get_engine
from app.infrastructure.redis_client import ping_redis

router = APIRouter(tags=["health"])


def _db_status() -> str:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # noqa: BLE001 —— 健康检查要吞掉一切异常只报状态
        return f"unavailable: {type(exc).__name__}"


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "llm": settings.describe(),
        "db": _db_status(),
        "redis": "ok" if ping_redis() else "unavailable",
    }
