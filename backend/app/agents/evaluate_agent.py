"""评分 Agent：依据要点评价回答，总分仅按五维权重计算。

模型失败或输出非法时有限修复，仍失败则抛错，不以零分冒充候选人答错。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.agents.adapters import get_text_adapter
from app.agents.prompt_policy import DATA_BOUNDARY, PROMPT_VERSION
from app.core.config import get_settings
from app.core.errors import ModelOutputInvalidError, classify_model_exception
from app.core.logging import get_logger
from app.llm_utils import ModelOutputError, parse_json_object
from app.schemas.evaluation import Evaluation

log = get_logger(__name__)

# mock 标记：MockTextAdapter 据此返回 canned 评分（见 adapters.py）
SYSTEM_PROMPT = DATA_BOUNDARY + """你是严格、公平的技术面试评分器，服务于学习练习。@mock:evaluation

【输出契约】只输出一个 JSON 对象，不要代码块或额外文字。字段必须齐全：
{
  "score": 0,
  "feedback": "具体点评",
  "rubric_results": [{"point": "评分要点原文", "met": "yes", "note": "回答依据"}],
  "dimension_scores": {"correctness": 0, "completeness": 0, "logic": 0, "depth": 0, "clarity": 0},
  "missing_points": [],
  "follow_up_suggested": false,
  "follow_up_reason": "",
  "follow_up_target": ""
}
score 与五维分均为 0–100 的整数；met 只能为 yes、partial、no；
missing_points 为非空字符串组成的数组，无缺失用 []；布尔值不能写成字符串。
feedback 非空；不追问时 follow_up_reason / follow_up_target 用空字符串。

【评分依据】
1. 只评当前题和当前回答。历史问答仅解释上下文，不能重复计分。
   当前若为追问，只依据本次追问的独立评分要点，不因未重述主问题而扣分。
2. 五维权重：correctness 正确性40%、completeness 完整性20%、logic 逻辑性20%、
   depth 深度15%、clarity 表达清晰度5%。score 为五维加权值四舍五入，代码最终重算。
3. 正确性看结论及机制是否成立；完整性只看本题要求；逻辑性看推导和因果是否自洽；
   深度看符合本题难度的解释、边界或取舍；清晰度看表达是否容易理解。
   简短但正确的答案可以高分，不按篇幅、术语数量、自信程度加减分。
   每个维度以完成本题实际要求为满分基准。回答已准确覆盖本题全部核心要求时，
   不能因未讲题干和要点均未要求的拓展机制而降低完整性或深度；这些只能作为可选学习建议。
   任何扣分理由都应对应本题要求、实际错误或表达歧义，不能为“深度”另造考点。
4. 评分要点是判断依据，不是第二套总分公式。允许技术上等价的正确方案；
   不要求逐字命中，不将某个具体实现当成唯一答案，不推测候选人未表达的知识。
5. rubric_results 与输入要点按顺序一一对应，point 照抄原文：
   yes=充分且正确覆盖；partial=部分正确或解释不足；no=未涉及或核心错误。
   note 必须说明回答中的具体依据或缺失，不能编造原话。
6. 分数参考：0–20 无有效技术内容、明显跑题或仅复述；21–39 少量相关内容但核心有重大错误；
   40–69 部分正确但关键遗漏或明显错误；70–89 核心正确且主要覆盖，仍有局部不足；
   90–100 在本题范围内准确完整、解释充分。区间必须与五维分、要点评价相符。
   基础概念题不强求工程权衡或量化指标，只有题目明确要求时才据此扣分。
7. missing_points 只列本题要求但未充分覆盖的关键内容，不无限扩展知识点。
   feedback 用两三句指出已覆盖内容、具体错误或遗漏，以及可执行的改进方向；
   不羞辱、不空泛鼓励，不直接给出整套标准答案，以免影响后续追问。
8. 有实质回答且存在值得核实或深挖的具体缺口，才建议追问；已充分覆盖、纯跑题、
   仅表示不会时不强行追问。follow_up_target 只写一个方向，不生成问题。
   结合历史问答避免重复建议已覆盖的方向，不能为追问制造缺失。
