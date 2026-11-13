"""五张业务表 + 恢复进度（开发说明 §8）。

关键决策对照文档：
- 服务端生成 UUID 主键（不信任客户端传入的 ID）。
- 不保存原始上传文件、Base64、模型内部推理（materials 只存解析结果与证据片段）。
- answers 的 (session_id, request_id) 唯一约束是幂等的数据库底线；
  (session_id, question_id) 唯一约束落实「首期每题只允许一次提交」。
- state_version 用于 CAS：锁租约失效后旧请求想写入会被版本检查拒绝。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import JSONVar

from app.models.base import Base, utcnow

# ---------------------------------------------------------------------------
# 匿名访客（开发说明 §8 anonymous_visitors）
# ---------------------------------------------------------------------------


class AnonymousVisitor(Base):
    __tablename__ = "anonymous_visitors"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 只存哈希：Cookie 里的明文 token 不入库，防拖库后伪造身份
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)


# ---------------------------------------------------------------------------
# 面试会话（§8 interview_sessions）
# ---------------------------------------------------------------------------

# 会话生命周期（§6.1）
SESSION_STATUSES = (
    "PREPARING",          # 创建中：解析/出题进行中（202 轮询阶段）
    "READY",              # 准备完成，可开始答题
    "IN_PROGRESS",        # 答题进行中
    "FINISHED",           # 全部完成或用户提前结束
    "CANCELLED",          # 主动取消
    "EXPIRED",            # 超时过期
    "PREPARATION_FAILED", # 解析/出题失败，终态
)


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 归属校验：所有查询都必须 join owner，防止跨访客读取（§11）
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("anonymous_visitors.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="PREPARING", index=True)
    question_count: Mapped[int] = mapped_column(Integer, default=15)
    # 展示投影：只做快速渲染，推进真相在 answers/progress 里
    current_question_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    # 版本三件套：复盘“哪次评分变化是哪个版本造成的”（§11）
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    graph_version: Mapped[str] = mapped_column(String(32), default="v1")
    rule_version: Mapped[str] = mapped_column(String(32), default="v1")

    # 恢复进度：受控 JSONB（§8 末段），阶段 4 的 recovery 专用重建依据
    progress: Mapped[dict] = mapped_column(JSONVar, default=dict)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    completion_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 脱敏后的错误信息：给前端看稳定错误码 + 人话，不带堆栈和内部细节
    sanitized_error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# 题目（§8 interview_questions）
# ---------------------------------------------------------------------------


class InterviewQuestion(Base):
    __tablename__ = "interview_questions"
    __table_args__ = (
        # 同一会话里 (第几道主问题, 第几次追问) 唯一——追问序号主问题为 0
        UniqueConstraint(
            "session_id", "main_index", "follow_up_index",
            name="uq_questions_session_index",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # main / follow_up
    main_index: Mapped[int] = mapped_column(Integer)          # 1..15
    follow_up_index: Mapped[int] = mapped_column(Integer, default=0)  # 追问 1..2
    parent_question_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    content: Mapped[str] = mapped_column(Text)      # 题干
    category: Mapped[str] = mapped_column(String(64))   # 题目分类
    difficulty: Mapped[str] = mapped_column(String(32))  # 难度
    # 评分要点及权重：【不返回前端】（§5/§10），泄露标准等于泄题
    evaluation_rubric: Mapped[list] = mapped_column(JSONVar, default=list)
    # 题目来源：来自简历哪段经历 / JD 哪条要求，支持 evidence 回溯
    material_evidence_ids: Mapped[list] = mapped_column(JSONVar, default=list)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# ---------------------------------------------------------------------------
# 回答（§8 interview_answers）
# ---------------------------------------------------------------------------

# 处理状态机（§6.1 步骤状态子集）
ANSWER_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")


class InterviewAnswer(Base):
    __tablename__ = "interview_answers"
    __table_args__ = (
        # 幂等底线：同 request_id 只能落一条（§9.3）
        # 必须显式命名：两条约束首列都是 session_id，
        # 交给 SQLAlchemy 自动命名会撞成同一个名字，建第二张时直接报 DuplicateTable
        UniqueConstraint("session_id", "request_id", name="uq_answers_session_request"),
        # 首期每题只允许一次提交（§8；放开修改答案需先撤掉这条）
        UniqueConstraint("session_id", "question_id", name="uq_answers_session_question"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_questions.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    # 同 request_id 携带不同输入 = 客户端 bug 或重放攻击，返回冲突
    input_hash: Mapped[str] = mapped_column(String(64))

    answer_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_kind: Mapped[str] = mapped_column(String(16))  # answer / skip

    processing_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    processing_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    processing_attempt: Mapped[int] = mapped_column(Integer, default=0)

    # 跳过或评分未成功时为空——【不给兜底 0 分】（§7.1）
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluation: Mapped[dict | None] = mapped_column(JSONVar, nullable=True)
    rule_decision: Mapped[dict | None] = mapped_column(JSONVar, nullable=True)
    # 可回放响应快照：去掉内部评分标准后的完整返回（§8）
    response_snapshot: Mapped[dict | None] = mapped_column(JSONVar, nullable=True)

    # 乐观并发：请求带入期望版本，写入时校验（§9.2 CAS）
    expected_state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)


# ---------------------------------------------------------------------------
# 解析材料（§8 interview_materials）
# ---------------------------------------------------------------------------


class InterviewMaterial(Base):
    __tablename__ = "interview_materials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 一场会话只对应一份材料
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), unique=True
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="1")

    resume_profile: Mapped[dict] = mapped_column(JSONVar, default=dict)
    jd_profile: Mapped[dict | None] = mapped_column(JSONVar, nullable=True)
    # 原文依据：evidence 数组（id/source/page/片段），最小化联系人信息
    evidence: Mapped[list] = mapped_column(JSONVar, default=list)
    uncertain_fields: Mapped[list] = mapped_column(JSONVar, default=list)
    warnings: Mapped[list] = mapped_column(JSONVar, default=list)

    parse_model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16), default="v1")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
