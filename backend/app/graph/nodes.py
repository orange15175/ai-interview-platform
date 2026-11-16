"""LangGraph 的逐题状态机节点。

prepare 校验初始题目，dispatch 选择当前题，wait_answer 暂停等待前端；
evaluate_answer_node 评分，decide_followup_node 用规则决定是否追问；
build_followup_node 生成追问，advance_node 移到下一题。节点只读写图状态，
正式事实仍由 PostgreSQL 保存，Redis checkpoint 只保存执行位置。

图的形状（build 见 builder.py）：

    START -> prepare -> dispatch -> wait_answer -> evaluate_answer -> decide
                         ^              ^                            |     |
                         |              +-- build_followup <--- ask -+     |
                         |                                                 |
                         +-- advance <---------------------------- stop ---+
                                |
                               END（主问题全部完成）

    wait_answer 内部用 interrupt() 暂停，候选人提交答案后用
    Command(resume=...) 恢复。暂停后本次图调用返回，不需要占着线程等用户输入：
    节点里不做模型调用、不写数据库，恢复后从检查点继续。

三条铁律：
1. 含 interrupt 的节点必须可重入：恢复时节点从头重跑，interrupt()
   直接返回 resume 值。所以 wait_answer 里没有任何副作用代码。
2. 模型调用只发生在恢复之后的节点（evaluate / build_followup）。
3. 评分失败 = 可重试错误（抛出去），绝不写 0 分冒充"答错"（§7.1）。
"""

from __future__ import annotations

from typing import Literal

from langgraph.types import interrupt

from app.agents.evaluate_agent import evaluate_answer
from app.agents.followup_agent import generate_followup
from app.core.config import get_settings
from app.core.errors import ModelCallError
from app.core.logging import get_logger
from app.graph.state import InterviewGraphState
from app.rules.followup_rules import RULE_VERSION, decide_followup
from app.schemas.followup import REASON_FOLLOWUP_FAILED, FollowupDecision
from app.schemas.questions import QuestionSet

log = get_logger(__name__)


# ---------------------------------------------------------------- 入口


def prepare(state: InterviewGraphState) -> dict:
    """检查已经生成的题目集，并把图的题号初始化到第一道主问题；这里不调出题模型。"""
    raw = state.get("questions") or []
    # 复用 QuestionSet 的全部校验（编号连续 / 无重复 / 权重合法），不信任调用方
    qs = QuestionSet.model_validate({"questions": raw}).sorted_questions()
    if not state.get("session_id"):
        raise ValueError("session_id 缺失：图的检查点与它绑定，必须提供")
    return {
        "questions": [q.model_dump() for q in qs],
        "current_main_index": 1,
        "current_followup_index": 0,
        "finished": False,
    }


def dispatch(state: InterviewGraphState) -> dict:
    """按当前主问题编号取题，放进待答题字段，并记录到已问题目清单。

    幂等：只依赖 state 推导，重复执行结果相同（崩溃重放安全）。"""
    questions = state["questions"]
    idx = state.get("current_main_index", 1)
    if not 1 <= idx <= len(questions):
        raise ValueError(f"主问题序号越界：{idx}（共 {len(questions)} 道）")

    q = questions[idx - 1]
    pending = dict(q)
    pending["kind"] = "main"
    pending["follow_up_index"] = 0
    # 已问清单追加本题主干，供追问 Agent 去重
    return {"pending_question": pending, "asked_contents": [q["content"]], "answer_history": []}


# ---------------------------------------------------------------- 等待回答


def _public_payload(state: InterviewGraphState) -> dict:
    """从图内部题目中挑出可公开字段，去掉内部评分要点后供调用方展示。

    评分要点 = 题目的答案结构，泄露给前端等于泄题。"""
    p = state["pending_question"]
    return {
        "session_id": state.get("session_id", ""),
        "kind": p["kind"],
        "main_index": p["main_index"],
        "follow_up_index": p.get("follow_up_index", 0),
        "content": p["content"],
        "category": p.get("category", ""),
        "difficulty": p.get("difficulty", ""),
        "total_questions": len(state["questions"]),
    }


