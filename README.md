# AI 模拟面试系统

上传简历（可附岗位 JD），系统解析后生成整套面试题，逐题作答并即时评分；结束后给出复盘报告。全程可中断、可恢复、可审计。

不是"套壳聊天"：把面试建模成状态机，题量、轮次、追问次数由代码控制，模型只负责"说什么"，不负责"流程往哪走"。

> 前端仓库：[ai-interview-platform-web](https://github.com/orange15175/ai-interview-platform-web)

## 流程

```
上传简历/JD ──▶ 解析成结构化画像 ──▶ 一次性预生成题库
                                        │
                                        ▼
                    ┌──────── 逐题作答（LangGraph interrupt 暂停等输入）
                    │              │
                    │              ▼
                    │        评分 → 规则判定是否追问 → 生成追问 / 推进下一题
                    │              │
                    └──────────────┘
                                        │
                                        ▼
                              复盘报告（总分 / 五维 / 薄弱点 / 建议）
```

## 技术栈

| 层 | 选型 | 用途 |
| --- | --- | --- |
| Web | FastAPI + Pydantic | 接口、校验、统一错误契约 |
| 编排 | LangGraph | `interrupt/resume` 让"等人打字"不占进程 |
| 存储 | PostgreSQL 16 | 正式记录（会话/题目/回答/评分/报告快照） |
| 缓存 | Redis | checkpoint、会话锁、幂等缓存 |
| 模型 | deepseek-chat / qwen3.8-omni-flash | 文本推理（出题评分追问） / 视觉解析（简历 JD） |
| 前端 | React 19 + Vite + Tailwind | 独立仓库 |

## 目录

```
.
├── backend/
│   ├── app/
│   │   ├── api/             HTTP 路由（auth / interviews / health）
│   │   ├── graph/           LangGraph 图：state、nodes、builder
│   │   ├── services/        业务编排：提交、恢复、补偿、报告
│   │   ├── agents/          模型调用与提示词（四个 Agent）
│   │   ├── repositories/    数据访问
│   │   ├── rules/           纯 Python 规则（追问判定）
│   │   ├── parsers/         简历/JD 解析流水线
│   │   └── models/          SQLAlchemy 表定义
│   ├── migrations/          Alembic 迁移
│   ├── tests/               单元 + 集成（默认零模型费用）
│   └── README.md            ★ 完整文档：接口、可靠性边界、隐私
├── build_optimization_doc.py   设计文档生成器（产物不入库）
└── .env.example                 配置模板（真实 Key 只放 .env）
```

## 快速开始

需要 Docker、Python 3.11+、Node 18+。

**1. 基础设施**

```bash
cd backend
docker compose up -d          # PostgreSQL 16@5433 + redis-stack@6380
```

**2. 后端**

```bash
python -m venv ../.venv
../.venv/Scripts/pip install -r requirements.txt
cp .env.example .env          # 填入 TEXT_API_KEY 与 VISION_API_KEY
../.venv/Scripts/python -m alembic upgrade head
../.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

接口文档：http://127.0.0.1:8000/docs

**3. 前端**（独立仓库）

```bash
git clone git@github.com:orange15175/ai-interview-platform-web.git
cd ai-interview-platform-web
npm install && npm run dev    # http://localhost:5173
```

**4. 测试**

```bash
../.venv/Scripts/python -m pytest tests -q
```

默认 `LLM_MOCK=1`，不连真实模型、不产生费用。

## 几个关键设计

**题目预生成，图只负责推进。** 准备阶段一次性生成整套题并落库，之后图按索引取题。所以同一份题库跑两遍轨迹一致——可复现、可回放。边聊边编题做不到这点。

**追问由规则决定，不由模型决定。** 模型生成追问内容，但"要不要追问、追几次"是 `rules/followup_rules.py` 里的纯 Python 判断。控制权必须收回，否则循环次数全凭模型心情。

**三道写入闸。** Redis 会话锁（谁是执行者）→ `write_epoch`（谁有权写）→ `state_version`（状态是否还是我以为的那版）。锁被接管后，迟到请求的结果一条也写不进去。

**评分失败不写 0 分。** 模型瞬时失败标为可重试，进后台补偿队列按退避重跑；不可重试的永不入队。没有有效评分时报告返回 `null` 并说明原因——0 分会被误读成"答得很差"。

**报告按需快照。** 首次打开时若所有题已有终态，聚合一次存进 `report_snapshot`；否则不写，照常现场算。避免"报告说 78、明细重算 82"这种无法解释的不一致。

## 已知边界

- **无频率限制**：公开部署前必须加网关限流，否则匿名接口可被刷消耗模型额度。
- **单 Worker 假设**：进程内登记表与图缓存不跨进程；多副本需外部存储或粘性会话。
- **不承诺 exactly-once**：网络超时后供应商侧可能已完成，目标是避免重复**业务效果**，复用已保存的阶段结果。
- 报告快照依赖"每题只允许提交一次"这一契约；若将来开放改答案，快照必须带版本号失效。

## 安全

- 真实密钥只在 `.env`（已 gitignore），仓库内仅有占位符模板。
- 简历、JD、回答均按不可信输入处理：材料内嵌指令不会改变评分规则。
- 对外响应剔除评分要点（`rubric_results`），不泄题。
- 归属校验写在 SQL 里，跨访客一律 404；UUID 不等于访问权限。
