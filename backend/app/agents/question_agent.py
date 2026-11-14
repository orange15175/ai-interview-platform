"""出题 Agent：按材料一次性生成整套主问题（开发说明 §5）。

要点：
- 一次生成完整题目集（前端一次只展示当前题，但后端一次存完）。
- 有 JD 优先覆盖岗位核心要求；没有 JD 时不得虚构岗位要求。
- 校验不过只给一次修复机会（可配置），仍失败就抛错误，绝不标记 READY。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.agents.adapters import get_text_adapter
from app.core.errors import ModelCallError, ModelOutputInvalidError, classify_model_exception
from app.core.logging import get_logger
from app.llm_utils import ModelOutputError, parse_json_object
from app.schemas.materials import ParsedMaterials
from app.schemas.questions import QuestionSet

log = get_logger(__name__)

SYSTEM_PROMPT = """你是资深技术面试官，负责根据候选人的简历（和可选的岗位描述）设计一整套面试题。@mock:questions

【输出格式】只输出一个 JSON 对象，不要解释、不要代码块：
{
  "questions": [
    {
      "main_index": 1,
      "category": "项目经验｜专业能力｜场景解决｜动机匹配 之一",
      "difficulty": "easy｜medium｜hard",
      "content": "口语化的面试问题，一到三句话",
      "material_evidence_ids": ["ev-1"],
      "evaluation_rubric": [{"point": "回答应覆盖的具体要点", "weight": 0.5}, {"point": "…", "weight": 0.5}]
    }
  ]
}

【出题规则】
1. 必须生成用户要求的题目数量，main_index 从 1 连续编号，不得跳号或重复。
2. 题目要覆盖：项目经验（占比最高）、与岗位相关的专业能力、真实场景的问题解决。
3. 有 JD 时优先覆盖 JD 的核心要求；没有 JD 时只依据简历，绝对不要虚构岗位要求。
4. JD 要求但简历未体现的技能，可以问"掌握程度/如何学习"，但不得预设候选人有相关项目经历。
5. 每道题都基于简历里真实存在的内容，material_evidence_ids 必须引用给定材料中的 evidence id；
   找不到依据的题不要出。
6. evaluation_rubric 是内部评分要点，2~5 条，权重之和为 1.0；不得把答案写进题干。
7. 题干不得出现"参考答案""标准答案"等字样，不得出现编号前缀（如"第1题"）。
8. 题目之间不得重复或高度相似。
9. 不要问与候选人经历无关的偏题、脑筋急转弯，不要询问年龄婚育等隐私。"""


def _materials_for_prompt(materials: ParsedMaterials) -> dict:
    """只把必要字段给模型，控制上下文长度（§6.2 节点输入尽量小）。"""
    data = {
        "resume": materials.resume.to_prompt_json(),
        "has_jd": materials.jd is not None,
    }
    if materials.jd is not None:
        data["jd"] = materials.jd.model_dump(mode="json")
    # evidence 只给 id 和前 120 字，够模型引用，不至于爆上下文
    data["evidence"] = [
        {"id": e.id, "source": e.source, "quote": e.quote[:120]} for e in materials.evidence
    ]
    return data


def _build_user_prompt(materials: ParsedMaterials, count: int) -> str:
    payload = json.dumps(_materials_for_prompt(materials), ensure_ascii=False, indent=1)
    jd_hint = "有 JD，请优先覆盖岗位核心要求" if materials.jd else "没有 JD，只依据简历，不要虚构岗位要求"
    return (
        f"题目数量：{count}\n{jd_hint}\n\n"
        f"以下是候选人的材料（JSON）：\n{payload}\n\n"
        f"请按要求生成 {count} 道主问题。"
    )


def _validate(raw: str, count: int, evidence_ids: set[str]) -> QuestionSet:
    data = parse_json_object(raw)
    qs = QuestionSet.model_validate(data)
    qs.assert_evidence_exists(evidence_ids)

    if len(qs.questions) != count:
        raise ValueError(f"题目数量不符：要求 {count} 道，实际 {len(qs.questions)} 道")
    return qs


def generate_questions(
    materials: ParsedMaterials,
    count: int,
    *,
    adapter=None,
    max_repair: int = 1,
) -> QuestionSet:
    """生成并通过校验；最多修复 max_repair 次（默认 1 次，§5）。"""
    adapter = adapter or get_text_adapter()
    user_prompt = _build_user_prompt(materials, count)
    evidence_ids = materials.evidence_index()

    last_error = ""
    for attempt in range(max_repair + 1):
        prompt = user_prompt
        if attempt > 0:
            # 把上次的问题明确告诉模型，要求完整重出（不是只补差异）
            prompt = (
                f"{user_prompt}\n\n你上一次的输出有以下问题：{last_error}\n"
                f"请重新输出完整 JSON，共 {count} 道题，编号 1..{count} 连续，不要重复题干。"
            )

        try:
            raw = adapter.complete(SYSTEM_PROMPT, prompt, temperature=0.7,
                                   max_tokens=8000, json_mode=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("出题模型调用失败（第 %d 次）: %s", attempt + 1, type(exc).__name__)
            raise classify_model_exception(exc, message="出题服务暂时不可用，请稍后重试") from exc

        try:
            qs = _validate(raw, count, evidence_ids)
        except (ValidationError, ModelOutputError, ValueError) as exc:
            last_error = _summarize_error(exc)
            log.warning("出题结果校验失败（第 %d 次）: %s", attempt + 1, last_error)
            continue

        log.info("题目生成完成: %d 道（第 %d 次尝试）", len(qs.questions), attempt + 1)
        return qs

    raise ModelOutputInvalidError(f"题目生成失败：{last_error or '输出不符合要求'}")


def _summarize_error(exc: Exception) -> str:
    """把 Pydantic 的冗长报错压成一句人话，回灌给模型修复。"""
    if isinstance(exc, ValidationError):
        errs = exc.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(x) for x in first.get("loc", ()))
            return f"{loc}: {first.get('msg', '校验失败')}"
        return "结构校验失败"
    return str(exc)[:300]