def _normalize_resume(value) -> tuple[str, bool, str | None]:
    """把恢复图时传入的内容整理成回答、是否跳过和错误提示三项。"""
    if isinstance(value, dict) and value.get("skipped"):
        return "", True, None
    if isinstance(value, dict):
        answer = str(value.get("answer", ""))
    else:
        answer = str(value)
    answer = answer.strip()

    max_chars = get_settings().business.answer_max_chars
    if not answer:
        return "", False, "回答内容为空：请输入回答，或明确选择跳过"
    if len(answer) > max_chars:
        return answer, False, f"回答超过 {max_chars} 字上限，请精简后重新提交"
    return answer, False, None


def wait_answer(state: InterviewGraphState) -> dict:
    """用 interrupt 保存等待点；下次提交回答时从这里恢复，不占用线程一直等待。

    resume 值的约定（前端契约）：
        {"answer": "文本..."}   正常回答
        {"skipped": true}       跳过本题（不调评分模型，

    非法输入（空回答 / 超长）不抛错——节点抛错会把非法 resume 值烧进
    检查点（LangGraph 行为：失败的 resume 值留在 pending writes，重试
    时被重放），正确的做法是带着 error 字段重新 interrupt，客户端修正
    后再 resume 即可。连续 3 次非法输入才真正抛错（防御异常客户端）。"""
    payload = _public_payload(state)

    for _ in range(3):
        value = interrupt(payload)
        answer, skipped, error = _normalize_resume(value)
        if error is None:
            return {"latest_answer": answer, "answer_skipped": skipped}
        # 带错误信息重新发起中断：客户端会看到 error 字段并重新提交
        payload = {**payload, "error": error}

    raise ValueError("连续多次提交非法回答，会话已停止；请刷新后重试")


# ---------------------------------------------------------------- 评分与裁决


def _current_targets(state: InterviewGraphState) -> list[str]:
    """收集当前主问题已经追问过的方向，防止再次追问相同缺口。"""
    idx = state.get("current_main_index", 1)
    return [
        d["target"]
        for d in state.get("followup_decisions", [])
        if d.get("main_index") == idx and d.get("decision") == "ask" and d.get("target")
    ]


def _question_context(state: InterviewGraphState) -> dict:
    """附加本主问题上下文，不改动已保存题目的独立评分标准。"""
    pending = dict(state["pending_question"])
    main = next((q for q in state.get("questions", [])
                 if q["main_index"] == pending["main_index"]), pending)
    pending["main_question"] = main["content"]
    pending["history"] = list(state.get("answer_history", []))
    return pending


def evaluate_answer_node(state: InterviewGraphState) -> dict:
    """给本次回答调用评分模型；跳过时只追加空评分，不产生模型费用。"""
    if state.get("answer_skipped"):
        return {"evaluations": [None]}

    pending = _question_context(state)
    answer = state["latest_answer"]
    # 失败直接抛（ModelCallError / ModelOutputInvalidError）：
    # 检查点停在本节点之前，客户端重试不会产生重复评分记录
    ev = evaluate_answer(pending, answer)
    return {
        "evaluations": [ev.model_dump()],
        "answer_history": [*state.get("answer_history", []),
                           {"question": pending["content"], "answer": answer}],
    }


def decide_followup_node(state: InterviewGraphState) -> dict:
    """根据分数、缺口和追问次数运行 Python 规则，决定继续追问还是换主问题。"""
    ev_dump = state.get("evaluations", [])[-1] if state.get("evaluations") else None
    from app.schemas.evaluation import Evaluation

    evaluation = Evaluation.model_validate(ev_dump) if ev_dump else None
    decision = decide_followup(
        evaluation=evaluation,
        skipped=bool(state.get("answer_skipped")),
        followup_count=state.get("current_followup_index", 0),
        asked_targets=_current_targets(state),
        max_followups=get_settings().business.max_followups,
    )
    record = {
        "main_index": state.get("current_main_index", 1),
        "decision": decision.decision,
        "reason_code": decision.reason_code,
        "rule_version": decision.rule_version,
        "detail": decision.detail,
        # ask 时才有：供下一轮的重复检查与日志追溯
        "target": (evaluation.follow_up_target if evaluation else "") or "",
    }
    return {"followup_decisions": [record]}


def route_after_decision(state: InterviewGraphState) -> Literal["build_followup", "advance"]:
    """读取最新追问决定，选择生成追问或直接推进主问题。"""
    decisions = state.get("followup_decisions", [])
    last = decisions[-1] if decisions else None
    if last and last["decision"] == "ask":
        return "build_followup"
    return "advance"


