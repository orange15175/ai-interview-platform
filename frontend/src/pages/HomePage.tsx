/**
 * 首页：登录后的落地页。
 *
 * 这里**不做任何跳转**——之前登录直接进上传/答题界面，用户既不知道自己
 * 有没有正在进行的面试，也没有入口去看历史。首页只负责回答三个问题：
 * 我上次面得怎么样、有没有没面完的、现在要不要开始新的。
 */

import { useEffect, useState } from 'react'

import { listSessions, type AuthUser, type SessionListItem } from '../api'
import { Empty, Spinner, StatusPill } from '../components/ui'
import { relativeTime, scoreClass } from '../shared'

export default function HomePage({
  user,
  onStart,
  onResume,
  onOpenReport,
  onShowHistory,
}: {
  user: AuthUser
  onStart: () => void
  onResume: (sessionId: string) => void
  onOpenReport: (sessionId: string) => void
  onShowHistory: () => void
}) {
  const [items, setItems] = useState<SessionListItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    listSessions(20)
      .then((list) => alive && setItems(list))
      .catch((e) => alive && setError(e instanceof Error ? e.message : '记录加载失败'))
    return () => {
      alive = false
    }
  }, [])

  const finished = items?.filter((i) => i.overall_score !== null) ?? []
  const avg =
    finished.length > 0
      ? Math.round(finished.reduce((s, i) => s + (i.overall_score ?? 0), 0) / finished.length)
      : null
  const answered = items?.reduce((s, i) => s + i.answered_count, 0) ?? 0
  const ongoing = items?.find((i) => i.resumable) ?? null
  const recent = (items ?? []).slice(0, 3)

  return (
    <div className="space-y-6">
      <header>
        <p className="text-xs text-gray-400">{greeting()}，欢迎回来</p>
        <h1 className="mt-1 text-xl font-semibold tracking-tight text-gray-900">
          {user.account}
        </h1>
        <p className="mt-1 text-sm text-gray-500">
          用简历生成专属面试题，逐题评分并给出可执行的复盘建议。
        </p>
      </header>

      {ongoing && (
        <div className="flex flex-col gap-3 rounded-2xl border border-blue-200 bg-gradient-to-r from-blue-50 to-indigo-50 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="flex items-center gap-2 text-sm font-medium text-blue-900">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-500 opacity-60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-600" />
              </span>
              有一场面试没结束
            </p>
            <p className="mt-1 truncate text-xs text-blue-700/80">
              已答 {ongoing.answered_count} / {ongoing.question_count} 题 ·{' '}
              {relativeTime(ongoing.last_activity_at)}更新
            </p>
          </div>
          <button
            onClick={() => onResume(ongoing.session_id)}
            className="shrink-0 rounded-xl bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700"
          >
            继续这场面试
          </button>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard label="面试场次" value={items ? String(items.length) : '—'} hint="全部记录" />
        <StatCard label="累计作答" value={items ? String(answered) : '—'} hint="道题" />
        <StatCard
          label="平均分"
          value={avg !== null ? String(avg) : '—'}
          hint={finished.length > 0 ? `来自 ${finished.length} 场有评分的面试` : '还没有有效评分'}
          accent={avg !== null ? scoreClass(avg) : undefined}
        />
      </div>

      <section>
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-gray-900">最近面试</h2>
          {(items?.length ?? 0) > 0 && (
            <button onClick={onShowHistory} className="text-xs text-blue-600 hover:underline">
              查看全部
            </button>
          )}
        </div>

        {error && (
          <div className="rounded-2xl border border-red-200 bg-red-50 px-5 py-4 text-xs text-red-700">
            {error}
          </div>
        )}

        {!error && items === null && (
          <div className="flex items-center gap-2 rounded-2xl border border-gray-200 bg-white px-5 py-6 text-sm text-gray-500">
            <Spinner /> 正在读取面试记录…
          </div>
        )}

        {!error && items !== null && recent.length === 0 && (
          <Empty
            title="还没有面试记录"
            hint="上传一份简历，系统会据此生成一整套面试题，答完可以看评分与复盘建议。"
            action={
              <button
                onClick={onStart}
                className="rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700"
              >
                开始第一场面试
              </button>
            }
          />
        )}

        {recent.length > 0 && (
          <ul className="space-y-2.5">
            {recent.map((item) => (
              <li key={item.session_id}>
                <SessionRow item={item} onResume={onResume} onOpenReport={onOpenReport} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="rounded-2xl border border-gray-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-gray-900">一场面试怎么走</h2>
        <ol className="mt-3 grid gap-3 sm:grid-cols-3">
          {[
            { n: '1', t: '上传简历', d: '支持 Word / PDF / 图片，可附岗位 JD' },
            { n: '2', t: '逐题作答', d: '生成 5~20 道题，每题提交后立刻评分，可能追问' },
            { n: '3', t: '看复盘报告', d: '总分、五维能力、薄弱点与改进建议' },
          ].map((s) => (
            <li key={s.n} className="rounded-xl bg-gray-50 px-4 py-3">
              <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-white text-[11px] font-semibold text-blue-600 ring-1 ring-blue-100">
                {s.n}
              </span>
              <p className="mt-1.5 text-xs font-medium text-gray-800">{s.t}</p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-gray-500">{s.d}</p>
            </li>
          ))}
        </ol>
      </section>
    </div>
  )
}

function StatCard({
  label,
  value,
  hint,
  accent,
}: {
  label: string
  value: string
  hint: string
  accent?: string
}) {
  return (
    <div className="rounded-2xl border border-gray-200 bg-white px-4 py-3.5">
      <p className="text-[11px] text-gray-500">{label}</p>
      <p className="mt-1 flex items-baseline gap-1.5">
        <span
          className={`text-2xl font-semibold tabular-nums ${
            accent ? `rounded-lg border px-2 py-0.5 text-lg ${accent}` : 'text-gray-900'
          }`}
        >
          {value}
        </span>
      </p>
      <p className="mt-0.5 truncate text-[11px] text-gray-400">{hint}</p>
    </div>
  )
}

/** 列表行：进行中的给"继续"，已结束的给"看报告"，两种动作不混。 */
export function SessionRow({
  item,
  onResume,
  onOpenReport,
}: {
  item: SessionListItem
  onResume: (sessionId: string) => void
  onOpenReport: (sessionId: string) => void
}) {
  return (
    <div className="group flex items-center gap-4 rounded-2xl border border-gray-200 bg-white px-4 py-3.5 transition hover:border-blue-200 hover:shadow-sm">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill status={item.status} />
          <span className="text-xs text-gray-400">
            {item.question_count} 题 · 已答 {item.answered_count}
          </span>
          <span className="text-xs text-gray-300">·</span>
          <span className="text-xs text-gray-400">{relativeTime(item.last_activity_at)}</span>
        </div>
        <p className="mt-1 truncate text-[13px] text-gray-700">
          会话 {item.session_id.slice(0, 8)}
          <span className="ml-2 text-gray-400">
            {item.finished ? '已结束，可查看复盘报告' : '未结束，可继续作答'}
          </span>
        </p>
      </div>

      {item.overall_score !== null ? (
        <span
          className={`shrink-0 rounded-xl border px-3 py-1.5 text-sm font-semibold tabular-nums ${scoreClass(item.overall_score)}`}
        >
          {item.overall_score}
        </span>
      ) : (
        <span className="shrink-0 rounded-xl border border-gray-200 bg-gray-50 px-3 py-1.5 text-xs text-gray-400">
          无评分
        </span>
      )}

      {item.resumable ? (
        <button
          onClick={() => onResume(item.session_id)}
          className="shrink-0 rounded-xl bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white hover:bg-blue-700"
        >
          继续
        </button>
      ) : (
        <button
          onClick={() => onOpenReport(item.session_id)}
          className="shrink-0 rounded-xl border border-gray-200 px-3.5 py-1.5 text-xs text-gray-600 hover:border-gray-300 hover:bg-gray-50"
        >
          查看报告
        </button>
      )}
    </div>
  )
}

function greeting(): string {
  const h = new Date().getHours()
  if (h < 6) return '凌晨好'
  if (h < 12) return '上午好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
}
