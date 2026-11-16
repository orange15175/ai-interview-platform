"""评分契约（开发说明 §7.1）。

关键约束：
- score 由代码根据分项与权重计算/校验，避免"文字说不错、分数给 40"的不一致。
- 模型调用失败或输出非法 → 抛错重试，绝不返回 0 分冒充"答错"。
- 追问建议只是建议，最终由 Python 规则链裁决（§7.2）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.questions import RubricPoint


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
        """检查五个评分维度是否都提供了数值，齐全时才允许代码重算总分。"""
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
    # 旧评分无此字段，读取时兼容；新评分的版本由 Agent 设置。
    prompt_version: str | None = Field(default=None, max_length=16)

    @field_validator("feedback")
    @classmethod
    def _feedback_not_blank(cls, v: str) -> str:
        """清理反馈首尾空白，拒绝只有空格的点评。"""
        v = v.strip()
        if not v:
            raise ValueError("反馈为空：宁可报错重试，也不要空点评")
        return v

    def with_code_score(self, weights: dict[str, float] | None = None) -> "Evaluation":
        """维度齐全时按权重计算总分并返回副本；与模型分差距较大时补充说明。

        模型给的分数与重算值差 > 15 时，代码分数为准并在 feedback 尾部
        追加归一化说明，保证"数值与文字一致"。"""
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


_QUESTION_MARKS = ("？", "?")


def to_single_question(text: str) -> str:
    """把中间问号变成逗号，保留最后问号，将多个紧邻问句合成一条追问。

    真实模型很爱这么写：「那改之前你有没有压过测？QPS 500→5000 是怎么测出来的？」
    这是两个问句。早先的实现直接判非法，结果**整条追问被丢弃**（退化成没有追问），
    实测两次追问全部这样挂掉。所以这里不删内容，只把中间的问号改成分句逗号——
    意思一字不少，但对外只有一个问号，答题人面对的就是一个问题。"""
    v = (text or "").strip()
    marks = [i for i, ch in enumerate(v) if ch in _QUESTION_MARKS]
    if len(marks) <= 1:
        return v.replace("?", "？")

    out: list[str] = []
    last = marks[-1]
    for i, ch in enumerate(v):
        if ch in _QUESTION_MARKS:
            out.append("？" if i == last else "，")
        else:
            out.append(ch)
    joined = "".join(out)
    return joined.replace("，？", "？").replace("，，", "，").strip()


class FollowUpQuestion(BaseModel):
    """追问 Agent 的输出：只一个问题，不泄露答案、不换话题。"""

    content: str = Field(min_length=8, max_length=500)

    @field_validator("content")
    @classmethod
    def _single_question(cls, v: str) -> str:
        """限制追问问句数量，并整理成单条追问；太短或问题堆砌时拒绝。"""
        v = v.strip()
        if v.count("？") + v.count("?") > 3:
            # 超过 3 个问句：模型在罗列问题清单，不是追问，宁可重试
            raise ValueError("追问包含过多问句，请只问一个问题")
        v = to_single_question(v)
        if len(v) < 8:
            raise ValueError("追问内容过短")
        return v


class GeneratedFollowUpQuestion(FollowUpQuestion):
    """新生成的追问必须附独立评分依据；旧记录的读取契约保持兼容。"""

    evaluation_rubric: list[RubricPoint] = Field(min_length=1, max_length=3)

    @model_validator(mode="before")
    @classmethod
    def _one_generated_question(cls, data):
        # 新生成内容有修复机会，不再靠改标点把多个问题伪装成一个。
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            content = data["content"].strip()
            if content.count("？") + content.count("?") != 1:
                raise ValueError("新追问必须且只能包含一个问号")
        return data

    @model_validator(mode="after")
    def _validate_generated(self) -> "GeneratedFollowUpQuestion":
        if not self.content.endswith("？"):
            raise ValueError("追问必须以问号结尾")
        if abs(sum(p.weight for p in self.evaluation_rubric) - 1.0) > 0.01:
            raise ValueError("追问评分要点权重之和必须为1")
        if any(not p.point.strip() for p in self.evaluation_rubric):
            raise ValueError("追问评分要点不能为空白")
        return self
