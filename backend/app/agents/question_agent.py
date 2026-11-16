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
from app.agents.prompt_policy import DATA_BOUNDARY, TECHNICAL_CATEGORIES
from app.core.errors import ModelCallError, ModelOutputInvalidError, classify_model_exception
from app.core.logging import get_logger
from app.llm_utils import ModelOutputError, parse_json_object
from app.schemas.materials import ParsedMaterials
from app.schemas.questions import QuestionSet

log = get_logger(__name__)

SYSTEM_PROMPT = DATA_BOUNDARY + """你是资深技术面试官，负责根据候选人的简历（和可选的岗位描述）设计一整套面试题。@mock:questions

【输出格式】只输出一个 JSON 对象，不要解释、不要代码块：
{
  "questions": [
    {
      "main_index": 1,
      "category": "项目经验｜专业能力｜场景解决 之一",
      "difficulty": "easy｜medium｜hard",
      "content": "口语化的面试问题，一到三句话",
      "material_evidence_ids": ["ev-1"],
      "evaluation_rubric": [{"point": "回答应覆盖的具体要点", "weight": 0.5}, {"point": "…", "weight": 0.5}]
    }
  ]
}

【出题规则】
1. 必须生成用户要求的题目数量，main_index 从 1 连续编号，不得跳号或重复。
2. 只出技术题，类别限项目经验、专业能力、场景解决，不出动机匹配题。项目经验优先；
   题量足够时覆盖三个类别，题量少时优先核心技能，不强求覆盖所有类别。
3. 有 JD 时优先覆盖 JD 的核心要求；没有 JD 时只依据简历，绝对不要虚构岗位要求。
4. JD 要求但简历未体现的技能，可以出概念、机制或假设场景题，引用 JD 依据；
   不预设候选人使用过该技术，不问空泛的学习意愿。
5. 经历类题引用简历依据；岗位能力题可引用 JD 依据。每题至少引用一个给定 evidence id，
   依据必须支持该题的技术方向，不能只因 ID 存在就随意引用；没有依据不出题。
6. evaluation_rubric 是内部评分要点，2~5 条，权重之和为 1.0；不得把答案写进题干。
7. 题干不得出现"参考答案""标准答案"等字样，不得出现编号前缀（如"第1题"）。
8. 题目之间不得重复或高度相似。
9. 不出与简历技术方向及 JD 都无关的偏题，不问脑筋急转弯或年龄婚育等隐私。
10. 每题只设一个主要考察目标，可有紧密相关的说明要求，不堆砌独立问题。
    easy 考基础概念和机制；medium 考应用与边界；hard 考复杂场景与技术取舍。
    根据材料体现的经历安排难度，不推测候选人的级别；不默认全部题都应困难。
11. 评分要点必须能从回答中判断，写清具体机制、必要条件或可接受的推理，
    不写“理解深入”等空泛标准。明确哪些是核心要求、哪些是进阶加分项，
    核心要求权重更高；允许等价正确方案，不把特定框架、关键词当作唯一答案。
    题干未要求的复杂细节不要写成必要要点；基础题不强制工程权衡或量化结果。
12. 简历中的业绩、并发量和性能数字仅是候选人的陈述，不是已验证事实；
    可以提问核实其测量方法，但不能将数字当作已证实的能力或编造经历。"""


def _materials_for_prompt(materials: ParsedMaterials) -> dict:
    """从解析结果挑选出题需要的字段，减少不必要的模型输入。"""
    data = {
        "resume": materials.resume.to_prompt_json(),
        "has_jd": materials.jd is not None,
    }
    if materials.jd is not None:
        data["jd"] = materials.jd.model_dump(mode="json")
    # 保留契约限定长度内的完整依据，避免截断后丢失技术方向或关键限定。
    data["evidence"] = [
        {"id": e.id, "source": e.source, "quote": e.quote} for e in materials.evidence
    ]
    return data


def _build_user_prompt(materials: ParsedMaterials, count: int) -> str:
    """把题数、岗位要求和材料组装成发给出题模型的用户提示词。"""
    payload = json.dumps(_materials_for_prompt(materials), ensure_ascii=False, indent=1)
    jd_hint = "有 JD，请优先覆盖岗位核心要求" if materials.jd else "没有 JD，只依据简历，不要虚构岗位要求"
    return (
        f"题目数量：{count}\n{jd_hint}\n\n"
        f"以下是候选人的材料（JSON）：\n{payload}\n\n"
        f"请按要求生成 {count} 道主问题。"
    )


def _validate(raw: str, count: int, evidence_ids: set[str]) -> QuestionSet:
    """解析模型 JSON，检查整套题目结构、题数和材料依据 ID 是否有效。"""
    data = parse_json_object(raw)
    qs = QuestionSet.model_validate(data)
    qs.assert_evidence_exists(evidence_ids)
    for q in qs.questions:
        if q.category not in TECHNICAL_CATEGORIES:
            raise ValueError("只允许项目经验、专业能力、场景解决三类技术题")
        if not q.material_evidence_ids:
            raise ValueError(f"第 {q.main_index} 题缺少材料依据")

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
    """让文字模型一次生成整套主问题；校验失败时有限次要求模型修正。"""
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
    """把结构校验错误压成短提示，供下一次模型修复使用。"""
    if isinstance(exc, ValidationError):
        errs = exc.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(x) for x in first.get("loc", ()))
            return f"{loc}: {first.get('msg', '校验失败')}"
        return "结构校验失败"
    return str(exc)[:300]
