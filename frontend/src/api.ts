/**
 * 后端 API 封装（阶段 5 契约：/api/interviews 六个接口）。
 *
 * 设计要点：
 * - 同源代理：默认走 vite/nginx 的 /api 反代，Cookie（匿名身份）自动携带；
 *   VITE_API_BASE 可覆盖为直连地址（直连时后端要配 ALLOWED_ORIGINS）。
 * - 错误统一成 ApiError{code, message, retryable}：后端领域错误原样透出，
 *   网络断连归为 NETWORK_ERROR。所有 code 见后端 app/core/errors.py。
 * - 绝不缓存提交结果：幂等由后端按 request_id 保证（同 id 重试原样回放）。
 */

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? ''

export interface ApiError {
  code: string
  message: string
  retryable: boolean
  /** HTTP 状态码，便于区分"没连上"与"被拒绝" */
  status?: number
}

export class RequestFailed extends Error implements ApiError {
  code: string
  message: string
  retryable: boolean
  status?: number

  constructor(err: ApiError) {
    super(err.message)
    this.code = err.code
    this.message = err.message
    this.retryable = err.retryable
    this.status = err.status
  }
}

// ---------------------------------------------------------------- 契约类型

export interface QuestionView {
  question_id: string
  kind: 'main' | 'follow_up'
  main_index: number
  follow_up_index: number
  content: string
  category: string
  difficulty: string
  total_questions?: number | null
}

export interface EvaluationView {
  score: number
  feedback: string
  dimension_scores?: Record<string, number> | null
  missing_points?: string[]
  follow_up_suggested?: boolean
}

export interface DecisionView {
  decision: string
  reason_code: string
}

export interface SubmitAnswerResponse {
  answer_id: string
  question_id: string
  evaluation: EvaluationView | null
  followup_decision: DecisionView | null
  next_question: QuestionView | null
  finished: boolean
  state_version: number
  replayed: boolean
  /** 图已推进、数据库补写出来的结果（不是重跑模型得来的） */
  rebuilt?: boolean
}

/** 准备阶段进度（后端 session.progress 的受控字段子集）。 */
export interface PreparationProgress {
  stage?: string
  stage_started_at?: string
  updated_at?: string
  text_blocks?: number
  render_blocks?: number
  char_count?: number
  question_count?: number
  retry_count?: number
  failed_stage?: string
  error_code?: string
  sanitized_error?: string
  answered_count?: number
}

export interface ProgressView {
  session_id: string
  status:
    | 'PREPARING'
    | 'IN_PROGRESS'
    | 'FINISHED'
    | 'CANCELLED'
    | 'EXPIRED'
    | 'PREPARATION_FAILED'
  state_version: number
  current_question?: QuestionView | null
  answered_count?: number
  question_count?: number
  finished: boolean
  recoverable?: boolean
  progress?: PreparationProgress
  sanitized_error?: string
}

export interface CurrentQuestionView {
  session_id: string
  status: string
  question: QuestionView | null
  finished: boolean
  state_version: number
  recovered: boolean
}

export interface AnswerRecord {
  question_id: string
  kind: 'main' | 'follow_up'
  main_index: number
  follow_up_index: number
  category: string
  content: string
  answer_id: string
  skipped: boolean
  answer_content: string | null
  score: number | null
  evaluation: EvaluationView | null
  followup_decision: DecisionView | null
}

export interface RecordsView {
  session_id: string
  status: string
  record_count: number
  records: AnswerRecord[]
}

/** 列表页一行：不含题干与作答原文，只有渲染卡片需要的信息。 */
export interface SessionListItem {
  session_id: string
  status: ProgressView['status']
  question_count: number
  answered_count: number
  /** 没有有效评分时是 null（不是 0） */
  overall_score: number | null
  created_at: string
  last_activity_at: string
  finished: boolean
  resumable: boolean
}

// ---------------------------------------------------------------- 复盘报告
//
// 报告是**只读聚合**：后端不再调模型，也不会返回评分要点（rubric）。
// 所以同一份记录刷新多少次，结论都一样——这点对"复盘"很重要。

export interface ReportSummary {
  question_count: number
  completed_count: number
  skipped_count: number
  failed_count: number
  incomplete_count: number
  /** 没有有效评分时是 null，**不是 0**：0 分是"答得很差"，不是"没数据" */
  overall_score: number | null
  score_coverage: number
  followup_count: number
}

export interface DimensionScore {
  score: number | null
  sample_count: number
}

export interface CategorySummary {
  category: string
  question_count: number
  completed_count: number
  average_score: number | null
}

export interface WeakPoint {
  point: string
  count: number
  question_ids: string[]
}

export interface AnswerEvidence {
  question_id: string
  main_index: number
  follow_up_index: number
  kind: 'main' | 'follow_up'
  content: string
  answer_excerpt: string
  score: number
  feedback: string
}

