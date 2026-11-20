/**
 * 报告页：报告 + 逐条记录，两个接口并行加载、互不连累。
 *
 * 为什么和答题页分开：报告是"事后复盘"，答题时不需要它；
 * 分开后可以从首页/记录页直接打开历史报告，不必重新走一遍面试。
 */

import { useCallback, useEffect, useState } from 'react'

import {
  RequestFailed,
  getRecords,
  getReport,
  type ApiError,
  type InterviewReport,
  type RecordsView as RecordsData,
} from '../api'
import RecordsView from '../components/RecordsView'
import ReportView from '../components/ReportView'
import { Spinner, StatusPill } from '../components/ui'

export default function ReportPage({
  sessionId,
  onBack,
  onRestart,
}: {
  sessionId: string
  onBack: () => void
  onRestart: () => void
}) {
  const [report, setReport] = useState<InterviewReport | null>(null)
  const [reportError, setReportError] = useState<ApiError | null>(null)
  const [records, setRecords] = useState<RecordsData | null>(null)
  const [recordsError, setRecordsError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setReportError(null)
    setRecordsError(null)
    // 两个接口互不依赖：用 allSettled，一个失败不能连累另一个
    const [rep, rec] = await Promise.allSettled([getReport(sessionId), getRecords(sessionId)])
    if (rep.status === 'fulfilled') setReport(rep.value)
    else setReportError(toApiError(rep.reason))
    if (rec.status === 'fulfilled') setRecords(rec.value)
    else setRecordsError(toApiError(rec.reason))
    setLoading(false)
  }, [sessionId])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <button
            onClick={onBack}
            className="mb-1 flex items-center gap-1 text-xs text-gray-500 hover:text-gray-800"
          >
            <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M11 5 6 10l5 5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            返回
          </button>
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-semibold tracking-tight text-gray-900">面试报告</h1>
            {report && <StatusPill status={report.status} />}
            {loading && <Spinner className="h-3.5 w-3.5" />}
          </div>
          <p className="mt-0.5 text-xs text-gray-400">
            会话 {sessionId.slice(0, 8)} ·{' '}
            {report ? formatTime(report.generated_at) : '正在读取'}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={load}
            disabled={loading}
            className="rounded-xl border border-gray-200 bg-white px-3.5 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button
            onClick={onRestart}
            className="rounded-xl bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white hover:bg-blue-700"
          >
            再面一场
          </button>
        </div>
      </header>

      <ReportView report={report} error={reportError} />

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-900">逐条记录</h2>
        <RecordsView
          records={records}
          sessionId={sessionId}
          error={recordsError}
          loading={loading}
          onRetry={load}
        />
      </section>
    </div>
  )
}

function toApiError(e: unknown): ApiError {
  if (e instanceof RequestFailed) return e
  return { code: 'UNKNOWN', message: e instanceof Error ? e.message : '未知错误', retryable: false }
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (!Number.isFinite(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
