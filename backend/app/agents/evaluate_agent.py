"""评分 Agent（开发说明 §7.1）：按评分要点 + 维度给候选人回答打分。

铁律（区别于旧原型 evaluate 节点）：
- 旧原型解析失败按 0 分兜底 —— 那是"把模型故障冒充成答错"。
  新流程里模型调用失败 / 输出非法 → 抛 ModelCallError / ModelOutputInvalidError，
  由上层重试，绝不产出 0 分的 Evaluation。
- 总分不由模型说了算：维度分齐全时按权重重算（Evaluation.with_code_score）；
  评分要点逐条评完时，按 met 值 × 权重算期望分，两者严重背离 → 修复一次，
  仍背离 → 抛错。这保证"文字说不错、分数给 40"的不一致过不了关。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.agents.adapters import get_text_adapter
from app.core.config import get_settings
from app.core.errors import ModelCallError, ModelOutputInvalidError, classify_model_exception
from app.core.logging import get_logger
from app.llm_utils import ModelOutputError, parse_json_object
from app.schemas.evaluation import Evaluation

log = get_logger(__name__)

# mock 标记：MockTextAdapter 据此返回 canned 评分（见 adapters.py）
SYSTEM_PROMPT = """你是资深技术面试官，正在评估候选人对一道面试题的回答。@mock:evaluation

【输出格式】只输出一个 JSON 对象，不要解释、不要代码块：
{
  "score": 0-100 整数,
  "feedback": "两三句点评：先说覆盖了什么，再说缺什么",
  "rubric_results": [
    {"point": "与评分要点原文一致", "met": "yes|partial|no", "note": "一句话说明"}
  ],
  "dimension_scores": {"correctness": 0-100, "completeness": 0-100, "logic": 0-100, "depth": 0-100, "clarity": 0-100},
  "missing_points": ["没覆盖到的要点"],
  "follow_up_suggested": true 或 false,
  "follow_up_reason": "建议追问的原因，30 字内",
  "follow_up_target": "追问方向，如「项目的并发量级」"
}

【评分规则】
1. rubric_results 必须逐条对应给出的评分要点，point 字段照抄要点原文。
2. dimension_scores 五个维度都必须给分；技术正确性权重最高，宁可低分不可虚高。
3. score 要与 dimension_scores 和 rubric_results 一致：要点大部分 met=yes 的回答
   不可能是 40 分；全部 met=no 的回答也不应该是 80 分。
4. follow_up_suggested 只在"回答有实质内容但存在值得深挖的缺口"时为 true；
   回答空泛、跑题、或已充分覆盖时为 false。
