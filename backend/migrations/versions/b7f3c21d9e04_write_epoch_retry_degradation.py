"""写入代次 + 失败提交补偿字段 + 模型降级记录

Revision ID: b7f3c21d9e04
Revises: 8c1d4f0a7b52
Create Date: 2026-11-15 17:40:00.000000

三类字段各有各的用途，别合并：

1. interview_sessions.write_epoch
   "这次请求还有没有写入资格"。锁被接管后新持有者递增 epoch，旧请求带着旧
   epoch 写入时条件更新影响 0 行 → 拒绝。它管**纵向**（谁有权写），
   state_version 管**横向**（业务状态是否仍是我以为的那一版）。

2. interview_answers.retryable / next_retry_at / attempt_count / claimed_at / claim_token
   后台补偿的调度依据。retryable 为假的行永不入队；attempt_count 决定退避档位；
   claimed_at + claim_token 做抢占，worker 崩溃后靠 claimed_at 超时把任务放回队列。
   **时间列必须是 timestamptz**：写成 timestamp without time zone 时 psycopg 会按
   会话时区（本机 Asia/Shanghai）转换并丢掉时区，读回来偏 8 小时，
   "到期没到期"的判断全错——这个坑在阶段 4 踩过一次。

3. degradation_level / degradation_reason / model_name / prompt_version
   降级留痕。评分失败、追问跳过是两种完全不同的降级，必须分得开：
   前者是核心结果缺失（保留回答、不推进），后者是可选体验降级（继续下一题）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7f3c21d9e04"
down_revision: Union[str, Sequence[str], None] = "8c1d4f0a7b52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- 会话：写入代次 ----
    op.add_column(
        "interview_sessions",
        sa.Column("write_epoch", sa.BigInteger(), nullable=False, server_default="0"),
    )

    # ---- 回答：重试调度 ----
    op.add_column(
        "interview_answers",
        sa.Column("write_epoch", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "interview_answers",
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "interview_answers",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "interview_answers",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "interview_answers",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "interview_answers",
        sa.Column("claim_token", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_answers_retry_scan",
        "interview_answers",
        ["processing_status", "retryable", "next_retry_at"],
    )

    # ---- 回答：降级记录 ----
    op.add_column(
        "interview_answers",
        sa.Column("degradation_level", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "interview_answers",
        sa.Column("degradation_reason", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "interview_answers",
        sa.Column("model_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "interview_answers",
        sa.Column("prompt_version", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("interview_answers", "prompt_version")
    op.drop_column("interview_answers", "model_name")
    op.drop_column("interview_answers", "degradation_reason")
    op.drop_column("interview_answers", "degradation_level")

    op.drop_index("ix_answers_retry_scan", table_name="interview_answers")
    op.drop_column("interview_answers", "claim_token")
    op.drop_column("interview_answers", "claimed_at")
    op.drop_column("interview_answers", "attempt_count")
    op.drop_column("interview_answers", "next_retry_at")
    op.drop_column("interview_answers", "retryable")
    op.drop_column("interview_answers", "write_epoch")

    op.drop_column("interview_sessions", "write_epoch")
