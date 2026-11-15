"""复盘报告的响应契约（§3.4）。

刻意保持"扁平 + 可空"：
- overall_score 没有有效评分时是 null，不是 0——0 分是"答得很差"的意思，
  把"没数据"表达成 0 分是误导，而且会被人截图当结论；
- 每个统计项都自带计数，前端不用自己算覆盖率；
- 绝不包含 rubric_results（内部评分要点），证据只给回答与反馈。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ReportSummary(BaseModel):
    """总量统计：四项计数互斥，加起来等于 question_count（已出过的题）。"""

    question_count: int = 0
    completed_count: int = 0     # 真正作答且有结果的
    skipped_count: int = 0       # 主动跳过：计入题数，不计入平均分
    failed_count: int = 0        # 处理失败：保留回答但没有分数
    incomplete_count: int = 0    # 卡在 PENDING/PROCESSING
    overall_score: int | None = None
    score_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    followup_count: int = 0


class DimensionScore(BaseModel):
    """单个维度：只统计有数值的记录，sample_count 说明这个平均分有多可信。"""

    score: int | None = None
    sample_count: int = 0


class CategorySummary(BaseModel):
    category: str
    question_count: int = 0
    completed_count: int = 0
    average_score: int | None = None


class WeakPoint(BaseModel):
    point: str
    count: int
    question_ids: list[str] = Field(default_factory=list)


class AnswerEvidence(BaseModel):
    """回答证据：最高分与最低分的回答摘录，用来支撑结论，不含评分要点。"""

    question_id: str
    main_index: int
    follow_up_index: int = 0
    kind: str
    content: str
    answer_excerpt: str
    score: int
    feedback: str


class InterviewReport(BaseModel):
    session_id: str
    status: str
    is_final: bool
    generated_at: datetime
    summary: ReportSummary
    dimension_scores: dict[str, DimensionScore] = Field(default_factory=dict)
    category_summary: list[CategorySummary] = Field(default_factory=list)
    weak_points: list[WeakPoint] = Field(default_factory=list)
    evidence: list[AnswerEvidence] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    # 数据不足时的如实说明（没有有效评分、还在答题中），绝不假装报告完整
    notice: str | None = None
