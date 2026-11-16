"""两个 Agent 的集成测试（mock 适配器，不联网、不产生费用）。"""

import json

import pytest

from app.agents.adapters import MockTextAdapter, MockVisionAdapter
from app.agents.parse_agent import parse_materials
from app.agents.question_agent import generate_questions
from app.core.errors import MaterialInsufficientError, ModelOutputInvalidError
from app.parsers.types import ParsedInput, TextBlock
from app.schemas.materials import ParsedMaterials


def _input(text: str = "张三，后端工程师，三年 Python 经验。") -> ParsedInput:
    return ParsedInput(
        text_blocks=[TextBlock(source="resume", label="简历.docx", text=text)],
        render_blocks=[],
        warnings=[],
    )


def test_parse_materials_with_mock():
    materials = parse_materials(_input(), adapter=MockVisionAdapter())
    assert isinstance(materials, ParsedMaterials)
    assert materials.resume.projects
    assert materials.evidence and materials.evidence[0].id == "ev-1"


def test_parse_rejects_empty_input():
    with pytest.raises(MaterialInsufficientError):
        parse_materials(ParsedInput(text_blocks=[], render_blocks=[], warnings=[]))


def test_parse_rejects_empty_resume():
    """模型返回了结构但没有任何实质内容 —— 要求用户补充，不硬出题。"""
    class EmptyAdapter:
        def complete(self, *args, **kwargs):
            return json.dumps({"resume": {"skills": []}, "evidence": [], "warnings": ["空白"]})

    with pytest.raises(MaterialInsufficientError):
        parse_materials(_input(), adapter=EmptyAdapter())


def test_parse_cleans_invalid_evidence_refs():
    class DirtyAdapter:
        def complete(self, *args, **kwargs):
            return json.dumps({
                "resume": {
                    "projects": [{"name": "p", "evidence_ids": ["ev-1", "ev-ghost"]}],
                    "skills": ["Python"],
                },
                "evidence": [{"id": "ev-1", "source": "resume", "quote": "原文"}],
            }, ensure_ascii=False)

    materials = parse_materials(_input(), adapter=DirtyAdapter())
    assert materials.resume.projects[0].evidence_ids == ["ev-1"]
    assert any("剔除" in w for w in materials.warnings)


def test_generate_questions_count_matches():
    materials = parse_materials(_input(), adapter=MockVisionAdapter())
    qs = generate_questions(materials, count=15, adapter=MockTextAdapter())
    assert len(qs.questions) == 15
    assert [q.main_index for q in qs.sorted_questions()] == list(range(1, 16))
    assert all(q.material_evidence_ids == ["ev-1"] for q in qs.questions)


def test_generate_questions_fails_after_repair():
    """数量永远不对的模型：修复一次后仍失败，必须抛错而不是返回残缺题库。"""
    class BadAdapter:
        def complete(self, system, user, **kwargs):
            return json.dumps({"questions": [{
                "main_index": 1, "category": "c", "difficulty": "easy",
                "content": "只有一个问题？", "material_evidence_ids": [],
                "evaluation_rubric": [{"point": "p", "weight": 1.0}],
            }]}, ensure_ascii=False)

    materials = parse_materials(_input(), adapter=MockVisionAdapter())
    with pytest.raises(ModelOutputInvalidError):
        generate_questions(materials, count=15, adapter=BadAdapter())


def test_generate_questions_rejects_unknown_evidence():
    class GhostEvidenceAdapter:
        def complete(self, system, user, **kwargs):
            return json.dumps({"questions": [{
                "main_index": 1, "category": "c", "difficulty": "easy",
                "content": "引用了不存在的依据的问题？",
                "material_evidence_ids": ["ev-ghost"],
                "evaluation_rubric": [{"point": "p", "weight": 1.0}],
            }]}, ensure_ascii=False)

    materials = parse_materials(_input(), adapter=MockVisionAdapter())
    with pytest.raises(ModelOutputInvalidError, match="依据"):
        generate_questions(materials, count=1, adapter=GhostEvidenceAdapter())


@pytest.mark.parametrize("category,evidence,valid", [
    ("专业能力", ["jd-kafka"], True),
    ("动机匹配", ["jd-kafka"], False),
    ("专业能力", [], False),
])
def test_jd_only_skills_allowed_but_non_technical_or_ungrounded_questions_rejected(category, evidence, valid):
    materials = ParsedMaterials.model_validate({
        "resume": {"projects": [{"name": "Python项目"}], "skills": ["Python"]},
        "jd": {"required_skills": ["Kafka"]},
        "evidence": [{"id": "jd-kafka", "source": "jd", "quote": "要求掌握Kafka"}],
    })
    class Adapter:
        def complete(self, system, user, **kwargs):
            return json.dumps({"questions": [{
                "main_index": 1, "category": category, "difficulty": "easy",
                "content": "请说明Kafka消费者组的作用？", "material_evidence_ids": evidence,
                "evaluation_rubric": [{"point": "解释消费者组分工消费分区的机制", "weight": 1.0}],
            }]}, ensure_ascii=False)
    if valid:
        qs = generate_questions(materials, 1, adapter=Adapter())
        assert qs.questions[0].material_evidence_ids == ["jd-kafka"]
    else:
        with pytest.raises(ModelOutputInvalidError):
            generate_questions(materials, 1, adapter=Adapter())
