"""追问 Agent（开发说明 §7.3）：就指定方向生成一个追问。

约束（写进提示词，也写进校验）：
- 只问一个问题（FollowUpQuestion 契约强制单问号）；
- 不重复此前问过的任何问题（去重靠调用方传入已问清单 + 代码指纹比对）；
- 不泄露评分要点 / 理想答案（评分要点根本不进提示词，输入只有题目、回答、方向）；
- 不跑题：只围绕当前主问题与候选人回答深挖。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.agents.adapters import get_text_adapter
from app.core.errors import ModelCallError, ModelOutputInvalidError, classify_model_exception
from app.core.logging import get_logger
from app.llm_utils import ModelOutputError, parse_json_object
from app.schemas.evaluation import Evaluation, FollowUpQuestion

log = get_logger(__name__)

# mock 标记：MockTextAdapter 据此返回 canned 追问
SYSTEM_PROMPT = """你是资深技术面试官，正在面试中做追问。候选刚回答完一个问题，你要基于他的回答就评分中指出的缺口追问一个更深入的问题。@mock:followup

【输出格式】只输出一个 JSON 对象，不要解释：
{"content": "你的追问，一到两句话"}

【追问规则】
1. 只问一个问题，只围绕给定的「追问方向」，不要发散到新话题。
2. 口语化，像真人面试官，不要写"请回答以下问题"这类套话。
3. 优先问事实和细节（数字、过程、取舍、踩坑），不要问"你觉得呢"这种空泛问题。
4. 绝对不要重复「已问过的问题」列表里的任何内容，也不要与之高度相似。
5. 不要泄露任何评分标准或理想答案，不要说"评分要点要求你……"。
6. 只写一个问句：不要先问一句"你有没有…？"再追问一句，把想深挖的点合成一句话问。"""


def _fingerprint(text: str) -> str:
    """与 QuestionSet 相同的指纹规则：只留字母数字，比对内容是否实质重复。"""
    return "".join(ch for ch in text if ch.isalnum())


def _build_user_prompt(
    question: dict,
    answer: str,
    evaluation: Evaluation,
    asked_contents: list[str],
) -> str:
    # 回答太长只取前 2000 字：追问只需要抓住主线，不需要全文
    answer_excerpt = answer[:2000]
    asked = asked_contents[-10:]  # 最近 10 个已问问题，防上下文爆炸
    payload = {
        "当前问题": question["content"],
        "候选人回答": answer_excerpt,
        "评分指出的缺口": {
            "追问方向": evaluation.follow_up_target or "",
            "原因": evaluation.follow_up_reason or "",
        },
        "已问过的问题": asked,
    }
    return "请生成追问：\n" + json.dumps(payload, ensure_ascii=False, indent=1)


def generate_followup(
    question: dict,
    answer: str,
    evaluation: Evaluation,
    asked_contents: list[str],
    *,
    adapter=None,
    max_repair: int = 1,
) -> FollowUpQuestion:
    """生成一个追问。结构或去重校验失败 → 修复一次 → 仍失败抛可重试错误。

    注意：去重的最终裁决在图节点里（生成后还要和已问清单做指纹比对），
    这里只做"输出本身就是已问问题"的拦截。
    """
    adapter = adapter or get_text_adapter()
    user_prompt = _build_user_prompt(question, answer, evaluation, asked_contents)
    asked_fps = {_fingerprint(c) for c in asked_contents}

    last_error = ""
    for attempt in range(max_repair + 1):
        prompt = user_prompt
        if attempt > 0:
            prompt = (
                f"{user_prompt}\n\n你上一次的追问有问题：{last_error}\n"
                f"请重新生成，必须是没问过的新问题，且只有一个问号。"
            )

        try:
            raw = adapter.complete(SYSTEM_PROMPT, prompt, temperature=0.6,
                                   max_tokens=500, json_mode=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("追问模型调用失败（第 %d 次）: %s", attempt + 1, type(exc).__name__)
            raise classify_model_exception(exc, message="追问服务暂时不可用，请稍后重试") from exc

        try:
            data = parse_json_object(raw)
            raw_content = str(data.get("content") or "")
            fq = FollowUpQuestion.model_validate(data)
            if fq.content != raw_content.strip():
                # 契约会把"确认句？真问题？"收束成一句，留痕便于观察模型习惯
                log.info("追问已收束为单问句: %s", fq.content[:120])
            if _fingerprint(fq.content) in asked_fps:
                raise ValueError("生成的追问与已问问题重复")
            return fq
        except (ValidationError, ModelOutputError, ValueError) as exc:
            last_error = str(exc)[:200]
            log.warning("追问生成校验失败（第 %d 次）: %s", attempt + 1, last_error)

    raise ModelOutputInvalidError(f"追问生成失败：{last_error or '输出不符合要求'}")