5. feedback 要具体可行动，不要"回答不错"这种空话。"""

# rubric met 值 → 得分系数（代码侧期望分计算用）
_MET_VALUE = {"yes": 1.0, "partial": 0.5, "no": 0.0}
# 模型分与代码期望分允许的最大偏差，超过触发修复
_SCORE_TOLERANCE = 20


class _RubricInconsistency(Exception):
    """内部信号：总分与要点期望分背离，携带两次数据用于修复/归一化。

    真实模型实测（DeepSeek）：要点判定通常可靠（"压测方法没讲"），
    但总分常常虚高。所以最终裁决是"信要点、修总分"，而不是报错
    中断面试——报错会让候选人的正常回答卡死在评分环节。
    """

    def __init__(self, ev: Evaluation, expected: float):
        super().__init__(f"总分 {ev.score} 与评分要点期望分 {expected} 严重不一致")
        self.ev = ev
        self.expected = expected


def _expected_score(question_rubric: list[dict], rubric_results: list[dict]) -> float | None:
    """按评分要点权重与 met 值算期望分（0-100）。

    前提：模型把要点逐条都评了（数量一致）才有效，缺条则返回 None 不做该项校验。
    """
    if len(rubric_results) != len(question_rubric):
        return None
    total = 0.0
    weight_sum = 0.0
    for point, result in zip(question_rubric, rubric_results):
        met = str(result.get("met", "")).lower()
        if met not in _MET_VALUE:
            return None
        w = float(point.get("weight", 0))
        total += w * _MET_VALUE[met]
        weight_sum += w
    if weight_sum <= 0:
        return None
    return round(total / weight_sum * 100, 1)


def _build_user_prompt(question: dict, answer: str) -> str:
    max_chars = get_settings().business.answer_max_chars
    if len(answer) > max_chars:
        answer = answer[:max_chars]
    payload = {
        "question": {
            "content": question["content"],
            "category": question.get("category", ""),
            "difficulty": question.get("difficulty", ""),
            "evaluation_rubric": question.get("evaluation_rubric", []),
        },
        "answer": answer,
    }
    return "请评估以下回答：\n" + json.dumps(payload, ensure_ascii=False, indent=1)


def _validate(raw: str, question_rubric: list[dict]) -> Evaluation:
    data = parse_json_object(raw)
    ev = Evaluation.model_validate(data)

    # 一致性校验 1：维度分齐全 → 代码按 TECHNICAL_WEIGHTS 重算覆盖
    ev = ev.with_code_score()

    # 一致性校验 2：要点逐条评完时，期望分与总分不得严重背离
    expected = _expected_score(question_rubric, data.get("rubric_results", []))
    if expected is not None and abs(ev.score - expected) > _SCORE_TOLERANCE:
        raise _RubricInconsistency(ev, expected)

    return ev


def evaluate_answer(question: dict, answer: str, *, adapter=None, max_repair: int = 1) -> Evaluation:
    """评估回答。

    失败分层处理：
    - 模型调用失败 / 输出解析不了 → ModelCallError / ModelOutputInvalidError，
      由上层重试（绝不产出 0 分 Evaluation 冒充"答错"，§7.1）；
    - 总分与要点期望分背离 → 先修复一次；仍背离 → **信要点、修总分**：
      用要点期望分归一化总分（score_source=code，feedback 加说明）。
      实测 DeepSeek 的要点判定比总分更严格也更可靠，硬报错反而会让
      候选人的正常回答卡死在评分环节。

    question 是 GeneratedQuestion.model_dump() 的 dict（含 evaluation_rubric）。
    """
    adapter = adapter or get_text_adapter()
    user_prompt = _build_user_prompt(question, answer)
    rubric = question.get("evaluation_rubric", [])

    last_error = ""
    inconsistent: _RubricInconsistency | None = None
    for attempt in range(max_repair + 1):
        prompt = user_prompt
        if attempt > 0:
            prompt = (
                f"{user_prompt}\n\n你上一次的输出有问题：{last_error}\n"
                f"请重新输出完整 JSON，注意 score 必须与 rubric_results / dimension_scores 一致，"
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
        except _RubricInconsistency as exc:
            inconsistent = exc
            last_error = str(exc)
            log.warning("评分总分与要点背离（第 %d 次）: %s", attempt + 1, last_error)
        except (ValidationError, ModelOutputError, ValueError) as exc:
            last_error = _summarize(exc)
            log.warning("评分结果校验失败（第 %d 次）: %s", attempt + 1, last_error)

    if inconsistent is not None:
        # 修复后仍背离：以要点期望分为准归一化（有依据、可解释），不中断面试
        ev = inconsistent.ev
        code_score = max(0, min(100, int(round(inconsistent.expected))))
        log.warning("评分总分归一化：%s -> %s（以评分要点为准）", ev.score, code_score)
        note = f"（总分按评分要点归一化为 {code_score}）"
        return ev.model_copy(update={
            "score": code_score,
            "score_source": "code",
            "feedback": (ev.feedback + note).strip(),
        })

    # 走到这里 = 修复后仍不合法。不兜底 0 分，抛错让上层重试整个节点。
    raise ModelOutputInvalidError(f"评分结果不合法：{last_error or '输出不符合要求'}")


def _summarize(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errs = exc.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(x) for x in first.get("loc", ()))
            return f"{loc}: {first.get('msg', '校验失败')}"
        return "结构校验失败"
    return str(exc)[:300]
