"""健康检查与错误兜底的契约测试。

这两个接口平时没人看，出问题的时候却决定了系统怎么被判断：
- /api/health 挂掉还报 200 "ok" → 负载均衡继续打流量，运维看到绿灯但服务写不进数据；
- 未捕获异常返回纯文本 → 前端 res.json() 解析失败，用户只看到 "HTTP_500"。
所以它们的行为同样值得钉住。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import health as health_api
from app.main import app


def test_health_ok_when_dependencies_alive():
    c = TestClient(app)
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["unavailable"] == []
    assert body["db"] == "ok" and body["redis"] == "ok"


def test_health_reports_503_when_db_down(monkeypatch):
    """任何一个依赖挂了都必须 503，否则这个接口等于说谎。"""
    monkeypatch.setattr(health_api, "_db_ok", lambda: False)
    c = TestClient(app)
    r = c.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] == "unavailable"
    assert body["unavailable"] == ["db"]
    # 脱敏：错误细节不能出现在响应里
    assert "password" not in r.text.lower() and "interview:" not in r.text


def test_health_live_does_not_depend_on_infra(monkeypatch):
    """存活探针不能因为数据库抖动就报死（否则容器会被无谓重启）。"""
    monkeypatch.setattr(health_api, "_db_ok", lambda: False)
    monkeypatch.setattr(health_api, "_redis_ok", lambda: False)
    r = TestClient(app).get("/api/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


def test_unhandled_exception_returns_json_contract():
    """未捕获异常也要返回 {code,message,retryable}，且不泄露堆栈与内部细节。"""
    from app.main import _unhandled_error_handler

    class _FakeRequest:
        method = "GET"

        class url:  # noqa: N801 —— 只要 .path 属性
            path = "/api/boom"

    resp = _unhandled_error_handler(
        _FakeRequest(), RuntimeError("内部细节：连接池耗尽 (secret-host:5433)")
    )
    assert resp.status_code == 500
    assert b"INTERNAL_ERROR" in resp.body
    # 绝不把堆栈、异常类型、内部主机名回传给客户端
    assert b"secret-host" not in resp.body
    assert b"RuntimeError" not in resp.body
