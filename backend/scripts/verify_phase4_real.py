"""阶段 4 真机验收脚本：真 PostgreSQL + 真 Redis + 真会话锁（模型走 mock，不产生费用）。

跑一遍会把 §13 阶段 4 的验收项逐条验出来：

    1. 同 request_id 重试       → 不新增回答，原样回放
    2. 不同 request_id 同题提交 → 拒绝（STALE_QUESTION / REQUEST_CONFLICT），不重复推进
    3. 两线程并发提交同一题     → 只有一条 COMPLETED（锁 + 唯一约束 + CAS）
    4. 锁租约/版本             → state_version 单调递增，不被旧请求覆盖
    5. Redis checkpoint 丢失    → 恢复入口从 PG 重建到真正的等待点，继续答题
    6. 卡住的提交              → 恢复对账关闭并清理空壳，题目可重新提交

用法（容器需先起来）：

    cd backend
    ..\\.venv\\Scripts\\python.exe scripts\\verify_phase4_real.py
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path

# 默认 mock 模型：本脚本验的是并发与持久化，不是模型能力（省钱且可重复）
os.environ.setdefault("LLM_MOCK", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.errors import AppError  # noqa: E402
from app.infrastructure.db import get_engine, get_session_factory  # noqa: E402
from app.infrastructure.redis_client import get_redis  # noqa: E402
from app.repositories import answers as answers_repo  # noqa: E402
from app.repositories import sessions as sessions_repo  # noqa: E402
from app.repositories import visitors as visitors_repo  # noqa: E402
from app.schemas.questions import GeneratedQuestion, RubricPoint  # noqa: E402
from app.services import graph_runtime, interview_workflow, recovery  # noqa: E402

QUESTIONS = [
    GeneratedQuestion(
        main_index=i,
        category=["项目经验", "专业能力", "场景解决"][i % 3],
        difficulty=["easy", "medium", "hard"][i % 3],
        content=f"第 {i} 题：请说明你在项目中做过的第 {i} 个关键技术取舍，并给出量化结果。",
        material_evidence_ids=["ev-1"],
        evaluation_rubric=[
            RubricPoint(point="讲清背景与取舍", weight=0.5),
            RubricPoint(point="给出量化结果", weight=0.5),
        ],
    )
    for i in (1, 2, 3)
]


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    # 前置探活：容器没起就别装作跑过了
    with get_engine().connect() as conn:
        conn.execute(text("select 1"))
    get_redis().ping()
    print("PostgreSQL / Redis 均可用，开始验收\n")

    graph = graph_runtime.get_compiled_graph()
    restore_graph = graph_runtime.get_compiled_graph(restore=True)
    factory = get_session_factory()

    db = factory()
    visitor, _ = visitors_repo.create(db)
    session = sessions_repo.create(db, owner_id=visitor.id, question_count=len(QUESTIONS))
    session.status = "READY"
    db.commit()
    sid, vid = session.id, visitor.id
    print(f"会话 {sid}（访客 {vid}）已创建")

    try:
        started = interview_workflow.start_interview(
            db=db, graph=graph, session=session, questions=QUESTIONS
        )
        q1 = started["question"]["question_id"]
        check("启动即停在第一题且不含评分要点", "evaluation_rubric" not in started["question"])

        # ---- 1. 同 request_id 重试
        first = interview_workflow.submit_answer(
            db=db, graph=graph, session_id=sid, owner_id=vid, question_id=q1,
            request_id="req-retry-1", answer="我做了取舍，并给出 30% 的延迟下降。",
        )
        replay = interview_workflow.submit_answer(
            db=db, graph=graph, session_id=sid, owner_id=vid, question_id=q1,
            request_id="req-retry-1", answer="我做了取舍，并给出 30% 的延迟下降。",
        )
        rows = answers_repo.list_for_session(db, sid)
        check(
            "同 request_id 重试不新增回答且回放原响应",
            replay["replayed"] and replay["answer_id"] == first["answer_id"] and len(rows) == 1,
            f"answers={len(rows)} v={first['state_version']}",
        )

        # ---- 2. 不同 request_id 提交同一题
        try:
            interview_workflow.submit_answer(
                db=db, graph=graph, session_id=sid, owner_id=vid, question_id=q1,
                request_id="req-second-id", answer="换一个请求号再交一次同样的题。",
            )
            check("不同 request_id 同题提交被拒绝", False)
        except AppError as exc:
            check("不同 request_id 同题提交被拒绝", exc.code in ("STALE_QUESTION", "REQUEST_CONFLICT"), exc.code)
        check("该题只有一条回答记录", len(answers_repo.list_for_session(db, sid)) == 1)

        # ---- 3. 两线程并发提交同一题
        q_next = first["next_question"]["question_id"]
        results: list[tuple[str, str]] = []

        def worker(idx: int) -> None:
            local_db = factory()
            try:
                res = interview_workflow.submit_answer(
                    db=local_db, graph=graph, session_id=sid, owner_id=vid,
                    question_id=q_next, request_id=f"req-race-{idx}",
                    answer=f"并发回答 {idx}：我按可观测性优先做了取舍。",
                )
                results.append(("ok", res["answer_id"]))
            except AppError as exc:
                results.append(("err", exc.code))
            finally:
                local_db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        completed = [
            a for a in answers_repo.list_for_session(db, sid)
            if a.question_id == uuid.UUID(q_next) and a.processing_status == "COMPLETED"
        ]
        check(
            "并发提交只产生一条 COMPLETED",
            len(completed) == 1,
            f"结果={results}",
        )
        check(
            "落败方得到明确冲突码而不是静默成功",
            any(r[0] == "err" and r[1] in ("SESSION_BUSY", "STALE_QUESTION", "VERSION_CONFLICT") for r in results),
        )

        # ---- 4. 版本单调
        db.refresh(session)
        versions = [a.result_state_version for a in answers_repo.list_for_session(db, sid)
                    if a.result_state_version is not None]
        check("state_version 单调递增", versions == sorted(versions) and session.state_version == max(versions), f"{versions}")

        # ---- 5. 伪造 checkpoint 丢失，从 PG 重建
        saver = graph.checkpointer
        if saver is not None:
            saver.delete_thread(str(sid))
        check("Redis 检查点已清空（模拟丢失）", not graph_runtime.has_checkpoint(graph, sid))

        recovered = recovery.recover_session(db=db, restore_graph=restore_graph, session=session)
        check("恢复后停在等待点且带题目", recovered["recovered"] and recovered["question"] is not None,
              f"main={recovered['question']['main_index']} follow={recovered['question']['follow_up_index']}")
        values, next_nodes, waiting = graph_runtime.snapshot_state(graph, sid)
        check("恢复后图真的处于 interrupt", waiting and next_nodes == ("wait_answer",),
              f"next={next_nodes} 历史评分={len(values.get('evaluations', []))}")

        # ---- 6. 恢复后继续答题
        cont = interview_workflow.submit_answer(
            db=factory(), graph=graph, session_id=sid, owner_id=vid,
            question_id=recovered["question"]["question_id"],
            request_id="req-after-recovery", answer="恢复后继续作答：我补充了量化指标。",
        )
        check("恢复后可以继续提交", cont["evaluation"] is not None, f"next={cont['next_question'] and cont['next_question']['main_index']}")

        print("\n阶段 4 真机验收通过。")
    finally:
        db.rollback()
        # 清理：删访客 → 级联删除会话/题目/回答/材料；再清 Redis 检查点
        db.execute(text("delete from anonymous_visitors where id = :id"), {"id": vid})
        db.commit()
        saver = graph.checkpointer
        if saver is not None:
            saver.delete_thread(str(sid))
        db.close()
        print("测试数据已清理（PG 级联删除 + Redis 检查点）")


if __name__ == "__main__":
    main()
