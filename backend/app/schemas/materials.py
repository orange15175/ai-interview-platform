"""解析产物契约（开发说明 §4.2 ParsedMaterials）。

约定：
- 缺失信息用 None 或空数组，不允许补全猜测（模型提示词 + 这里的最小长度约束双重保障）。
- evidence 是唯一"可核对依据"，题目只能引用存在的 evidence_id。
- 联系人信息（姓名/电话/邮箱/证件号）不建模——最小化采集（§11）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class Evidence(BaseModel):
    """一段可核对的原文依据。page 只有 PDF/图片来源才有。"""

    id: str = Field(min_length=1, max_length=64)
    source: str = Field(pattern="^(resume|jd)$")
    page: int | None = Field(default=None, ge=1)
    quote: str = Field(min_length=1, max_length=500)


class Education(BaseModel):
    school: str | None = Field(default=None, max_length=200)
    degree: str | None = Field(default=None, max_length=100)
    major: str | None = Field(default=None, max_length=200)
    start: str | None = Field(default=None, max_length=32)
    end: str | None = Field(default=None, max_length=32)


class Experience(BaseModel):
    company: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    duration: str | None = Field(default=None, max_length=100)
    highlights: list[str] = Field(default_factory=list, max_length=10)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("highlights")
    @classmethod
    def _strip(cls, v: list[str]) -> list[str]:
        """清理经历亮点的首尾空白并删除空项，让后续提示词更干净。"""
        return [x.strip() for x in v if x and x.strip()]


class Project(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    stack: list[str] = Field(default_factory=list, max_length=20)
    highlights: list[str] = Field(default_factory=list, max_length=10)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)


class SkillEvidence(BaseModel):
    """技能 → 依据的关联（文档 §4.2：技能和项目关联 evidence_id）。"""

    skill: str = Field(min_length=1, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)


class ResumeProfile(BaseModel):
    education: list[Education] = Field(default_factory=list)
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, max_length=60)
    skill_evidence: list[SkillEvidence] = Field(default_factory=list, max_length=60)

    @field_validator("skills")
    @classmethod
    def _dedup_skills(cls, v: list[str]) -> list[str]:
        """忽略大小写去重技能名，同时保留原有顺序。"""
        seen: set[str] = set()
        out: list[str] = []
        for s in v:
            s = s.strip()
            key = s.lower()
            if s and key not in seen:
                seen.add(key)
                out.append(s)
        return out

    @property
    def is_empty(self) -> bool:
        """没有教育、工作经历和项目时返回 True，用于判断材料是否不足以出题。"""
        return not (self.education or self.experiences or self.projects)

    def to_prompt_json(self) -> dict:
        """把简历对象转成可序列化字典；完整证据原文清单位于外层材料对象。"""
        return self.model_dump(mode="json")


class JDProfile(BaseModel):
    responsibilities: list[str] = Field(default_factory=list, max_length=20)
    required_skills: list[str] = Field(default_factory=list, max_length=40)
    nice_to_have: list[str] = Field(default_factory=list, max_length=20)
    experience_years: str | None = Field(default=None, max_length=64)


class UncertainField(BaseModel):
    field: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=300)
    source: str | None = Field(default=None, max_length=100)


class ParsedMaterials(BaseModel):
    resume: ResumeProfile
    jd: JDProfile | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=200)
    uncertain_fields: list[UncertainField] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("evidence")
    @classmethod
    def _unique_evidence_ids(cls, v: list[Evidence]) -> list[Evidence]:
        """拒绝重复的依据 ID，避免一个编号同时指向两段不同材料。"""
        ids = [e.id for e in v]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence id 重复：题目引用会指向歧义依据")
        return v

    def evidence_index(self) -> set[str]:
        """取出所有有效依据 ID，供出题和引用清理快速检查。"""
        return {e.id for e in self.evidence}
