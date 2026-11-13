"""阶段 2 真实链路验证（手工运行，不在 pytest 收集中）。

真实调用 qwen3.8-omni-flash 与 deepseek-chat，会消耗真实 token。
运行方式：cd backend && ../.venv/Scripts/python.exe scripts/verify_e2e_real.py


简历 DOCX → 解析（qwen3.8-omni-flash）→ 出题（deepseek-chat）
不走 mock，真实消耗 token（约几千），用于确认双模型契约能对上。
"""

from __future__ import annotations

import io
import json

from docx import Document

from app.agents.parse_agent import parse_materials
from app.agents.question_agent import generate_questions
from app.parsers.pipeline import build_parsed_input


def make_resume_docx() -> bytes:
    doc = Document()
    doc.add_heading("李明的简历", level=1)
    doc.add_paragraph("求职方向：后端开发 / AI 应用工程师")
    doc.add_paragraph("联系方式：liming@example.com | 138-0000-0000")

    doc.add_heading("教育背景", level=2)
    doc.add_paragraph("2018.09 - 2022.06  某某大学  计算机科学与技术  本科")

    doc.add_heading("工作经历", level=2)
    doc.add_paragraph("2024.03 - 至今  某科技公司  高级后端工程师")
    doc.add_paragraph("负责 AI 客服平台后端，使用 LangGraph 编排多 agent 流程，"
                      "接入 DeepSeek 与千问双模型，客服首响时间从 45 秒降到 8 秒。")
    doc.add_paragraph("2022.07 - 2024.02  某电商公司  后端开发工程师")
    doc.add_paragraph("负责订单中台，基于 FastAPI + PostgreSQL 重构下单链路，"
                      "峰值 QPS 从 800 提升到 2200，接口 P99 延迟下降 40%。")

    doc.add_heading("项目经历", level=2)
    doc.add_paragraph("智能客服多轮对话系统（2025）")
    doc.add_paragraph("使用 LangGraph 实现 supervisor 多 agent 架构，Redis 做会话状态存储，"
                      "意图识别准确率 88%，支持人工接管。")

    doc.add_heading("技能", level=2)
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "语言"
    table.cell(0, 1).text = "Python, TypeScript, SQL"
    table.cell(1, 0).text = "框架"
    table.cell(1, 1).text = "FastAPI, LangGraph, Celery"
    table.cell(2, 0).text = "中间件"
    table.cell(2, 1).text = "PostgreSQL, Redis, Docker, K8s"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def main() -> None:
    data = make_resume_docx()
    print(f"[1] 简历 DOCX 已生成：{len(data)} 字节")

    parsed = build_parsed_input(
        resume_filename="resume.docx",
        resume_bytes=data,
        jd_text="岗位：AI 应用后端工程师。要求：熟悉 Python、FastAPI、LangGraph，"
                "有 LLM 应用落地经验，了解 RAG 与多 agent 编排。",
    )
    print(f"[2] 提取完成：文本块 {len(parsed.text_blocks)} 个，"
          f"渲染块 {len(parsed.render_blocks)} 个，警告 {parsed.warnings}")

    materials = parse_materials(parsed)
    r = materials.resume
    print("[3] 千问解析完成：")
    print(f"    教育 {len(r.education)} 条 | 经历 {len(r.experiences)} 条 "
          f"| 项目 {len(r.projects)} 条 | 技能 {len(r.skills)} 项")
    print(f"    技能列表：{r.skills}")
    print(f"    evidence 依据：{len(materials.evidence)} 条")
    print(f"    不确定字段：{materials.uncertain_fields or '无'}")
    print(f"    警告：{materials.warnings or '无'}")

    qs = generate_questions(materials, count=5)
    print(f"[4] DeepSeek 出题完成：{len(qs.questions)} 道")
    for q in qs.questions:
        weights = q.rubric_weight_sum
        print(f"    Q{q.main_index} [{q.category}/{q.difficulty}] {q.content[:70]}")
        print(f"        评分要点 {len(q.evaluation_rubric)} 条，权重和 {weights:.2f}，"
              f"依据 {q.material_evidence_ids or '(无)'}")

    print("\n[OK] 全链路真实调用成功")
    print(json.dumps({"skills_count": len(r.skills), "questions": len(qs.questions)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
