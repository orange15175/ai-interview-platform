"""评分契约（开发说明 §7.1）。

关键约束：
- score 由代码根据分项与权重计算/校验，避免"文字说不错、分数给 40"的不一致。
- 模型调用失败或输出非法 → 抛错重试，绝不返回 0 分冒充"答错"。
- 追问建议只是建议，最终由 Python 规则链裁决（§7.2）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RubricResult(BaseModel):
    point: str = Field(min_length=1, max_length=300)
    met: Literal["yes", "partial", "no"]
    note: str | None = Field(default=None, max_length=300)


class DimensionScores(BaseModel):
    """技术题默认维度（行为题/自我介绍另配权重，不生搬硬套）。"""

    correctness: int | None = Field(default=None, ge=0, le=100)
    completeness: int | None = Field(default=None, ge=0, le=100)
    logic: int | None = Field(default=None, ge=0, le=100)
    depth: int | None = Field(default=None, ge=0, le=100)
    clarity: int | None = Field(default=None, ge=0, le=100)

    def complete(self) -> bool:
        return all(v is not None for v in self.model_dump().values())


# 技术题维度权重（可在 rules 里按题型覆盖）
TECHNICAL_WEIGHTS: dict[str, float] = {
    "correctness": 0.40,
    "completeness": 0.20,
    "logic": 0.20,
    "depth": 0.15,
    "clarity": 0.05,
}


class Evaluation(BaseModel):
    score: int = Field(ge=0, le=100)
    feedback: str = Field(min_length=1, max_length=2000)
    rubric_results: list[RubricResult] = Field(default_factory=list, max_length=20)
    dimension_scores: DimensionScores | None = None
    missing_points: list[str] = Field(default_factory=list, max_length=20)
    follow_up_suggested: bool = False
    follow_up_reason: str | None = Field(default=None, max_length=300)
    follow_up_target: str | None = Field(default=None, max_length=200)
    # 分数来源：model（模型给的）/ code（按维度权重重算覆盖）
    score_source: Literal["model", "code"] = "model"

    @field_validator("feedback")
    @classmethod
    def _feedback_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("反馈为空：宁可报错重试，也不要空点评")
        return v

    def with_code_score(self, weights: dict[str, float] | None = None) -> "Evaluation":
        """维度分齐全时用代码重算总分（四舍五入取整）。

        模型给的分数与重算值差 > 15 时，代码分数为准并在 feedback 尾部
        追加归一化说明，保证"数值与文字一致"。
        """
        dims = self.dimension_scores
        if dims is None or not dims.complete():
            return self
        w = weights or TECHNICAL_WEIGHTS
        total = sum(float(w[k]) * float(getattr(dims, k)) for k in w)
        code_score = max(0, min(100, int(round(total))))
        if code_score == self.score:
            return self
        note = f"（总分按维度权重归一化为 {code_score}）" if abs(code_score - self.score) > 15 else ""
        return self.model_copy(update={
            "score": code_score,
            "score_source": "code",
            "feedback": (self.feedback + note).strip(),
        })


class FollowUpQuestion(BaseModel):
    """追问 Agent 的输出：只一个问题，不泄露答案、不换话题。"""

    content: str = Field(min_length=8, max_length=500)

    @field_validator("content")
    @classmethod
    def _single_question(cls, v: str) -> str:
        v = v.strip()
        if v.count("？") + v.count("?") > 1:
            raise ValueError("追问必须只包含一个问题")
        return v
