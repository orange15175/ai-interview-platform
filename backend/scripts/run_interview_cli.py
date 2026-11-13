"""新面试流程的命令行运行器（阶段 3 验收用）。

用法：
    cd backend
    # 真实模型（需 .env 配好 key）：
    ..\\.venv\\Scripts\\python.exe scripts\\run_interview_cli.py
    # 不花钱走 mock（题目固定、回答随意）：
    set LLM_MOCK=1 && ..\\.venv\\Scripts\\python.exe scripts\\run_interview_cli.py

流程：mock 简历 → 出题 Agent（真实 DeepSeek / mock）→ 逐题作答 →
评分 → 追问 → 收尾。图用 MemorySaver 存状态，全程单进程。
"""

from __future__ import annotations

import os
import sys
import uuid

# 允许以脚本方式直接运行：把 backend 目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台中文编码兜底
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 无 reconfigure 属性时忽略
        pass

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.agents.parse_agent import parse_materials  # noqa: E402
from app.agents.question_agent import generate_questions  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.graph import build_interview_graph  # noqa: E402
from app.parsers.pipeline import build_parsed_input  # noqa: E402

# 极简 demo 简历（跑通链路用；真实效果请等简历上传接入后体验）
_DEMO_RESUME_LINES = [
    "求职方向：后端 / AI 应用工程师",
    "教育：某某大学 计算机科学与技术 本科（2018-2022）",
    "经历1：2022-2024 某电商公司 后端开发。用 FastAPI 与 PostgreSQL 重构订单中台，"
    "峰值 QPS 从 800 提升到 2200，P99 延迟下降 40%。",
    "经历2：2024-至今 某科技公司 高级后端。用 LangGraph 搭建 AI 客服多 agent 系统，"
    "接入 DeepSeek 与千问双模型，首响时间从 45 秒降到 8 秒。",
    "技能：Python, FastAPI, LangGraph, PostgreSQL, Redis, Docker",
]


def _demo_resume_docx() -> bytes:
    """把 demo 简历打包成真实 DOCX——上传校验只认 DOCX/PDF/图片。"""
    import io

    from docx import Document

    doc = Document()
    doc.add_heading("测试候选人简历", level=1)
    for line in _DEMO_RESUME_LINES:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def main() -> None:
    setup_logging()
    s = get_settings()
    mode = "MOCK（不联网）" if s.llm_mock else "真实模型"
    print(f"=== AI 模拟面试 · 命令行版 ===  [模型模式：{mode}]")
    print()

    # 准备阶段：解析 demo 简历 → 出题
    print("[准备] 解析简历并生成题目……")
    parsed = build_parsed_input(resume_filename="resume.docx",
                                resume_bytes=_demo_resume_docx())
    materials = parse_materials(parsed)
    count = min(s.business.question_count, 3)  # 命令行演示只出 3 题
    qs = generate_questions(materials, count=count)
    print(f"[准备] 共 {len(qs.questions)} 道主问题，每题最多追问 {s.business.max_followups} 次\n")

    graph = build_interview_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = graph.invoke(
        {"session_id": cfg["configurable"]["thread_id"],
         "questions": [q.model_dump() for q in qs.sorted_questions()]},
        cfg,
    )

    turn = 0
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        tag = "追问" if payload["kind"] == "follow_up" else f"第 {payload['main_index']}/{payload['total_questions']} 题"
        print(f"【{tag} · {payload['category']} · {payload['difficulty']}】")
        print(f"{payload['content']}")
        if payload.get("error"):
            print(f"⚠ {payload['error']}")
        print()

        raw = input("你的回答（回车=跳过本题，输入 q 退出）> ").strip()
        if raw.lower() == "q":
            print("已退出。")
            return
        turn += 1
        resume = {"skipped": True} if not raw else {"answer": raw}
        result = graph.invoke(Command(resume=resume), cfg)

        # 展示评分反馈（来自图状态，而不是前端契约）
        state = graph.get_state(cfg).values
        evals = state.get("evaluations", [])
        if evals and evals[-1] is not None:
            ev = evals[-1]
            print(f"\n[评分 {ev['score']} 分 · 来源 {ev['score_source']}] {ev['feedback']}")
            dec = state.get("followup_decisions", [])
            if dec:
                print(f"[追问裁决] {dec[-1]['decision']} / {dec[-1]['reason_code']}")
        else:
            print("\n[已跳过，本题不评分]")
        print()

    print("=" * 40)
    print("面试结束。评分记录：")
    for i, ev in enumerate(graph.get_state(cfg).values.get("evaluations", []), 1):
        if ev is None:
            print(f"  第 {i} 轮：跳过")
        else:
            print(f"  第 {i} 轮：{ev['score']} 分 - {ev['feedback'][:40]}")


if __name__ == "__main__":
    main()
