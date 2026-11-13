"""SQLAlchemy 2 引擎与会话工厂（新架构正式存储层）。

- 正式环境：PostgreSQL（compose 端口 5433），JSONB 用 sa.dialects.postgresql.JSONB。
- 单元/集成测试：可用 SQLite 内存库（tests 里把 DATABASE_URL 切成 sqlite 并 create_all）。
  为此 ORM 里所有 JSON 字段写法是 sa.JSON().variant(JSONB, "postgresql")：
  PG 上渲染成 JSONB，SQLite 上退回 JSON，同一套模型两边都能跑。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine = None
_session_factory: sessionmaker | None = None


def get_engine():
    """惰性单例引擎。测试里先设置环境变量再首次调用即可指向不同库。"""
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,   # 每次取连接前 ping 一下，容器重启后不挂死
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 3},  # 容器没起时 3 秒内失败，绝不能挂死健康检查
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def session_scope() -> Iterator[Session]:
    """带事务语义的会话上下文：正常提交、异常回滚、最终关闭。

    注意（开发说明 §9.2）：不要跨模型调用持有长事务——
    模型调用的 1~2 分钟里占着连接池是事故源。需要长流程时用多个短事务。
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