export interface InterviewReport {
  session_id: string
  status: string
  is_final: boolean
  generated_at: string
  summary: ReportSummary
  dimension_scores: Record<string, DimensionScore>
  category_summary: CategorySummary[]
  weak_points: WeakPoint[]
  evidence: AnswerEvidence[]
  suggestions: string[]
  /** 数据不足 / 会话未结束时的如实说明 */
  notice: string | null
}

// ---------------------------------------------------------------- 请求核心

async function request<T = any>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      // 必须是 include，不能用 same-origin：一旦把 VITE_API_BASE 指向别的域
      // （前后端分开部署），same-origin 会让请求**不带访客 Cookie**，
      // 后端每个请求都新建访客，答题记录当场全丢，而且不报任何错。
      // 同源部署时 include 与 same-origin 行为一致，只是多带一个判据。
      credentials: 'include',
      ...init,
    })
  } catch {
    throw new RequestFailed({
      code: 'NETWORK_ERROR',
      message: '连不上后端服务，请确认后端已启动',
      retryable: true,
    })
  }

  if (!res.ok) {
    let body: ApiError = {
      code: `HTTP_${res.status}`,
      message: `请求失败（HTTP ${res.status}）`,
      retryable: res.status >= 500,
    }
    try {
      const parsed = await res.json()
      if (parsed && parsed.code) body = { ...body, ...parsed }
    } catch {
      /* 响应不是 JSON：保留通用错误 */
    }
    throw new RequestFailed({ ...body, status: res.status })
  }
  return res.json()
}

// ---------------------------------------------------------------- 账号
//
// 登录态是后端下发的 HttpOnly Cookie（HMAC 签名），前端**拿不到也存不了**令牌：
// 只能问 /api/auth/me "我是谁"，靠 Cookie 自动携带。这也意味着刷新页面后
// 身份不丢，但前端不能自己伪造登录态（localStorage 里绝不存用户信息）。

export interface AuthUser {
  user_id: string
  account: string
  created_at: string | null
  last_login_at: string | null
}

export function getMe(): Promise<{ user: AuthUser | null }> {
  return request('/api/auth/me')
}

export function registerAccount(account: string, password: string): Promise<{ user: AuthUser }> {
  return request('/api/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account, password }),
  })
}

export function loginAccount(account: string, password: string): Promise<{ user: AuthUser }> {
  return request('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account, password }),
  })
}

export function logoutAccount(): Promise<{ ok: boolean }> {
  return request('/api/auth/logout', { method: 'POST' })
}

// ---------------------------------------------------------------- 六个接口

export function createInterview(form: FormData): Promise<{ session_id: string }> {
  return request('/api/interviews', { method: 'POST', body: form })
}

/**
 * 面试记录列表（按当前身份，最近的在前）。
 *
 * 归属判定在后端 SQL 里：登录态按账号，匿名态按访客 Cookie 且排除已被认领的会话。
 * 前端不要自己按 localStorage 过滤——那会把"换设备登录后找不回"当成正常现象。
 */
export function listSessions(limit = 20, offset = 0): Promise<SessionListItem[]> {
  return request(`/api/interviews?limit=${limit}&offset=${offset}`)
}

export function getProgress(sessionId: string): Promise<ProgressView> {
  return request(`/api/interviews/${sessionId}`)
}

export function getCurrentQuestion(sessionId: string): Promise<CurrentQuestionView> {
  return request(`/api/interviews/${sessionId}/current-question`)
}

export function submitAnswer(
  sessionId: string,
  body: {
    question_id: string
    request_id: string
    answer?: string
    submission_kind?: 'skip'
    expected_state_version?: number
  },
): Promise<SubmitAnswerResponse> {
  return request(`/api/interviews/${sessionId}/answers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function finishInterview(sessionId: string): Promise<{
  session_id: string
  status: string
  state_version: number
  finished: boolean
}> {
  return request(`/api/interviews/${sessionId}/finish`, { method: 'POST' })
}

export function getRecords(sessionId: string): Promise<RecordsView> {
  return request(`/api/interviews/${sessionId}/records`)
}

/**
 * 复盘报告（只读聚合，不调模型、不含评分要点）。
 *
 * 会话还在答题中也能取：此时只覆盖已完成的题，`notice` 会写明；
 * 没有有效评分时 `summary.overall_score` 是 null，前端不能拿 0 兜底。
 */
export function getReport(sessionId: string): Promise<InterviewReport> {
  return request(`/api/interviews/${sessionId}/report`)
}

/**
 * 重试准备（只重跑出题）：解析结果已经存在时，不必重传简历。
 * 材料没存下来时后端返回 MATERIAL_MISSING，前端应引导用户重新上传。
 */export function retryPreparation(sessionId: string): Promise<{
  session_id: string
  status: string
  retry_count: number
  state_version: number
}> {
  return request(`/api/interviews/${sessionId}/retry-preparation`, { method: 'POST' })
}

/** 显式恢复：把会话从 PostgreSQL 重建到安全等待点（不重新评分、不越题）。 */
export function recoverInterview(sessionId: string): Promise<CurrentQuestionView> {
  return request(`/api/interviews/${sessionId}/recover`, { method: 'POST' })
}
