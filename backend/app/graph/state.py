"""新面试流程图的 State（开发说明 §6）。

设计要点：
- 题目在「准备阶段」已一次性生成好，图只负责逐题推进，不再边聊边编题；
- 追加型字段（evaluations / followup_decisions / asked_contents）用 operator.add
  合并——LangGraph 的默认行为是「覆盖」，对列表会丢历史；
- pending_question 带 evaluation_rubric（评分要用），但发给前端的
  interrupt payload 由 nodes.py 重新组装、剔除 rubric（§5/§10 不得泄露评分标准）。

字段一览（谁写、谁读）：
    questions               prepare 写入；dispatch 读
    current_main_index      prepare/advance 写；dispatch/decide 读
    current_followup_index  prepare/advance 归零；build_followup 递增；decide 读
    pending_question        dispatch/build_followup 写；wait_answer/evaluate 读
    latest_answer           wait_answer 写；evaluate/build_followup 读
    answer_skipped          wait_answer 写；evaluate/decide 读
    evaluations             evaluate 追加（skipped 时记 None 项）
    followup_decisions      decide 追加（**每题恰好一条**，与 evaluations 按下标对齐）
    followup_failures       build_followup 生成失败时追加（不参与下标对齐）
    asked_contents          dispatch/build_followup 追加（去重依据）
    finished                advance 写

一条容易踩的坑（已踩过）：followup_decisions 是**按下标对账**的列表——
interview_workflow 用"第几条回答"去取"第几条裁决"。所以 build_followup 失败时
绝对不能再往 followup_decisions 里塞一条：那会把后面所有题的裁决整体错位一格，
表现是"第 3 题记录里写着第 2 题没追问的原因"。失败留痕走 followup_failures。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class InterviewGraphState(TypedDict, total=False):
    session_id: str
    # 预生成的主问题列表（GeneratedQuestion.model_dump()，按 main_index 升序）
    questions: list[dict]

    # 当前推进到第几道主问题（1 起算）
    current_main_index: int
    # 当前主问题已追问的次数（0 = 还没追问）
    current_followup_index: int

    # 正在等待回答的题目（dict，含 evaluation_rubric，仅内部使用）
    pending_question: dict
    # wait_answer 恢复后写入的候选人回答
    latest_answer: str
    answer_skipped: bool
    # 当前主问题下的正式问答上下文；覆盖更新，换主问题清空，恢复时由数据库重建。
    answer_history: list[dict]

    # 追加型：每题的评分记录（skipped 的题为 None）
    evaluations: Annotated[list[dict | None], operator.add]
    # 追加型：每次追问裁决的留痕。**每题恰好一条**（下标与 evaluations 对齐），
    # 供 interview_workflow 按题序取回本次裁决。
    followup_decisions: Annotated[list[dict], operator.add]
    # 追加型：追问生成失败的留痕（可选环节的降级证据）。
    # 刻意与 followup_decisions 分开：它不参与"第几条回答"的下标对账。
    followup_failures: Annotated[list[dict], operator.add]
    # 本次 build_followup 是否失败（非追加型，每次覆盖）；条件边靠它决定
    # "跳过追问直接推进"还是"进入等待点追问"。
    followup_build_failed: bool
    # 追加型：已问过的题干（含追问），追问 Agent 的去重依据
    asked_contents: Annotated[list[str], operator.add]

    finished: bool
