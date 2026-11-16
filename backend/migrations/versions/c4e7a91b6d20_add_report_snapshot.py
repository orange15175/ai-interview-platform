"""interview_sessions 增加 report_snapshot（复盘报告快照）

Revision ID: c4e7a91b6d20
Revises: b7f3c21d9e04
Create Date: 2026-11-16 21:00:00.000000

为什么需要这一列：
- 报告原本每次 GET 都现场聚合（15 题要读 15+ 条答案再算），历史列表页是 N+1。
  固化一份之后，详情页只要读一列 JSON。
- 存的是 services/reporting.py 的输出，不含 rubric_results（内部评分要点）。
- 可空且不回填旧数据：旧会话第一次打开时会现场算并写入。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e7a91b6d20"
down_revision: Union[str, Sequence[str], None] = "b7f3c21d9e04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "interview_sessions",
        sa.Column("report_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("interview_sessions", "report_snapshot")
