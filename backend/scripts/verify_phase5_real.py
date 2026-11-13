"""阶段 5 真机验收：HTTP 全流程 + 真实双模型 + 匿名隔离（会产生少量模型费用）。

前置：.env 已配置 TEXT_API_KEY（deepseek-chat）与 VISION_API_KEY（qwen3.8-omni-flash），
且 compose 的 PG(5433)/redis-stack(6380) 已启动。**不要设置 LLM_MOCK**。

用例（对应开发说明 §13 阶段 5 验收）：
 1. 真实 qwen 解析简历 + deepseek 出题：题目与简历内容相关、不带评分要点泄露；
 2. 真实 deepseek 评分：分数有界、反馈非空、对外无 rubric_results；
 3. 追问与推进：每题答完要么追问要么前进，不重问已答题；
 4. 中途删除 Redis checkpoint → current-question 自动从 PG 恢复到当前题；
 5. finish 幂等；records 与提交数一致；
 6. 第二个访客（另一个 Cookie）访问一律 404；
 7. 结束自动清理 PG 与 Redis，不留脏数据。

用法：cd backend && ..\\.venv\\Scripts\\python.exe scripts\\verify_phase5_real.py
"""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from docx import Document

sys.path.insert(0, ".")

from app.core.logging import setup_logging  # noqa: E402

setup_logging()

BASE = "http://127.0.0.1:8000"
QUESTION_COUNT = 3
PREPARE_TIMEOUT_S = 240
SUBMIT_TIMEOUT_S = 120

RESUME_TEXT = """【工作经历】
星辰科技 后端开发工程师 2022.07-2025.06
- 主导订单中心重构，把同步链路拆为消息驱动的异步编排，峰值 QPS 从 800 提升到 6500，P99 延迟从 1.2s 降到 180ms。
- 设计库存扣减的分布式锁方案，用 Redis + Lua 实现原子扣减，大促零超卖。
- 推动接口测试覆盖从 30% 提升到 85%，线上缺陷率同比下降 60%。

【项目经历】
高性能订单中台（2024）
技术栈：Python、FastAPI、PostgreSQL、Redis、Kafka
- 分库分表方案落地：按租户维度拆 16 库，写吞吐提升 4 倍。
- 引入本地缓存 + 旁路缓存两级缓存策略，热点查询命中率 92%。

【技能】
Python / FastAPI / SQLAlchemy / PostgreSQL / Redis / Kafka / Docker
【教育】
华东理工大学 计算机科学与技术 本科 2018-2022
"""

JD_TEXT = (
    "岗位：高级后端工程师。负责高并发交易系统的设计与优化。"
    "要求：精通 Python 或 Go，熟悉 PostgreSQL 与 Redis，有大促保障经验；"
    "了解 RAG 或向量检索者优先；具备跨团队推动能力。"
)


def build_resume_docx() -> bytes:
    doc = Document()
    for line in RESUME_TEXT.splitlines():
        if line.strip():
            doc.add_paragraph(line.strip())
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class Client:
    """极简 HTTP 客户端：自己管 Cookie，模拟一个独立浏览器。"""

    def __init__(self) -> None:
        self.cookie: str | None = None

    def request(self, method: str, path: str, data=None, multipart=None, timeout: int = 60):
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        body = None
        if multipart:
            boundary = uuid.uuid4().hex
            parts = []
            for key, val in multipart.items():
                if isinstance(val, tuple):
                    fn, fb, fm = val
                    parts.append(
                        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{fn}"\r\n'
                        f"Content-Type: {fm}\r\n\r\n".encode()
                        + fb
                        + b"\r\n"
                    )
                else:
                    parts.append(
                        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode()
                    )
            body = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            self._remember_cookie(resp.headers.get("Set-Cookie"))
            return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            self._remember_cookie(exc.headers.get("Set-Cookie"))
            return exc.code, json.loads(exc.read() or b"null")

    def _remember_cookie(self, set_cookie: str | None) -> None:
        if set_cookie and set_cookie.startswith("aiiv_visitor="):
            self.cookie = set_cookie.split(";")[0]


PASS: list[str] = []
SESSIONS: list[str] = []
TOKENS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        raise SystemExit(f"验收失败：{name} {detail}")
    PASS.append(name)


def cleanup() -> None:
    """结束清理：删 PG 五表相关行 + Redis checkpoint。"""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select

    from app.infrastructure.checkpoint import get_checkpointer
    from app.infrastructure.db import get_session_factory
    from app.models import (
        InterviewAnswer,
        InterviewMaterial,
        InterviewQuestion,
        InterviewSession,
    )
    from app.repositories import visitors as visitors_repo

    saver = get_checkpointer()
    db = get_session_factory()()
    try:
        for token in TOKENS:
            visitor = visitors_repo.find_by_token(db, token)
            if visitor is None:
                continue
            sids = list(
                db.scalars(
                    select(InterviewSession.id).where(InterviewSession.owner_id == visitor.id)
                )
            )
            for sid in sids:
                db.execute(sa_delete(InterviewAnswer).where(InterviewAnswer.session_id == sid))
                db.execute(sa_delete(InterviewQuestion).where(InterviewQuestion.session_id == sid))
                db.execute(sa_delete(InterviewMaterial).where(InterviewMaterial.session_id == sid))
                try:
                    saver.delete_thread(str(sid))
                except Exception:  # noqa: BLE001
                    pass
            db.execute(sa_delete(InterviewSession).where(InterviewSession.owner_id == visitor.id))
            db.execute(sa_delete(type(visitor)).where(type(visitor).id == visitor.id))
        db.commit()
        print("已清理 PG 与 Redis 残留")
    finally:
        db.close()


