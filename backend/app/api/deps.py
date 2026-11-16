"""FastAPI 依赖：每个请求都要经过的基础边界。

get_db 管理数据库会话；get_visitor 维护匿名身份；get_current_user 解析登录
Cookie；require_same_origin 防 CSRF；get_graph 和 get_redis_client 提供共享基础设施。
把这些边界集中在依赖层，避免每个路由各写一套而漏掉安全检查。

匿名身份和账号身份是两套 Cookie。匿名 Cookie 的最终写出由 main.py 的中间件负责，
登录 Cookie 的签发由 auth.py 负责。请求 ID 和会话 ID 都不能代替这两种身份凭证。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session as OrmSession

from app.core.config import get_settings
from app.core.errors import AuthRequiredError, CsrfRejectedError
from app.core.logging import get_logger
from app.core.security import parse_login_token
from app.infrastructure.db import get_session_factory
from app.models import AnonymousVisitor, User
from app.repositories import users as users_repo
from app.repositories import visitors as visitors_repo

log = get_logger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_db() -> Iterator[OrmSession]:
    """为当前请求提供数据库会话，请求结束时关闭，未提交的修改会回滚。

    服务层自己调 commit（短事务），这里只负责关闭；异常时未提交的写入被丢弃。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def get_visitor(
    request: Request,
    db: OrmSession = Depends(get_db),
) -> AnonymousVisitor:
    """用匿名 Cookie 找访客；找不到或已过期就新建，并登记需要下发的 Cookie。

    Cookie **不在依赖里直接 set**，而是登记到 request.state，由
    PendingCookieMiddleware（main.py）盖到最终响应上。原因：异常处理器
    会新建 JSONResponse，依赖里设置的头会静默丢失——首次访客的第一个
    请求若恰好失败（404/422），就永远拿不到身份，之后每个请求都变成
    新访客。真实联调时这个 bug 表现为"评分丢失、会话找不回"。

    Cookie 只在"新建/过期"时登记，正常请求不重复 Set-Cookie。"""
    settings = get_settings()
    token = request.cookies.get(settings.visitor_cookie_name)
    visitor, new_token = visitors_repo.get_or_create(db, token)
    db.commit()  # 新访客必须立刻落库，否则下一次请求又变成另一个人

    if new_token:
        # 用 starlette 自己的格式化逻辑拼 Set-Cookie，保证与直接 set_cookie 一致
        scratch = Response()
        scratch.set_cookie(
            key=settings.visitor_cookie_name,
            value=new_token,
            max_age=settings.business.session_ttl_hours * 3600,
            httponly=True,                      # JS 读不到，防 XSS 偷身份
            samesite="lax",                     # 跨站 POST 不带 Cookie
            secure=settings.cookie_secure,      # 生产 HTTPS 必须开
            path="/",
        )
        request.state.pending_set_cookie = scratch.headers["set-cookie"]
        log.info("已下发新访客凭证 visitor=%s", visitor.id)
    return visitor


def require_same_origin(request: Request) -> None:
    """写请求带 Origin 时检查其主机和端口；来源不允许就拒绝，没有 Origin 则放行。

    判定口径：
    - 安全方法（GET/HEAD/OPTIONS）不校验；
    - 带 Origin 且 host 不一致 → 403；
    - 不带 Origin（curl、服务间调用）→ 放行：浏览器在跨站写请求上**一定会**
      带 Origin，缺省它意味着不是浏览器发起的 CSRF。"""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if not origin:
        return
    allowed = {request.url.netloc}
    extra = _extra_origins()
    if extra:
        allowed |= extra
    origin_netloc = origin.split("://", 1)[-1].rstrip("/")
    if origin_netloc not in allowed:
        log.warning("拒绝跨站写请求 origin=%s host=%s", origin, request.url.netloc)
        raise CsrfRejectedError()


def _extra_origins() -> set[str]:
    """从配置中取出额外允许的前端主机和端口，供写请求来源检查使用。"""
    raw = get_settings().allowed_origins
    return {
        item.split("://", 1)[-1].rstrip("/")
        for item in (raw or "").split(",")
        if item.strip()
    }


def get_graph():
    """提供正常答题用的图实例；需要从记录恢复时由恢复服务取另一种入口。"""
    from app.services import graph_runtime

    return graph_runtime.get_compiled_graph()


def get_redis_client():
    """提供 Redis 客户端，给幂等响应缓存使用。"""
    from app.infrastructure.redis_client import get_redis

    return get_redis()


def get_current_user(
    request: Request,
    db: OrmSession = Depends(get_db),
) -> User | None:
    """验证登录 Cookie 的签名和有效期，再按用户 ID 查账号；无效时按未登录处理。

    刻意不在这里抛 401：绝大多数接口（创建面试、答题）匿名也能用，
    只有真正需要账号的接口才用 require_user 强制登录。"""
    settings = get_settings()
    token = request.cookies.get(settings.auth_cookie_name)
    user_id = parse_login_token(token, secret=settings.auth_secret)
    if user_id is None:
        return None
    return users_repo.get(db, user_id)


def require_user(user: User | None = Depends(get_current_user)) -> User:
    """要求请求已经登录；没有有效账号就返回 AUTH_REQUIRED。"""
    if user is None:
        raise AuthRequiredError()
    return user
