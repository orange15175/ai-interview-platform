"""FastAPI 依赖：数据库会话、匿名访客 Cookie、编译好的图（开发说明 §1.1/§11）。

阶段 4 只落地"身份与并发"这两块基础设施，具体路由在阶段 5 接上。
放这里的意义：Cookie 的写法（HttpOnly/SameSite/Secure、只在新建时下发）
必须是**唯一一处**，否则各接口各写一遍，迟早漏掉某个属性。

三个依赖：
- get_db：每请求一个 Session，异常时关闭（未提交的事务随之回滚）。
- get_visitor：读 Cookie → 转哈希查库；无效/过期则新建并 Set-Cookie。
- require_same_origin：写操作的 Origin 校验（CSRF），浏览器跨站请求直接 403。

阶段 5 追加：
- get_graph：编译好的面试图（进程内缓存，状态在 checkpointer 里）。
- get_redis_client：幂等缓存用的 Redis 客户端（与 checkpointer 是两个用途）。
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
    """请求级数据库会话。

    服务层自己调 commit（短事务），这里只负责关闭；异常时未提交的写入被丢弃。
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def get_visitor(
    request: Request,
    db: OrmSession = Depends(get_db),
) -> AnonymousVisitor:
    """解析匿名访客；首次访问时登记 HttpOnly Cookie。

    Cookie **不在依赖里直接 set**，而是登记到 request.state，由
    PendingCookieMiddleware（main.py）盖到最终响应上。原因：异常处理器
    会新建 JSONResponse，依赖里设置的头会静默丢失——首次访客的第一个
    请求若恰好失败（404/422），就永远拿不到身份，之后每个请求都变成
    新访客。真实联调时这个 bug 表现为"评分丢失、会话找不回"。

    Cookie 只在"新建/过期"时登记，正常请求不重复 Set-Cookie。
    """
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
    """写操作来源校验（同源代理部署下，Origin 的 host 必须与服务端一致）。

    判定口径：
    - 安全方法（GET/HEAD/OPTIONS）不校验；
    - 带 Origin 且 host 不一致 → 403；
    - 不带 Origin（curl、服务间调用）→ 放行：浏览器在跨站写请求上**一定会**
      带 Origin，缺省它意味着不是浏览器发起的 CSRF。
    """
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
    """允许的前端地址（本地开发前端跑在 5173，与后端 8000 不同源）。"""
    raw = get_settings().allowed_origins
    return {
        item.split("://", 1)[-1].rstrip("/")
        for item in (raw or "").split(",")
        if item.strip()
    }


def get_graph():
    """面试图（正常变体）。恢复变体在 recovery 流程里单独取。"""
    from app.services import graph_runtime

    return graph_runtime.get_compiled_graph()


def get_redis_client():
    """幂等缓存客户端（§9.3 第 8 步）。测试可覆写。"""
    from app.infrastructure.redis_client import get_redis

    return get_redis()


def get_current_user(
    request: Request,
    db: OrmSession = Depends(get_db),
) -> User | None:
    """当前登录用户；未登录/令牌过期/签名不对一律返回 None（当作游客）。

    刻意不在这里抛 401：绝大多数接口（创建面试、答题）匿名也能用，
    只有真正需要账号的接口才用 require_user 强制登录。
    """
    settings = get_settings()
    token = request.cookies.get(settings.auth_cookie_name)
    user_id = parse_login_token(token, secret=settings.auth_secret)
    if user_id is None:
        return None
    return users_repo.get(db, user_id)


def require_user(user: User | None = Depends(get_current_user)) -> User:
    """强制登录的依赖（留给"我的面试记录"这类接口用）。"""
    if user is None:
        raise AuthRequiredError()
    return user
