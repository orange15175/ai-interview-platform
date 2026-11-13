"""pytest 全局夹具。

- 跑测试默认 LLM_MOCK=1：绝不因测试产生真实模型费用（开发说明 §3.1）。
- DB 相关测试用 SQLite 内存库（同一套 ORM，JSON 字段自动退回 sa.JSON），
  PostgreSQL 迁移/行为验证在容器就绪后单独做。
"""

from __future__ import annotations

import os

# 必须在导入 app 模块之前设置（app.core.config 读环境）
os.environ.setdefault("LLM_MOCK", "1")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base


@pytest.fixture()
def db_session():
    """每个测试一张全新的内存表，互相隔离。"""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
