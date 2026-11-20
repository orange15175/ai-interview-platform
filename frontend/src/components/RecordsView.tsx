/**
 * 逐条问答记录：报告给结论，这里给证据。
 *
 * 与报告一致的边界：不含评分要点（rubric），只展示题干、我的回答、分数、反馈。
 */

import { useState } from 'react'

import type { AnswerRecord, ApiError, RecordsView as RecordsData } from '../api'
import { scoreClass } from '../shared'

export default function RecordsView({
  records,
  sessionId,
  error,
  loading,
  onRetry,
}: {
  records: RecordsData | null
  sessionId: string | null
  error: ApiError | null
  loading: boolean
  onRetry: () => void
}) {
  // 有错误先说错误：即使之前加载成功过，也要让用户知道这次刷新失败了
  if (error) {
    return (
      <div className="rounded-2xl border border-red-200 bg-white px-6 py-8 text-center">
        <p className="text-sm text-gray-700">记录加载失败：{error.message}</p>
        <p className="mt-1 text-[11px] text-gray-400">（{error.code}）</p>
        <button
          onClick={onRetry}
          disabled={loading}
          className="mt-4 rounded-xl bg-blue-600 px-4 py-2 text-sm text-white disabled:opacity-50"
        >
          {loading ? '正在重新加载…' : '重新加载记录'}
        </button>
      </div>
    )
  }
  if (!records) {
    return (
      <div className="rounded-2xl border border-gray-200 bg-white px-6 py-8 text-center text-sm text-gray-500">
        正在加载正式记录…
      </div>
    )
  }
  if (records.record_count === 0) {
    return (
      <div className="rounded-2xl border border-gray-200 bg-white px-6 py-8 text-center text-sm text-gray-500">
        本次面试没有已完成的问答记录。
      </div>
    )
  }

  const scored = records.records.filter((r) => !r.skipped && r.score !== null)
  const avg =
    scored.length > 0
      ? Math.round(scored.reduce((s, r) => s + (r.score ?? 0), 0) / scored.length)
      : null

  return (
    <div className="space-y-2.5">
      <div className="flex items-center justify-between rounded-2xl border border-gray-200 bg-white px-5 py-3.5 text-sm">
        <span className="text-xs text-gray-600">
          共 {records.record_count} 条记录 · 已作答 {scored.length} 条 · 跳过{' '}
          {records.record_count - scored.length} 条
        </span>
        {avg !== null && (
          <span className={`rounded-full border px-3 py-1 text-xs font-semibold tabular-nums ${scoreClass(avg)}`}>
            平均 {avg} 分
          </span>
        )}
      </div>
      {records.records.map((r) => (
        <RecordRow key={r.answer_id} record={r} />
      ))}
      <p className="text-center text-[11px] text-gray-400">
        会话 ID：{sessionId?.slice(0, 8)}… · 逐条记录不含评分要点
      </p>
    </div>
  )
}

function RecordRow({ record }: { record: AnswerRecord }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-2xl border border-gray-200 bg-white px-5 py-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-gray-400">
            <span>
              第 {record.main_index} 题
              {record.kind === 'follow_up' && ` · 追问 ${record.follow_up_index}`}
            </span>
            {record.category && (
              <span className="rounded-full bg-gray-100 px-2 py-0.5">{record.category}</span>
            )}
            {record.skipped && (
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-gray-500">已跳过</span>
            )}
          </div>
          <p className="mt-1.5 line-clamp-2 text-sm text-gray-800">{record.content}</p>
        </div>
        {!record.skipped && record.score !== null && (
          <span
            className={`shrink-0 rounded-xl border px-2.5 py-1 text-sm font-semibold tabular-nums ${scoreClass(record.score)}`}
          >
            {record.score} 分
          </span>
        )}
      </div>
      <button
        onClick={() => setOpen(!open)}
        className="mt-2 text-xs text-blue-600 hover:underline"
      >
        {open ? '收起详情' : '展开我的回答与反馈'}
      </button>
      {open && (
        <div className="mt-2 space-y-2 border-t border-gray-100 pt-2 text-sm">
          <div>
            <span className="text-[11px] text-gray-400">我的回答：</span>
            <p className="whitespace-pre-wrap leading-relaxed text-gray-700">
              {record.skipped ? '（跳过）' : record.answer_content || '（空）'}
            </p>
          </div>
          {record.evaluation?.feedback && (
            <div>
              <span className="text-[11px] text-gray-400">AI 反馈：</span>
              <p className="whitespace-pre-wrap leading-relaxed text-gray-700">
                {record.evaluation.feedback}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
