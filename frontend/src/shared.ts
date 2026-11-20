/**
 * 前端跨页共享的常量与本地存储读写。
 *
 * 为什么单独一个文件：会话恢复相关的键（sessionId / 准备起始时间 / 待提交项）
 * 被首页、答题页、报告页同时读写，散在各页面里最容易出现"一个页面清了另一个
 * 还在用"，导致重复提交同一道题。
 */

import type { DecisionView, EvaluationView, QuestionView } from './api'

const PREP_STARTED_KEY_PREFIX = 'aiiv_prep_started_at:'
const PENDING_KEY = 'aiiv_pending_submit'

export const RESUME_ACCEPT = '.docx,.pdf,.jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff'
export const MAX_FILE_MB = 10
export const JD_TEXT_MIN_CHARS = 10
export const POLL_BASE_MS = 2000
export const POLL_MAX_ATTEMPTS = 6

/** 准备中：解析材料 → 生成题；答题中：逐题作答；failed 单列，可"重试出题"。 */
export type Phase = 'preparing' | 'question' | 'finished' | 'failed'

export interface PendingSubmit {
  sessionId: string
  questionId: string
  requestId: string
  answer: string
  skip: boolean
}

export interface FeedbackCard {
  evaluation: EvaluationView | null
  decision: DecisionView | null
  questionLabel: string
}

/** 准备阶段的中文名。后端只给稳定标识，文案在前端维护。 */
export const PREP_STAGE_LABEL: Record<string, string> = {
  QUEUED: '已受理，等待开始',
  VALIDATING_UPLOAD: '校验上传文件',
  PARSING_MATERIAL: '解析简历与 JD',
  VALIDATING_MATERIAL: '检查解析结果',
  GENERATING_QUESTIONS: '生成面试题',
  VALIDATING_QUESTIONS: '校验题目',
  PERSISTING_QUESTIONS: '保存题目',
  WAITING_ANSWER: '准备完成',
  PREPARATION_FAILED: '准备失败',
}

export function newRequestId(): string {
  return crypto.randomUUID?.() ?? `req-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function readPending(): PendingSubmit | null {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as PendingSubmit
    if (!parsed?.requestId || !parsed?.questionId || !parsed?.sessionId) return null
    return parsed
  } catch {
    return null
  }
}

export function writePending(p: PendingSubmit | null) {
  try {
    if (p) sessionStorage.setItem(PENDING_KEY, JSON.stringify(p))
    else sessionStorage.removeItem(PENDING_KEY)
  } catch {
    /* 隐私模式下 sessionStorage 可能不可写：退化为内存态，不影响提交 */
  }
}

function prepStartedKey(sessionId: string): string {
  return `${PREP_STARTED_KEY_PREFIX}${sessionId}`
}

export function readPrepStartedAt(sessionId: string): number | null {
  try {
    const value = Number(localStorage.getItem(prepStartedKey(sessionId)))
    return Number.isFinite(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

export function writePrepStartedAt(sessionId: string, value: number) {
  try {
    localStorage.setItem(prepStartedKey(sessionId), String(value))
  } catch {
    /* 隐私模式下 localStorage 可能不可写，计时仍可在当前页面工作 */
  }
}

export function clearPrepStartedAt(sessionId: string | null) {
  if (!sessionId) return
  try {
    localStorage.removeItem(prepStartedKey(sessionId))
  } catch {
    /* ignore storage failures */
  }
}

export function scoreClass(score: number) {
  if (score >= 85) return 'bg-emerald-50 text-emerald-700 border-emerald-200'
  if (score >= 70) return 'bg-blue-50 text-blue-700 border-blue-200'
  if (score >= 60) return 'bg-amber-50 text-amber-700 border-amber-200'
  return 'bg-red-50 text-red-700 border-red-200'
}

export function questionLabel(q: QuestionView): string {
  return q.kind === 'follow_up'
    ? `第 ${q.main_index} 题 · 追问 ${q.follow_up_index}`
    : `第 ${q.main_index} 题`
}

/** 相对时间：列表页只需要"刚刚 / 3 小时前 / 11月12日"这种粒度。 */
export function relativeTime(iso: string): string {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return ''
  const diff = Date.now() - t
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时前`
  const day = Math.floor(hour / 24)
  if (day < 7) return `${day} 天前`
  const d = new Date(t)
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

export const STATUS_LABEL: Record<string, string> = {
  PREPARING: '准备中',
  IN_PROGRESS: '答题中',
  FINISHED: '已完成',
  CANCELLED: '已结束',
  EXPIRED: '已过期',
  PREPARATION_FAILED: '准备失败',
}

/** 状态对应的胶囊配色：进行中偏蓝，异常偏红，其余中性。 */
export function statusClass(status: string): string {
  if (status === 'IN_PROGRESS') return 'bg-blue-50 text-blue-700 ring-blue-100'
  if (status === 'PREPARING') return 'bg-indigo-50 text-indigo-700 ring-indigo-100'
  if (status === 'PREPARATION_FAILED') return 'bg-red-50 text-red-700 ring-red-100'
  return 'bg-gray-100 text-gray-600 ring-gray-200'
}