"""


def _build_user_prompt(question: dict, answer: str) -> str:
    """把当前题、内部评分要点和候选人回答组装成评分提示词。"""
    max_chars = get_settings().business.answer_max_chars
    if len(answer) > max_chars:
        answer = answer[:max_chars]
    payload = {
        "question": {
            "content": question["content"],
            "kind": question.get("kind", "main"),
            "category": question.get("category", ""),
            "difficulty": question.get("difficulty", ""),
            "evaluation_rubric": question.get("evaluation_rubric", []),
        },
        "answer": answer,
        "main_question": question.get("main_question"),
        "history": question.get("history", []),
    }
    return "请评估以下回答：\n" + json.dumps(payload, ensure_ascii=False, indent=1)


def _validate(raw: str, question_rubric: list[dict]) -> Evaluation:
    """严格校验新模型输出；旧记录读取兼容，总分仅由五维计算。"""
    data = parse_json_object(raw)
    ev = Evaluation.model_validate(data)
    if ev.dimension_scores is None or not ev.dimension_scores.complete():
        raise ValueError("dimension_scores 必须包含五个整数分数")
    for key in ("score",):
        if type(data.get(key)) is not int:
            raise ValueError(f"{key} 必须为整数")
    if any(type(data["dimension_scores"].get(k)) is not int
           for k in ev.dimension_scores.model_dump()):
        raise ValueError("五维分数必须为整数，不接受字符串或布尔值")
    required = {"feedback", "rubric_results", "missing_points", "follow_up_suggested",
                "follow_up_reason", "follow_up_target"}
    if not required.issubset(data):
        raise ValueError("评分字段不齐全")
    if type(data["follow_up_suggested"]) is not bool:
        raise ValueError("follow_up_suggested 必须为布尔值")
    if any(not isinstance(data[k], str) for k in ("follow_up_reason", "follow_up_target")):
        raise ValueError("追问原因和方向必须为字符串")
    if [r.point for r in ev.rubric_results] != [p["point"] for p in question_rubric]:
        raise ValueError("rubric_results 必须按顺序逐条对应评分要点原文")
    if any(not r.note or not r.note.strip() for r in ev.rubric_results):
        raise ValueError("每条评分要点必须提供具体评价依据")
    if any(not p.strip() for p in ev.missing_points):
        raise ValueError("missing_points 不允许空白元素")
    if ev.follow_up_suggested and not (ev.follow_up_target or "").strip():
        raise ValueError("建议追问时必须提供一个明确方向")
    if not ev.follow_up_suggested:
        ev = ev.model_copy(update={"follow_up_reason": "", "follow_up_target": ""})
    # 要点用于解释覆盖情况；不再以要点覆盖率覆盖五维加权总分。
    return ev.with_code_score().model_copy(update={"prompt_version": PROMPT_VERSION, "score_source": "code"})


def evaluate_answer(question: dict, answer: str, *, adapter=None, max_repair: int = 1) -> Evaluation:
    """校验五维和要点评价，按五维重算总分；非法输出有限修复，失败不伪造零分。"""
    adapter = adapter or get_text_adapter()
    user_prompt = _build_user_prompt(question, answer)
    rubric = question.get("evaluation_rubric", [])

    last_error = ""
    for attempt in range(max_repair + 1):
        prompt = user_prompt
        if attempt > 0:
            prompt = (
                f"{user_prompt}\n\n你上一次的输出有问题：{last_error}\n"
                f"请重新输出完整 JSON，注意 score 必须按五维权重计算，五维整数分及全部字段必须齐全，"
                f"rubric_results 的 point 必须逐条照抄评分要点原文。"
            )

        try:
            raw = adapter.complete(SYSTEM_PROMPT, prompt, temperature=0.2,
                                   max_tokens=2000, json_mode=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("评分模型调用失败（第 %d 次）: %s", attempt + 1, type(exc).__name__)
            raise classify_model_exception(exc, message="评分服务暂时不可用，请稍后重试") from exc

        try:
            return _validate(raw, rubric)
        except (ValidationError, ModelOutputError, ValueError) as exc:
            last_error = _summarize(exc)
            log.warning("评分结果校验失败（第 %d 次）: %s", attempt + 1, last_error)

    # 走到这里 = 修复后仍不合法。不兜底 0 分，抛错让上层重试整个节点。
    raise ModelOutputInvalidError(f"评分结果不合法：{last_error or '输出不符合要求'}")


def _summarize(exc: Exception) -> str:
    """把校验失败原因缩短，便于回传给模型要求修复。"""
    if isinstance(exc, ValidationError):
        errs = exc.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(x) for x in first.get("loc", ()))
            return f"{loc}: {first.get('msg', '校验失败')}"
        return "结构校验失败"
    return str(exc)[:300]
