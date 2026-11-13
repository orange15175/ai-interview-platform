"""健康检查：启动自检与联调排障的第一入口。

原则（§11）：返回内容全部脱敏——模型名、配置状态、组件连通性；
绝不包含 API key、连接串密码、简历内容。

状态语义（否则这个接口没有意义）：
- 组件全部可用 → 200 + "ok"；
- 任一组件不可用 → 503 + "degraded"，并在 unavailable 里列出是谁挂了。
之前无论死活都返回 200 + "ok"，负载均衡会把流量继续打进来，
运维看到绿灯其实服务已经写不进任何数据。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.db import get_engine
from app.infrastructure.redis_client import ping_redis

router = APIRouter(tags=["health"])


def _db_ok() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 —— 健康检查要吞掉一切异常只报状态
        return False


def _redis_ok() -> bool:
    try:
        return bool(ping_redis())
    except Exception:  # noqa: BLE001
        return False


def _snapshot() -> dict:
    db_ok = _db_ok()
    redis_ok = _redis_ok()
    unavailable = [name for name, ok in (("db", db_ok), ("redis", redis_ok)) if not ok]
    return {
        "status": "ok" if not unavailable else "degraded",
        "llm": get_settings().describe(),
        "db": "ok" if db_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
        # 只给组件名，不给异常细节（连接串可能带密码）
        "unavailable": unavailable,
    }


@router.get("/health", summary="健康检查（脱敏，组件不可用时 503）")
def health() -> JSONResponse:
    body = _snapshot()
    return JSONResponse(status_code=200 if body["status"] == "ok" else 503, content=body)


@router.get("/health/live", summary="存活探针：进程活着就 200，不查依赖")
def health_live() -> dict:
    """liveness：只回答"进程还在吗"，不因数据库抖动而重启容器。"""
    return {"status": "alive"}
