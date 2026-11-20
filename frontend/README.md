# AI 模拟面试系统 · 前端

本仓库的 Web 界面。React 19 + Vite 6 + Tailwind 4 + TypeScript。

后端位于 [`../backend/`](../backend/)（FastAPI + LangGraph + PostgreSQL + Redis），项目整体说明见 [根目录 README](../README.md)。

本目录负责界面与交互。

## 启动

```bash
cd frontend        # 从仓库根目录进入
npm ci
npm run dev        # http://localhost:5173
```

需要后端在 `127.0.0.1:8000` 运行。Vite 把 `/api` 同源代理到后端，
**所以匿名身份 Cookie 能自动携带，不需要任何 CORS 配置**——这是刻意的选择，
生产环境用 Nginx 反代 `/api` 即可，前端代码一行不用改。

```bash
npm run build      # 产物在 dist/
npx tsc --noEmit   # TypeScript 类型检查
npm run preview    # 本地预览构建结果
```

## 页面

```
侧边栏 ─ 首页 / 面试记录 / [+ 开始新面试]
  首页        问候、统计卡、未完成面试的继续入口、最近记录
  上传页      简历 + 可选 JD + 题量
  答题页      进度条、题目卡片、上一题反馈、回答框
  报告页      总分、五维能力、薄弱点、建议、逐条记录
  记录页      全部历史，可按状态筛选
```

登录后落在**首页**，不会直接进入答题流程。

## 目录

```
src/
├── App.tsx              路由与登录态
├── api.ts               全部接口调用与类型定义
├── shared.ts            状态映射、分数配色等共用逻辑
├── components/
│   ├── AppShell.tsx     侧边导航 + 移动端顶栏
│   ├── ui.tsx           Card / Button / Badge 等基础件
│   ├── ReportView.tsx   报告展示
│   ├── RecordsView.tsx  逐条问答记录
│   └── FeedbackCardView.tsx
└── pages/               Home / Setup / Session / Report / History
```

## 几处实现约定

**会话恢复不依赖 localStorage。** 一律读后端 `GET /api/interviews` 的 `resumable` 字段。
好处是换设备登录同一账号也能找回；本地存 key 只是假的安全感——清个 Cookie 就没了。

**提交回答带 `request_id`。** 网络超时后重试同一个 id，后端回放原响应而不是重复计分。
冲突时（`STALE_QUESTION`）页面会重新拉取当前题，而不是盲目重发。

**报告是只读视图。** 打开报告不触发任何模型调用；后端返回什么就渲染什么，
`overall_score` 为 `null` 时显示"暂无有效评分"，绝不用 0 分占位。

**轮询有终止条件。** 准备阶段按固定间隔查进度，页面切走或已出结果立即停止，
不留下野定时器。