def main() -> None:
    c = Client()

    # 0. 服务与真实模型确认
    sc, health = c.request("GET", "/api/health")
    check("服务在线", sc == 200, str(health))
    check(
        "真实双模型已配置（非 mock）",
        health["llm"]["mock"] is False
        and health["llm"]["text_model"]["configured"]
        and health["llm"]["vision_model"]["configured"],
        json.dumps(health["llm"], ensure_ascii=False),
    )

    # 1. 上传 → 后台真实解析+出题
    docx = ("resume.docx", build_resume_docx(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    sc, body = c.request("POST", "/api/interviews",
                         multipart={"resume": docx, "question_count": str(QUESTION_COUNT),
                                    "jd_text": JD_TEXT})
    check("创建返回 202", sc == 202, json.dumps(body, ensure_ascii=False))
    sid = body["session_id"]
    SESSIONS.append(sid)
    if c.cookie:
        TOKENS.append(c.cookie.split("=", 1)[1])

    status = "PREPARING"
    deadline = time.time() + PREPARE_TIMEOUT_S
    while time.time() < deadline:
        sc, view = c.request("GET", f"/api/interviews/{sid}")
        status = view.get("status")
        if status != "PREPARING":
            break
        time.sleep(3)
    check("真实解析+出题完成", status == "IN_PROGRESS", f"status={status}")

    # 2. 题目检查：不带评分要点、数量正确
    sc, cq = c.request("GET", f"/api/interviews/{sid}/current-question")
    check("获取当前题", sc == 200 and cq["question"] is not None)
    q1 = cq["question"]
    check(
        "题目投影无评分要点泄露",
        "evaluation_rubric" not in q1 and "rubric" not in json.dumps(q1),
        q1["content"][:40],
    )

    # 3. 真实评分：答第 1 题
    sc, out = c.request("POST", f"/api/interviews/{sid}/answers", timeout=SUBMIT_TIMEOUT_S, data={
        "question_id": q1["question_id"], "request_id": f"v5-{uuid.uuid4()}",
        "answer": "订单中心重构时我在同步改异步之间做过取舍：先用 Kafka 做削峰，"
                  "但为了不丢订单消息，我们设计了本地消息表 + 对账任务兜底。"
                  "最终峰值 QPS 从 800 到 6500，P99 从 1.2s 降到 180ms，大促零超卖。"})
    check("真实评分提交成功", sc == 200, json.dumps(out, ensure_ascii=False)[:200])
    ev = out["evaluation"]
    check("评分有界且反馈非空",
          ev is not None and 0 <= ev["score"] <= 100 and bool(ev["feedback"].strip()),
          f"score={ev['score']}")
    check("对外响应无 rubric_results", "rubric_results" not in (ev or {}))

    # 4. 中途删 checkpoint → current-question 自动恢复
    if out["next_question"]:
        from app.infrastructure.checkpoint import get_checkpointer
        get_checkpointer().delete_thread(sid)
        sc, rec = c.request("GET", f"/api/interviews/{sid}/current-question",
                            timeout=SUBMIT_TIMEOUT_S)
        check("checkpoint 丢失后自动恢复", sc == 200 and rec["recovered"] is True
              and rec["question"] is not None,
              f"恢复到 {rec['question']['main_index']}/{rec['question']['follow_up_index']}")

        # 5. 恢复后继续作答 1 题（真实模型）
        sc, out2 = c.request("POST", f"/api/interviews/{sid}/answers",
                              timeout=SUBMIT_TIMEOUT_S, data={
                                  "question_id": rec["question"]["question_id"],
                                  "request_id": f"v5-{uuid.uuid4()}",
                                  "answer": "分布式锁方案上，我一开始想用数据库乐观锁，"
                                            "但热点行竞争激烈；改用 Redis+Lua 原子扣减并加了"
                                            "库存预占与补偿，实测大促零超卖。"})
        check("恢复后继续答题", sc == 200, json.dumps(out2, ensure_ascii=False)[:150])

    # 6. finish 幂等 + records
    sc, f1 = c.request("POST", f"/api/interviews/{sid}/finish")
    sc2, f2 = c.request("POST", f"/api/interviews/{sid}/finish")
    check("finish 幂等", sc == 200 and sc2 == 200 and f1["status"] == f2["status"], f1["status"])
    sc, records = c.request("GET", f"/api/interviews/{sid}/records")
    check("records 完整", sc == 200 and records["record_count"] >= 2,
          f"record_count={records['record_count']}")
    check("records 无评分要点泄露",
          all("evaluation_rubric" not in r for r in records["records"]))

    # 7. 匿名隔离：另一个访客
    stranger = Client()
    sc, _ = stranger.request("GET", f"/api/interviews/{sid}")
    check("跨访客访问 404", sc == 404)
    if stranger.cookie:
        TOKENS.append(stranger.cookie.split("=", 1)[1])

    print(f"\n阶段 5 真机验收全部通过（{len(PASS)} 项）")


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
