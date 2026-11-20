/**
 * 复盘报告展示：全部来自后端对**已落库评分**的聚合（不调模型）。
 *
 * 两条展示规则必须守：
 * - overall_score 为 null 时显示"暂无评分"，绝不用 0 分顶上；
 * - 五维只显示 sample_count > 0 的维度，没数据的维度不画成 0 分柱。
 */

import { useState } from 'react'

import type { AnswerEvidence, ApiError, InterviewReport } from '../api'
import { scoreClass } from '../shared'

const DIMENSION_LABELS: Record<string, string> = {
  correctness: '正确性',
  completeness: '完整性',
  logic: '逻辑性',
  depth: '深度',
  clarity: '表达清晰度',
}

const DIMENSION_ORDER = ['correctness', 'completeness', 'logic', 'depth', 'clarity']

export default function ReportView({
  report,
  error,
}: {
  report: InterviewReport | null
  error: ApiError | null
}) {
  if (error) {
    return (
      <div className="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-4 text-xs leading-relaxed text-amber-800">
        复盘报告生成失败：{error.message}
        <span className="ml-1 text-amber-500">（{error.code}）</span>
        <span className="ml-1">逐条记录仍可在下方查看。</span>
      </div>
    )
  }
  if (!report) {
    return (
      <div className="rounded-2xl border border-gray-200 bg-white px-6 py-8 text-center text-sm text-gray-500">
        正在生成复盘报告…
      </div>
    )
  }

  const { summary } = report
  const dims = DIMENSION_ORDER.map((key) => ({
    key,
    label: DIMENSION_LABELS[key] ?? key,
    ...report.dimension_scores[key],
  })).filter((d) => d && d.sample_count > 0 && d.score !== null)

  return (
    <div className="space-y-4">
      {/* 总分 */}
      <div className="rounded-2xl border border-gray-200 bg-white p-6">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-gray-900">复盘报告</h2>
          <span className="text-[11px] text-gray-400">
            {report.is_final ? '最终版' : '截至当前'} · 基于已落库评分聚合
          </span>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-5">
          {summary.overall_score !== null ? (
            <div className="flex items-baseline gap-1">
              <span
                className={`rounded-2xl border px-4 py-2 text-3xl font-semibold tabular-nums ${scoreClass(summary.overall_score)}`}
              >
                {summary.overall_score}
              </span>
              <span className="text-xs text-gray-400">分</span>
            </div>
          ) : (
            <span className="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-2 text-sm text-gray-500">
              暂无评分
            </span>
          )}
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-gray-500 sm:grid-cols-4">
            <span>
              题目 <b className="font-medium text-gray-800">{summary.question_count}</b>
            </span>
            <span>
              作答 <b className="font-medium text-gray-800">{summary.completed_count}</b>
            </span>
            <span>
              跳过 <b className="font-medium text-gray-800">{summary.skipped_count}</b>
            </span>
            <span>
              覆盖率{' '}
              <b className="font-medium text-gray-800">
                {Math.round(summary.score_coverage * 100)}%
              </b>
            </span>
          </div>
        </div>

        {report.notice && (
          <p className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-700">
            {report.notice}
          </p>
        )}

        {dims.length > 0 && (
          <div className="mt-5">
            <p className="text-[11px] font-medium text-gray-500">五维能力</p>
            <div className="mt-2 space-y-2">
              {dims.map((d) => (
                <div key={d.key} className="flex items-center gap-3">
                  <span className="w-20 shrink-0 text-xs text-gray-500">{d.label}</span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
                    <div
                      className={`h-full rounded-full ${
                        (d.score ?? 0) >= 80
                          ? 'bg-emerald-500'
                          : (d.score ?? 0) >= 60
                            ? 'bg-blue-500'
                            : 'bg-amber-500'
                      }`}
                      style={{ width: `${Math.max(0, Math.min(100, d.score ?? 0))}%` }}
                    />
                  </div>
                  <span className="w-16 shrink-0 text-right text-xs text-gray-600">
                    {d.score} 分
                    <span className="ml-1 text-[10px] text-gray-400">/{d.sample_count}</span>
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* 薄弱点 + 建议 */}
      {(report.weak_points.length > 0 || report.suggestions.length > 0) && (
        <div className="grid gap-4 sm:grid-cols-2">
          {report.weak_points.length > 0 && (
            <div className="rounded-2xl border border-gray-200 bg-white p-5">
              <p className="text-[11px] font-medium text-gray-500">反复出现的薄弱点</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {report.weak_points.map((w) => (
                  <span
                    key={w.point}
                    className="max-w-full truncate rounded-full bg-amber-50 px-2.5 py-1 text-xs text-amber-800"
                    title={w.point}
                  >
                    {w.point}
                    <span className="ml-1 text-amber-500">×{w.count}</span>
                  </span>
                ))}
              </div>
            </div>
          )}
          {report.suggestions.length > 0 && (
            <div className="rounded-2xl border border-gray-200 bg-white p-5">
              <p className="text-[11px] font-medium text-gray-500">改进建议</p>
              <ul className="mt-2 space-y-1.5 text-xs leading-relaxed text-gray-700">
                {report.suggestions.map((s) => (
                  <li key={s} className="flex gap-2">
                    <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-blue-400" />
                    <span>{s}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* 类别 + 代表回答 */}
      {(report.category_summary.length > 0 || report.evidence.length > 0) && (
        <div className="rounded-2xl border border-gray-200 bg-white p-5">
          {report.category_summary.length > 0 && (
            <>
              <p className="text-[11px] font-medium text-gray-500">按题目类别</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {report.category_summary.map((c) => (
                  <span
                    key={c.category}
                    className="rounded-xl bg-gray-50 px-2.5 py-1 text-xs text-gray-600"
                  >
                    {c.category} · {c.completed_count}/{c.question_count}
                    {c.average_score !== null && ` · 均分 ${c.average_score}`}
                  </span>
                ))}
              </div>
            </>
          )}

          {report.evidence.length > 0 && (
            <div className={report.category_summary.length > 0 ? 'mt-5' : ''}>
              <p className="text-[11px] font-medium text-gray-500">
                代表回答（最高分与最低分）
              </p>
              <div className="mt-2 space-y-2">
                {report.evidence.map((e) => (
                  <EvidenceRow key={`${e.question_id}-${e.follow_up_index}`} evidence={e} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function EvidenceRow({ evidence }: { evidence: AnswerEvidence }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-xl border border-gray-100 bg-gray-50/60 px-3 py-2">
      <div className="flex items-start justify-between gap-3">
        <p className="min-w-0 text-xs text-gray-600">
          第 {evidence.main_index} 题
          {evidence.kind === 'follow_up' && ` · 追问 ${evidence.follow_up_index}`}
          <span className="ml-2 text-gray-500">{evidence.content}</span>
        </p>
        <span
          className={`shrink-0 rounded-full border px-2 py-0.5 text-xs font-semibold tabular-nums ${scoreClass(evidence.score)}`}
        >
          {evidence.score}
        </span>
      </div>
      <button
        onClick={() => setOpen(!open)}
        className="mt-1 text-[11px] text-blue-600 hover:underline"
      >
        {open ? '收起' : '看回答摘录与反馈'}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1 text-xs leading-relaxed text-gray-600">
          <p className="whitespace-pre-wrap">{evidence.answer_excerpt || '（空）'}</p>
          {evidence.feedback && <p className="text-gray-500">AI 反馈：{evidence.feedback}</p>}
        </div>
      )}
    </div>
  )
}
