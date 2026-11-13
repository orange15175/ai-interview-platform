"""ORM 基类与公共类型。

表结构对应开发说明文档 §8，命名统一 interview_ 前缀。
时间戳统一用带时区的 UTC（DateTime(timezone=True)）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import JSON

# 命名约定：让 Alembic 自动生成的约束名字稳定可预测
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        # 业务 JSON 字段在 PG 上是 JSONB，SQLite（测试）退回 JSON
        dict: JSON().with_variant(JSONB(), "postgresql"),
    }


# 业务 JSON 类型：PG 上是 JSONB，SQLite（测试）退回 JSON。
# 所有表的 JSON 列统一用它，不要直接写 sa.JSON。
JSONVar = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    """统一时区时钟，避免各处 datetime.now 写法不一致。"""
    return datetime.now(timezone.utc)
