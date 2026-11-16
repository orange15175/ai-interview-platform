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
from app.agents.prompt_policy import DATA_BOUNDARY
from app.core.errors import ModelOutputInvalidError, classify_model_exception
from app.core.logging import get_logger
from app.llm_utils import ModelOutputError, parse_json_object
from app.schemas.evaluation import Evaluation, GeneratedFollowUpQuestion

log = get_logger(__name__)

# mock 标记：MockTextAdapter 据此返回 canned 追问
SYSTEM_PROMPT = DATA_BOUNDARY + """你是资深技术面试官，正在面试中做追问。候选刚回答完一个问题，你要基于他的回答就评分中指出的缺口追问一个更深入的问题。@mock:followup

【输出格式】只输出一个 JSON 对象，不要解释：
{"content": "一条追问？", "evaluation_rubric": [{"point": "仅本追问要求覆盖的具体要点", "weight": 1.0}]}

【追问规则】
1. 只问一个问题，只围绕给定的「追问方向」，不要发散到新话题。
2. 口语化，像真人面试官，不要写"请回答以下问题"这类套话。
3. 优先问事实和细节（数字、过程、取舍、踩坑），不要问"你觉得呢"这种空泛问题。
4. 绝对不要重复「已问过的问题」列表里的任何内容，也不要与之高度相似。
5. 不要泄露任何评分标准或理想答案，不要说"评分要点要求你……"。
6. 只追一个维度：实现细节、边界条件、性能指标、故障排查、技术取舍，五选一。
   只留一个以“？”结尾的问句，即使只用一个问号，也不能堆多个独立作答任务。
7. 必须紧扣当前问题和回答中的具体表述；对于未提及内容，不能虚构“你刚才说过”。
   主问题和历史问答仅作背景，避免重复已答内容；只问指定方向中尚未充分验证的一点。
8. 不评分、不总结、不回答、不建议，不列举正确做法来暗示答案。
   通常20–80字，以具体清楚为先，不为凑字数添加套话，不用通用兜底问题。
9. evaluation_rubric 是内部独立评分依据，1–3条，正权重之和为1；
   只考本条追问实际要求的内容，接受等价正确方案，不复制主问题全部要点。
   题干不能泄露这些要点；不要把未问到的进阶内容列为必要要求。"""


def _fingerprint(text: str) -> str:
    """去掉空白和标点后比较题干，用来识别换了标点但内容相同的追问。"""
    return "".join(ch for ch in text if ch.isalnum())


def _build_user_prompt(
    question: dict,
    answer: str,
    evaluation: Evaluation,
    asked_contents: list[str],
) -> str:
    """把当前题、回答、评分缺口和已问题目交给模型，要求围绕缺口追问。"""
    asked = asked_contents[-10:]  # 最近 10 个已问问题，防上下文爆炸
    payload = {
        "当前问题": question["content"],
        "候选人回答": answer,
        "主问题": question.get("main_question", question["content"]),
        "历史问答": question.get("history", []),
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
) -> GeneratedFollowUpQuestion:
    """生成一个追问并检查长度、问句数量和重复内容，失败时有限次修复。

    注意：去重的最终裁决在图节点里（生成后还要和已问清单做指纹比对），
    这里只做"输出本身就是已问问题"的拦截。"""
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
                                   max_tokens=1200, json_mode=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("追问模型调用失败（第 %d 次）: %s", attempt + 1, type(exc).__name__)
            raise classify_model_exception(exc, message="追问服务暂时不可用，请稍后重试") from exc

        try:
            data = parse_json_object(raw)
            fq = GeneratedFollowUpQuestion.model_validate(data)
            if _fingerprint(fq.content) in asked_fps:
                raise ValueError("生成的追问与已问问题重复")
            return fq
        except (ValidationError, ModelOutputError, ValueError) as exc:
            last_error = str(exc)[:200]
            log.warning("追问生成校验失败（第 %d 次）: %s", attempt + 1, last_error)

    raise ModelOutputInvalidError(f"追问生成失败：{last_error or '输出不符合要求'}")
