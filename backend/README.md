# AI 模拟面试系统 · 后端

匿名访客上传简历（+可选 JD），双模型生成面试题并逐题作答评分，全程可恢复、可审计。
按《AI面试系统开发说明》五阶段改造实现；架构：FastAPI + LangGraph(interrupt/resume) + PostgreSQL(正式记录) + Redis(checkpoint/锁/幂等缓存)。

## 启动

```bash
# 1. 基础设施（PostgreSQL 16@5433 + redis-stack@6380）
docker compose up -d

# 2. 依赖（Python 3.11+）
python -m venv ../.venv && ../.venv/Scripts/pip install -r requirements.txt

# 3. 配置：复制 .env.example 为 .env，填入两套模型 Key
#    TEXT_*  = deepseek-chat（出题/评分/追问）
#    VISION_* = qwen3.8-omni-flash（简历/JD 解析，千问 MaaS OpenAI 兼容协议）

# 4. 数据库迁移
../.venv/Scripts/python -m alembic upgrade head

# 5. 启动 API（接口文档 http://127.0.0.1:8000/docs）
../.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

前端在 `J:/develope/ai面试-前端`：`npm run dev` 后访问 http://localhost:5173（Vite 把 `/api` 同源代理到 8000，匿名 Cookie 无需跨源配置）。

## API 一览（详见 /docs OpenAPI）

| 接口 | 作用 |
| --- | --- |
| POST /api/interviews | multipart 上传简历(+可选JD)，202+session_id，后台解析出题 |
| GET /api/interviews/{id} | 轮询准备进度/进度/可恢复性（只读） |
| GET /api/interviews/{id}/current-question | 当前题；checkpoint 丢失时自动从 PG 重建 |
| POST /api/interviews/{id}/answers | 提交回答/跳过（question_id+request_id 幂等） |
| POST /api/interviews/{id}/finish | 用户提前结束（幂等） |
| GET /api/interviews/{id}/records | 正式问答记录（不含内部评分要点） |

错误统一 `{code, message, retryable}`；稳定错误码见 `app/core/errors.py`。

## 测试与验收

```bash
cd backend && ../.venv/Scripts/python -m pytest tests -q     # 130 passed（LLM_MOCK=1，零模型费用）
../.venv/Scripts/python scripts/verify_phase4_real.py         # 并发/幂等/恢复 真机验收（真 PG+Redis）
../.venv/Scripts/python scripts/verify_phase5_real.py         # HTTP 全流程 + 匿名隔离 真机验收
```

- 单元/集成测试默认 `LLM_MOCK=1`；连真实 PG/Redis 的用例在事务内回滚，不留脏数据。
- 真机验收脚本会真实调用 qwen3.8-omni-flash 与 deepseek-chat，产生少量费用。

## 可靠性边界（如实声明，不承诺 exactly-once）

- 回答提交按 `request_id` 幂等：同 id 重试回放原响应、绝不重复计分；不同 id 提交同题被 `STALE_QUESTION` 拒绝。
- 双保险：Redis 会话锁（SET NX + 自动续期 + Lua 校验释放）+ PostgreSQL `state_version` CAS；锁服务不可用返回 503，绝不无锁降级。
- Redis checkpoint 丢失：`GET /current-question` 自动从 PG 的材料/题目/回答/评分重建到安全等待点，并对账未完成提交（PENDING/PROCESSING → FAILED）。
- 网络超时可能已在模型供应商侧完成：目标是避免重复**业务效果**，复用已保存的阶段结果，不承诺模型调用恰好一次。
- 准备任务是进程内后台任务：服务重启会把孤儿 PREPARING 会话标记为 PREPARATION_FAILED，需重新上传；未实现持久任务队列。

## 隐私与安全

- 匿名身份 = HttpOnly/SameSite=Lax Cookie（token 只存 sha256）；UUID 不等于访问权限，归属校验在 SQL 里，跨访客一律 404；清 Cookie 即丢身份，默认无法找回记录。
- 写操作做 Origin 校验（CSRF）；生产 HTTPS 请设 `COOKIE_SECURE=1`。
- 对外评分剔除 `rubric_results`（评分要点不泄题）；日志不记简历内容与 Key。
- 简历/JD/回答均为不可信输入：材料内嵌指令不会改变评分规则。

## 已知限制

- 提交后修改答案未开放（首期一题一次，口径见开发说明 §7）。
- 长文本 PDF 仅取前 10 页（`MAX_PDF_PAGES`），单文件 ≤10MB。
- 会话保留 72 小时（`SESSION_TTL_HOURS`），到期后状态置 EXPIRED；正式记录留存策略待产品确认后补充清理任务。
