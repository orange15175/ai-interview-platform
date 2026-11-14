"""解析 Agent 的"有限修复"与错误码分层（开发说明 §七）。

三条要守住的东西：
1. 结构错误最多修一次，修好就用，不整单失败；
2. 修复调用**不带简历原文、不带图片**（不为一份坏输出付两次解析费）；
3. 修复后仍不合法 → MODEL_OUTPUT_INVALID；超时/不可用各自有稳定错误码。
"""

from __future__ import annotations

import json

import pytest

from app.agents.parse_agent import parse_materials
from app.core.errors import (
    MaterialInsufficientError,
    ModelOutputInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from app.parsers.types import ParsedInput, RenderBlock, TextBlock

VALID_PAYLOAD = {
    "resume": {
        "education": [{"school": "测试大学", "degree": "本科", "major": "计算机"}],
        "experiences": [
            {"company": "测试公司", "role": "后端开发", "duration": "2023-2025",
             "highlights": ["把订单接口 P99 从 800ms 降到 120ms"], "evidence_ids": ["ev-1"]}
        ],
        "projects": [],
        "skills": ["Python"],
        "skill_evidence": [],
    },
    "jd": None,
    "evidence": [{"id": "ev-1", "source": "resume", "page": None, "quote": "订单接口优化"}],
    "uncertain_fields": [],
    "warnings": [],
}


class ScriptedAdapter:
    """按顺序吐固定输出，并记录每次调用的入参。"""

    def __init__(self, outputs: list[object]):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def complete(self, system, user, *, images=None, temperature=0.1, json_mode=False):
        self.calls.append({"system": system, "user": user, "images": list(images or [])})
        item = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def _input(text: str = "工作经历：测试公司 后端开发 2023-2025，负责订单系统重构。") -> ParsedInput:
    return ParsedInput(
        text_blocks=[TextBlock(source="resume", label="简历.docx", text=text)],
        render_blocks=[],
        warnings=[],
    )


def test_valid_output_needs_no_repair():
    adapter = ScriptedAdapter([json.dumps(VALID_PAYLOAD, ensure_ascii=False)])
    materials = parse_materials(_input(), adapter=adapter)
    assert len(adapter.calls) == 1
    assert materials.resume.experiences[0].company == "测试公司"


def test_schema_error_is_repaired_once_without_resending_resume():
    bad = json.dumps({"resume": {"experiences": "不是数组"}, "evidence": []}, ensure_ascii=False)
    adapter = ScriptedAdapter([bad, json.dumps(VALID_PAYLOAD, ensure_ascii=False)])

    materials = parse_materials(_input(), adapter=adapter)

    assert len(adapter.calls) == 2
    repair = adapter.calls[1]
    # 只回传字段路径与错误类型；绝不把简历原文再拼一遍
    assert "resume.experiences" in repair["user"]
    assert "工作经历：测试公司" not in repair["user"]
    assert repair["images"] == []
    assert materials.resume.experiences[0].role == "后端开发"


def test_non_json_output_is_repaired_once():
    adapter = ScriptedAdapter([
        "好的，以下是解析结果：暂时无法提取。",
        json.dumps(VALID_PAYLOAD, ensure_ascii=False),
    ])
    materials = parse_materials(_input(), adapter=adapter)
    assert len(adapter.calls) == 2
    assert materials.resume.education[0].school == "测试大学"


def test_still_invalid_after_repair_raises_model_output_invalid():
    bad = json.dumps({"resume": {"experiences": 123}}, ensure_ascii=False)
    adapter = ScriptedAdapter([bad, bad])

    with pytest.raises(ModelOutputInvalidError) as exc:
        parse_materials(_input(), adapter=adapter)

    assert exc.value.code == "MODEL_OUTPUT_INVALID"
    assert len(adapter.calls) == 2


def test_timeout_and_unavailable_have_distinct_codes():
    with pytest.raises(ModelTimeoutError) as exc:
        parse_materials(_input(), adapter=ScriptedAdapter([TimeoutError("read timed out")]))
    assert exc.value.code == "MODEL_TIMEOUT"

    with pytest.raises(ModelUnavailableError) as exc2:
        parse_materials(_input(), adapter=ScriptedAdapter([ConnectionError("refused")]))
    assert exc2.value.code == "MODEL_UNAVAILABLE"


def test_empty_resume_is_material_insufficient_not_unreadable():
    empty = json.dumps({"resume": {"skills": []}, "evidence": [], "warnings": ["空白"]})
    adapter = ScriptedAdapter([empty])

    with pytest.raises(MaterialInsufficientError) as exc:
        parse_materials(_input(), adapter=adapter)
    assert exc.value.code == "MATERIAL_INSUFFICIENT"


def test_no_content_at_all_is_unreadable():
    with pytest.raises(MaterialInsufficientError) as exc:
        parse_materials(ParsedInput(text_blocks=[], render_blocks=[], warnings=[]))
    assert exc.value.code == "UNREADABLE_MATERIAL"


def test_images_are_attached_to_first_call_only():
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    parsed = ParsedInput(
        text_blocks=[TextBlock(source="resume", label="简历.docx", text="个人信息")],
        render_blocks=[RenderBlock(source="resume", label="简历.docx 内嵌图片 1", data=png)],
        warnings=[],
    )
    adapter = ScriptedAdapter([json.dumps(VALID_PAYLOAD, ensure_ascii=False)])
    parse_materials(parsed, adapter=adapter)
    assert adapter.calls[0]["images"] == [png]
