/**
 * 答题页：准备中 → 逐题作答 → 结束（跳转报告）。
 *
 * 可靠性口径（与后端契约一致）：
 * - 待重试的提交落 sessionStorage（含 request_id）：刷新不等于放弃这道题；
 * - 刷新后先用**同一个 request_id** 续跑，绝不用新 id 重复提交同一题；
 * - 轮询单飞（上一轮没回来不发起下一轮），并按错误类型分级：
 *   404/过期 → 停止并退出；5xx/网络 → 有限次退避重试；准备失败 → 停止并说明。
 *
 * 这里**不渲染报告**：结束后交给报告页，答题页只管答题。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  RequestFailed,
  finishInterview,
  getCurrentQuestion,
  getProgress,
  recoverInterview,
  retryPreparation,
  submitAnswer,
  type ApiError,
  type PreparationProgress,
  type QuestionView,
} from '../api'
import FeedbackCardView from '../components/FeedbackCardView'
import {
  POLL_BASE_MS,
  POLL_MAX_ATTEMPTS,
  PREP_STAGE_LABEL,
  clearPrepStartedAt,
  newRequestId,
  questionLabel,
  readPending,
  readPrepStartedAt,
  writePending,
  writePrepStartedAt,
  type FeedbackCard,
  type PendingSubmit,
  type Phase,
} from '../shared'

export default function SessionPage({
  sessionId,
  onFinished,
  onExit,
}: {
  sessionId: string
  onFinished: (sessionId: string) => void
  onExit: () => void
}) {
  const [phase, setPhase] = useState<Phase>('preparing')
  const [question, setQuestion] = useState<QuestionView | null>(null)
  const [input, setInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [feedback, setFeedback] = useState<FeedbackCard | null>(null)
  const [recovered, setRecovered] = useState(false)
  const [recoveryNeeded, setRecoveryNeeded] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [prepStage, setPrepStage] = useState<string>('QUEUED')
  const [prepProgress, setPrepProgress] = useState<PreparationProgress | null>(null)
  const [prepStartedAt, setPrepStartedAt] = useState(() => Date.now())
  const [prepFailure, setPrepFailure] = useState<{ message: string; code: string } | null>(null)
  const [retryingPrep, setRetryingPrep] = useState(false)
  const [now, setNow] = useState(() => Date.now())

  const pendingRef = useRef<PendingSubmit | null>(null)
  const bootstrappedRef = useRef(false)
  // 首次问过后端再开始轮询：phase 初值是 preparing，不 gate 会重复打一次 getProgress
  const [booted, setBooted] = useState(false)

  // ---------------------------------------------------------------- 加载当前题

  const loadQuestion = useCallback(
    async (sid: string) => {
      try {
        const view = await getCurrentQuestion(sid)
        setRecovered(view.recovered)
        setRecoveryNeeded(false)
        if (view.finished || !view.question) {
          setQuestion(null)
          onFinished(sid)
        } else {
          setQuestion(view.question)
          setPhase('question')
        }
        setError(null)
      } catch (e) {
        setError(toApiError(e))
      }
    },
    [onFinished],
  )

  // ---------------------------------------------------------------- 提交回答

  const doSubmit = useCallback(
    async (payload: PendingSubmit) => {
      setSubmitting(true)
      pendingRef.current = payload
      writePending(payload)
      try {
        const res = await submitAnswer(payload.sessionId, {
          question_id: payload.questionId,
          request_id: payload.requestId,
          ...(payload.skip ? { submission_kind: 'skip' as const } : { answer: payload.answer }),
        })
        pendingRef.current = null
        writePending(null)
        setRecoveryNeeded(false)
        setFeedback({
          evaluation: res.evaluation,
          decision: res.followup_decision,
          questionLabel: payload.skip
            ? '本题（跳过）'
            : question
              ? questionLabel(question)
              : '上一题',
        })
        setInput('')
        setError(null)
        if (res.finished) {
          onFinished(payload.sessionId)
        } else if (res.next_question) {
          setQuestion(res.next_question)
          setPhase('question')
        } else {
          await loadQuestion(payload.sessionId)
        }
      } catch (e) {
        const err = toApiError(e)
        switch (err.code) {
          case 'STALE_QUESTION':
          case 'REQUEST_CONFLICT':
            // 这题已经被推进过：刷新到真正的当前题，重新作答
            setInput('')
            pendingRef.current = null
            writePending(null)
            await loadQuestion(payload.sessionId)
            break
          case 'SESSION_FINISHED':
            pendingRef.current = null
            writePending(null)
            onFinished(payload.sessionId)
            break
          case 'SESSION_NOT_FOUND':
          case 'SESSION_EXPIRED':
            writePending(null)
            onExit()
            break
          case 'STATE_RECOVERY_REQUIRED':
            // 图与记录对不上：保留原文与 request_id，点"恢复并继续"再提交
            setRecoveryNeeded(true)
            setError(err)
            break
          default:
            // 可重试错误：保留同一 request_id，用户点重试即可，不会重复计分
            setError(err)
        }
      } finally {
        setSubmitting(false)
      }
    },
    [loadQuestion, onExit, onFinished, question],
  )

  /** 用同一个 request_id 续跑待提交项（刷新后也走这条路）。 */
  const resumePending = useCallback(
    async (sid: string): Promise<boolean> => {
      const pending = readPending()
      if (!pending || pending.sessionId !== sid) return false
      try {
        const view = await getCurrentQuestion(sid)
        if (view.finished || !view.question) {
          // 其实已经答完了：仍然用原 request_id 回放，把那份评分拿回来
          await doSubmit(pending)
          return true
        }
        if (view.question.question_id !== pending.questionId) {
          // 题已经变了：这份待提交属于过去，重投只会 STALE，如实清掉
          writePending(null)
          setQuestion(view.question)
          setPhase('question')
          return false
        }
        setQuestion(view.question)
        setPhase('question')
        if (!pending.skip) setInput(pending.answer)
        await doSubmit(pending)
        return true
      } catch {
        return false
      }
    },
    [doSubmit],
  )

  // ---------------------------------------------------------------- 轮询（单飞 + 分级）

  useEffect(() => {
    if (phase !== 'preparing' || !booted) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let inFlight = false
    let attempts = 0

    const schedule = (ms: number) => {
      if (cancelled) return
      timer = setTimeout(tick, ms)
    }

    async function tick() {
      // 单飞：上一轮请求没回来就不发起下一轮
      if (cancelled || inFlight) return
      inFlight = true
      try {
        const view = await getProgress(sessionId)
        if (cancelled) return
        attempts = 0
        setPrepProgress(view.progress ?? null)
        setPrepStage(view.progress?.stage ?? 'PREPARING')

        if (view.status === 'PREPARING') {
          schedule(POLL_BASE_MS)
          return
        }
        if (view.status === 'PREPARATION_FAILED') {
          setPrepFailure({
            message: view.sanitized_error ?? '面试准备失败，请重试或重新上传材料',
            code: view.progress?.error_code ?? 'PREPARATION_FAILED',
          })
          setPhase('failed')
          return
        }
        if (view.finished) {
          onFinished(sessionId)
          return
        }
        // 准备完成：先看有没有"刷新前没提交成功"的回答
        if (await resumePending(sessionId)) return
        await loadQuestion(sessionId)
      } catch (e) {
        if (cancelled) return
        const err = toApiError(e)
        // 永久错误立刻停：继续轮询等于对着 404 刷日志
        if (
          err.code === 'SESSION_NOT_FOUND' ||
          err.code === 'SESSION_EXPIRED' ||
          err.status === 404 ||
          err.status === 410
        ) {
          writePending(null)
          onExit()
          return
        }
        if (err.code === 'SESSION_FINISHED') {
          onFinished(sessionId)
          return
        }
        // 网络/5xx：有限次退避重试
        attempts += 1
        if (attempts > POLL_MAX_ATTEMPTS) {
          setPrepFailure({
            message: '多次重试仍拿不到准备进度，请检查网络后重新加载页面',
            code: err.code,
          })
          setPhase('failed')
          return
        }
        schedule(Math.min(POLL_BASE_MS * 2 ** attempts, 15000))
      } finally {
        inFlight = false
      }
    }

    tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [phase, booted, sessionId, loadQuestion, resumePending, onExit, onFinished])

  // 准备耗时显示（每秒 tick，只在准备阶段）
  useEffect(() => {
    if (phase !== 'preparing') return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [phase])

  // ---------------------------------------------------------------- 首次进入：先问后端这场面试在哪一步

  useEffect(() => {
    if (bootstrappedRef.current) return
    bootstrappedRef.current = true
    ;(async () => {
      try {
        const view = await getProgress(sessionId)
        if (view.status === 'PREPARING') {
          const startedAt = readPrepStartedAt(sessionId) ?? Date.now()
          writePrepStartedAt(sessionId, startedAt)
          setPrepStartedAt(startedAt)
          setPhase('preparing')
          return
        }
        if (view.status === 'PREPARATION_FAILED') {
          setPrepFailure({
            message: view.sanitized_error ?? '面试准备失败，请重试或重新上传材料',
            code: view.progress?.error_code ?? 'PREPARATION_FAILED',
          })
          setPhase('failed')
          return
        }
        if (view.finished) {
          onFinished(sessionId)
          return
        }
        if (await resumePending(sessionId)) return
        await loadQuestion(sessionId)
      } catch (e) {
        const err = toApiError(e)
        if (err.code === 'SESSION_NOT_FOUND' || err.code === 'SESSION_EXPIRED') {
          writePending(null)
          onExit()
        } else {
          setError(err)
          setPhase('failed')
        }
      } finally {
        setBooted(true)
      }
    })()
  }, [sessionId, loadQuestion, resumePending, onExit, onFinished])

  // ---------------------------------------------------------------- 操作

  const sendAnswer = () => {
    const text = input.trim()
    if (!text || !question || submitting) return
    doSubmit({
      sessionId,
      questionId: question.question_id,
      requestId: newRequestId(),
      answer: text,
      skip: false,
    })
  }

  const skipQuestion = () => {
    if (!question || submitting) return
    doSubmit({
      sessionId,
      questionId: question.question_id,
      requestId: newRequestId(),
      answer: '',
      skip: true,
    })
  }

  const retrySubmit = () => {
    const pending = pendingRef.current ?? readPending()
    if (!pending) return
    doSubmit(pending) // 同一 request_id：后端幂等回放或续跑，绝不重复记录
  }

  /** 恢复会话并继续：先重建执行状态，再用原 request_id 重投那次回答。 */
  const recoverAndContinue = async () => {
    if (submitting) return
    setSubmitting(true)
    try {
      await recoverInterview(sessionId)
      setRecoveryNeeded(false)
      setError(null)
      const pending = pendingRef.current ?? readPending()
      if (pending) {
        await doSubmit(pending)
      } else {
        await loadQuestion(sessionId)
      }
    } catch (e) {
      setError(toApiError(e))
    } finally {
      setSubmitting(false)
    }
  }

  const retryPrep = async () => {
    if (retryingPrep) return
    setRetryingPrep(true)
    setError(null)
    try {
      await retryPreparation(sessionId)
      setPrepFailure(null)
      setPrepStage('QUEUED')
      const startedAt = readPrepStartedAt(sessionId) ?? Date.now()
      writePrepStartedAt(sessionId, startedAt)
      setPrepStartedAt(startedAt)
      setPhase('preparing')
    } catch (e) {
      const err = toApiError(e)
      if (err.code === 'MATERIAL_MISSING') {
        setError({
          ...err,
          message: '简历解析结果没有保存成功，重试出题不可用，请重新上传简历',
          retryable: false,
        })
      } else {
        setError(err)
      }
    } finally {
      setRetryingPrep(false)
    }
  }

  const endInterview = async () => {
    try {
      await finishInterview(sessionId)
      clearPrepStartedAt(sessionId)
      onFinished(sessionId)
    } catch (e) {
      setError(toApiError(e))
    }
  }

  // ---------------------------------------------------------------- 渲染

  const elapsed = Math.max(0, Math.floor((now - prepStartedAt) / 1000))
  const stageLabel = PREP_STAGE_LABEL[prepStage] ?? '准备中'

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold tracking-tight text-gray-900">
            {phase === 'preparing' && '正在准备这场面试'}
            {phase === 'failed' && '准备未完成'}
            {phase === 'question' && question
              ? `第 ${question.main_index} 题${question.kind === 'follow_up' ? ` · 追问 ${question.follow_up_index}` : ''}`
              : ''}
            {phase === 'question' && !question ? '面试进行中' : ''}
          </h1>
          <p className="mt-0.5 text-xs text-gray-500">
            {phase === 'preparing' && `${stageLabel}…`}
            {phase === 'failed' && '可以重试出题，或重新上传简历'}
            {phase === 'question' &&
              `共 ${question?.total_questions ?? '—'} 题 · 已答 ${question?.main_index ? question.main_index - 1 : 0} 题 · 随时可提前结束`}
          </p>
        </div>
        {phase === 'question' && (
          <button
            onClick={endInterview}
            className="shrink-0 rounded-xl border border-gray-200 bg-white px-3.5 py-1.5 text-xs text-gray-600 hover:border-gray-300 hover:bg-gray-50"
          >
            提前结束
          </button>
        )}
      </header>

      {error && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-xs leading-relaxed text-red-700">
          <span>
            {error.message}
            <span className="ml-1 text-red-400">({error.code})</span>
          </span>
          <div className="flex shrink-0 gap-2">
            {error.retryable && pendingRef.current && (
              <button
                onClick={retrySubmit}
                className="rounded-lg bg-red-600 px-3 py-1 font-medium text-white hover:opacity-90"
              >
                重试
              </button>
            )}
            {recoveryNeeded && (
              <button
                onClick={recoverAndContinue}
                disabled={submitting}
                className="rounded-lg bg-red-600 px-3 py-1 font-medium text-white hover:opacity-90 disabled:opacity-50"
              >
                恢复并继续
              </button>
            )}
          </div>
        </div>
      )}

      {/* 准备中 */}
      {phase === 'preparing' && (
        <div className="rounded-2xl border border-gray-200 bg-white px-8 py-12 text-center">
          <div className="mx-auto flex h-10 items-center justify-center">
            <span className="h-2.5 w-2.5 animate-bounce rounded-full bg-blue-500" />
            <span className="ml-1.5 h-2.5 w-2.5 animate-bounce rounded-full bg-blue-400 [animation-delay:150ms]" />
            <span className="ml-1.5 h-2.5 w-2.5 animate-bounce rounded-full bg-blue-300 [animation-delay:300ms]" />
          </div>
          <p className="mt-5 text-sm font-medium text-gray-800">{stageLabel}</p>
          <p className="mt-1 text-xs text-gray-500">
            已用 {elapsed} 秒 · 通常需要几十秒，本页可以关闭，稍后从「面试记录」回来会接上
          </p>
          <div className="mx-auto mt-5 flex max-w-md flex-wrap justify-center gap-1.5 text-[11px] text-gray-400">
            {Object.entries(PREP_STAGE_LABEL)
              .filter(([key]) => !['WAITING_ANSWER', 'PREPARATION_FAILED'].includes(key))
              .map(([key, label]) => (
                <span
                  key={key}
                  className={`rounded-full px-2 py-0.5 ${
                    key === prepStage ? 'bg-blue-50 text-blue-700' : 'bg-gray-50'
                  }`}
                >
                  {label}
                </span>
              ))}
          </div>
          {!!prepProgress?.char_count && (
            <p className="mt-3 text-[11px] text-gray-400">
              已提取 {prepProgress.char_count} 字 · 图片 {prepProgress.render_blocks ?? 0} 张
            </p>
          )}
        </div>
      )}

      {/* 准备失败 */}
      {phase === 'failed' && (
        <div className="rounded-2xl border border-red-200 bg-white px-6 py-8">
          <h2 className="text-sm font-semibold text-gray-900">面试准备没有完成</h2>
          <p className="mt-2 text-sm leading-relaxed text-gray-700">
            {prepFailure?.message ?? error?.message ?? '准备失败，请重试或重新上传材料'}
          </p>
          {prepFailure?.code && (
            <p className="mt-1 text-[11px] text-gray-400">错误码：{prepFailure.code}</p>
          )}
          <div className="mt-5 flex flex-wrap gap-2">
            <button
              onClick={retryPrep}
              disabled={retryingPrep}
              className="rounded-xl bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {retryingPrep ? '正在重试…' : '重试出题（不用重传简历）'}
            </button>
            <button
              onClick={onExit}
              className="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm text-gray-600 hover:bg-gray-50"
            >
              重新上传简历
            </button>
          </div>
          <p className="mt-3 text-[11px] text-gray-400">
            若失败发生在解析阶段，只能重新上传；出题阶段失败无需重传。
          </p>
        </div>
      )}

      {/* 答题 */}
      {phase === 'question' && question && (
        <>
          {recovered && (
            <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-2 text-xs text-blue-700">
              检测到执行状态缺失，已从数据库恢复到当前题目
            </div>
          )}

          {/* 进度条 */}
          {!!question.total_questions && (
            <div className="flex items-center gap-3">
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-200">
                <div
                  className="h-full rounded-full bg-blue-500 transition-all"
                  style={{
                    width: `${Math.min(100, ((question.main_index - 1) / question.total_questions) * 100)}%`,
                  }}
                />
              </div>
              <span className="shrink-0 text-[11px] tabular-nums text-gray-400">
                {question.main_index - 1} / {question.total_questions}
              </span>
            </div>
          )}

          {feedback && <FeedbackCardView card={feedback} />}

          <div className="rounded-2xl border border-gray-200 bg-white px-6 py-5 shadow-sm">
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
              {question.category && (
                <span className="rounded-full bg-gray-100 px-2 py-0.5">{question.category}</span>
              )}
              <span className="rounded-full bg-gray-100 px-2 py-0.5">
                {question.difficulty === 'easy'
                  ? '基础'
                  : question.difficulty === 'medium'
                    ? '进阶'
                    : '挑战'}
              </span>
              {question.kind === 'follow_up' && (
                <span className="rounded-full bg-amber-50 px-2 py-0.5 font-medium text-amber-700">
                  追问
                </span>
              )}
            </div>
            <p className="mt-3 whitespace-pre-wrap text-[15px] leading-relaxed text-gray-900">
              {question.content}
            </p>
          </div>

          <div className="rounded-2xl border border-gray-200 bg-white px-6 py-5">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault()
                  sendAnswer()
                }
              }}
              disabled={submitting}
              rows={7}
              maxLength={10000}
              placeholder={
                submitting
                  ? 'AI 正在评分…'
                  : '像面试一样口述你的回答：背景、你的做法、为什么这样选、结果如何（Ctrl+Enter 发送）'
              }
              className="w-full resize-none rounded-xl border border-gray-200 px-4 py-3 text-sm leading-relaxed outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-100 disabled:bg-gray-50"
            />
            <div className="mt-3 flex items-center justify-between">
              <button
                onClick={skipQuestion}
                disabled={submitting}
                className="rounded-xl border border-gray-200 bg-white px-4 py-2 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40"
              >
                跳过本题
              </button>
              <div className="flex items-center gap-3">
                <span className="text-[11px] text-gray-400">{input.length} / 10000</span>
                <button
                  onClick={sendAnswer}
                  disabled={submitting || !input.trim()}
                  className="flex items-center gap-2 rounded-xl bg-blue-600 px-6 py-2 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-200 disabled:text-gray-400"
                >
                  {submitting && (
                    <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
                  )}
                  {submitting ? '评分中…' : '提交回答'}
                </button>
              </div>
            </div>
            <p className="mt-2 text-right text-[11px] text-gray-400">
              每题只能提交一次，提交前请想好
            </p>
          </div>
        </>
      )}
    </div>
  )
}

function toApiError(e: unknown): ApiError {
  if (e instanceof RequestFailed) return e
  return { code: 'UNKNOWN', message: e instanceof Error ? e.message : '未知错误', retryable: false }
}
