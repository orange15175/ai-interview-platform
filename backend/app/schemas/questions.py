"""题目契约（开发说明 §5）。

校验重点（对应文档"校验题数、编号、空内容、重复项、评分要点及来源关联"）：
- 编号必须 1..N 连续无洞；
- 题干不能重复（模型偷懒时常见）；
- 每道题至少一个评分要点，权重之和接近 1；
- material_evidence_ids 必须能在 ParsedMaterials 里找到（在 agent 层交叉校验）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class RubricPoint(BaseModel):
    point: str = Field(min_length=1, max_length=300)
    weight: float = Field(gt=0, le=1)


class GeneratedQuestion(BaseModel):
    main_index: int = Field(ge=1)
    kind: str = Field(default="main", pattern="^main$")
    category: str = Field(min_length=1, max_length=64)
    difficulty: str = Field(pattern="^(easy|medium|hard)$")
    content: str = Field(min_length=8, max_length=1000)
    material_evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    evaluation_rubric: list[RubricPoint] = Field(min_length=1, max_length=10)

    @field_validator("content")
    @classmethod
    def _no_blank(cls, v: str) -> str:
        """去掉题干首尾空白，再拒绝实际没有文字的题目。"""
        v = v.strip()
        if not v:
            raise ValueError("题干为空")
        return v

    @property
    def rubric_weight_sum(self) -> float:
        """计算本题全部评分要点的权重之和，供整套题目校验使用。"""
        return sum(p.weight for p in self.evaluation_rubric)


class QuestionSet(BaseModel):
    questions: list[GeneratedQuestion]

    @model_validator(mode="after")
    def _validate_set(self) -> "QuestionSet":
        """检查题目非空、编号连续、题干不重复、权重合理且题干未直接附标准答案。"""
        qs = self.questions
        if not qs:
            raise ValueError("题目集为空")

        # 1) 编号必须从 1 开始连续
        indices = sorted(q.main_index for q in qs)
        expected = list(range(1, len(qs) + 1))
        if indices != expected:
            raise ValueError(f"题目编号不连续或有重复：{indices}")

        # 2) 题干不能重复（去掉空白与标点差异后比较）
        def fingerprint(text: str) -> str:
            """只保留字母和数字来比较题干，忽略空白和标点造成的表面差异。"""
            return "".join(ch for ch in text if ch.isalnum())

        fps = [fingerprint(q.content) for q in qs]
        if len(fps) != len(set(fps)):
            raise ValueError("存在重复题目")

        # 3) 评分要点权重之和应接近 1（容忍 0.85~1.15，避免模型四舍五入误伤）
        for q in qs:
            s = q.rubric_weight_sum
            if not (0.85 <= s <= 1.15):
                raise ValueError(f"第 {q.main_index} 题评分要点权重之和异常：{s:.2f}")

        # 4) 题目内容不能带着"标准答案"式的自问自答（简单启发式）
        for q in qs:
            if q.content.count("参考答案") or q.content.count("标准答案"):
                raise ValueError(f"第 {q.main_index} 题题干疑似包含答案")

        return self

    def sorted_questions(self) -> list[GeneratedQuestion]:
        """按主问题编号返回题目，保证模型输出乱序时仍按正确顺序面试。"""
        return sorted(self.questions, key=lambda q: q.main_index)

    def assert_evidence_exists(self, evidence_ids: set[str]) -> None:
        """检查题目引用的材料依据 ID 都存在，防止引用不存在的来源。"""
        for q in self.questions:
            missing = [eid for eid in q.material_evidence_ids if eid not in evidence_ids]
            if missing:
                raise ValueError(f"第 {q.main_index} 题引用了不存在的依据：{missing}")
