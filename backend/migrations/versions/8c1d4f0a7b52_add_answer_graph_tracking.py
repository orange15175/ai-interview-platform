"""interview_answers 增加图对账字段（graph_stage / graph_wait_question_id / last_error_code）

Revision ID: 8c1d4f0a7b52
Revises: f02b782ee78f
Create Date: 2026-11-14 20:05:00.000000

为什么需要这三列：
- 重试路径必须能回答"这次重试要处理的回答，图当时等的是不是它"。只靠
  "图有没有 checkpoint" 判断会把"图已推进、DB 没落"误判成"可以 resume"，
  结果把旧回答喂给下一题（跳题）。
- 三列全部可空且不回填旧数据：旧行没有对账信息时按"信息不足"处理，
  走显式恢复（STATE_RECOVERY_REQUIRED），不做猜测。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8c1d4f0a7b52"
down_revision: Union[str, Sequence[str], None] = "f02b782ee78f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "interview_answers",
        sa.Column("graph_stage", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "interview_answers",
        sa.Column("graph_wait_question_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "interview_answers",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("interview_answers", "last_error_code")
    op.drop_column("interview_answers", "graph_wait_question_id")
    op.drop_column("interview_answers", "graph_stage")