# ---------------------------------------------------------------- 追问与推进


def build_followup_node(state: InterviewGraphState) -> dict:
    """生成并设置下一道追问；服务调用失败上抛，内容生成失败则留痕后跳过追问。

    失败策略（区别于评分）：
    - 模型服务不可用（ModelCallError）→ 抛出去让客户端重试；
    - 修复后输出仍非法（ModelOutputInvalidError）→ 不死磕：记一条
      followup_failures 留痕后直接推进主流程。面试不能因为追问生成
      失败而卡死——追问是增益项，不是必选项。

    **失败绝不往 followup_decisions 里追加**：那个列表按下标与
    evaluations 对齐（"第几条回答"取"第几条裁决"），多塞一条会让后面
    所有题的裁决错位一格。留痕走独立的 followup_failures。"""
    from app.schemas.evaluation import Evaluation

    pending = state["pending_question"]
    evaluations = state.get("evaluations", [])
    ev_dump = evaluations[-1] if evaluations else None
    if ev_dump is None:
        # 理论到不了这里（规则链对 skipped 已裁决 stop），防御性兜底
        raise ValueError("无评分结果，不能生成追问")
    evaluation = Evaluation.model_validate(ev_dump)

    try:
        fq = generate_followup(
            {**_question_context(state), "history": list(state.get("answer_history", []))[:-1]},
            state["latest_answer"], evaluation,
            asked_contents=list(state.get("asked_contents", [])),
        )
    except ModelCallError:
        raise  # 服务问题：重试有意义
    except Exception as exc:  # noqa: BLE001 —— 含 ModelOutputInvalidError
        log.warning("追问生成失败，跳过追问继续主流程: %s", exc)
        failed = FollowupDecision(
            decision="stop",
            reason_code=REASON_FOLLOWUP_FAILED,
            rule_version=RULE_VERSION,
            detail=f"追问生成失败：{str(exc)[:120]}",
        )
        return {
            "followup_failures": [{
                "main_index": state.get("current_main_index", 1),
                # 失败的是"这一题之后本该生成的那次追问"，按被答完的题记录坐标
                "answered_follow_up_index": state.get("current_followup_index", 0),
                "reason_code": failed.reason_code,
                "rule_version": failed.rule_version,
                "detail": failed.detail,
            }],
            "followup_build_failed": True,
        }

    next_index = state.get("current_followup_index", 0) + 1
    followup_pending = {
        "main_index": pending["main_index"],
        "kind": "follow_up",
        "follow_up_index": next_index,
        "content": fq.content,
        "category": pending.get("category", ""),
        "difficulty": pending.get("difficulty", ""),
        # 独立评价本条追问，不能因未重述主问题其他内容而扣分。
        "evaluation_rubric": [p.model_dump() for p in fq.evaluation_rubric],
        "material_evidence_ids": pending.get("material_evidence_ids", []),
    }
    return {
        "pending_question": followup_pending,
        "current_followup_index": next_index,
        "asked_contents": [fq.content],
        "followup_build_failed": False,
    }


def route_after_build_followup(state: InterviewGraphState) -> Literal["wait_answer", "advance"]:
    """追问生成成功就等待回答，失败就换主问题，避免重复展示刚答过的题。

    pending_question 还停留在已答过的题目上，直接 wait_answer 会把
    同一道题再问一遍（候选人重答 → 重复评分 → 死循环隐患）。
    失败就放弃追问，直接推进主流程。

    判定用 followup_build_failed（本次覆盖写），不再去翻
    followup_decisions 的最后一条——那个列表已经不含失败记录了。"""
    return "advance" if state.get("followup_build_failed") else "wait_answer"


def advance_node(state: InterviewGraphState) -> dict:
    """主问题编号加一并清零追问次数；没有下一道主问题时标记全部完成。"""
    questions = state["questions"]
    idx = state.get("current_main_index", 1)
    if idx >= len(questions):
        log.info("会话 %s 全部题目完成", state.get("session_id"))
        return {"finished": True}
    return {
        "current_main_index": idx + 1,
        "current_followup_index": 0,
    }


def route_after_advance(state: InterviewGraphState) -> Literal["dispatch", "__end__"]:
    """还有题就回到发题节点，全部完成就走图的结束出口。"""
    return "dispatch" if not state.get("finished") else "__end__"
