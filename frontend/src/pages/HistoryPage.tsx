/**
 * 面试记录：全部历史，可按状态筛选。
 *
 * 数据来自后端 GET /api/interviews（按当前身份），不读 localStorage——
 * 换设备登录同一账号也要能看到自己的记录。
 */

import { useEffect, useMemo, useState } from 'react'

import { listSessions, type SessionListItem } from '../api'
import { Empty, Spinner } from '../components/ui'
import { SessionRow } from './HomePage'

type Filter = 'all' | 'ongoing' | 'finished'

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'ongoing', label: '未完成' },
  { key: 'finished', label: '已结束' },
]

export default function HistoryPage({
  onResume,
  onOpenReport,
  onStart,
}: {
  onResume: (sessionId: string) => void
  onOpenReport: (sessionId: string) => void
  onStart: () => void
}) {
  const [items, setItems] = useState<SessionListItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')

  useEffect(() => {
    let alive = true
    listSessions(50)
      .then((list) => alive && setItems(list))
      .catch((e) => alive && setError(e instanceof Error ? e.message : '记录加载失败'))
    return () => {
      alive = false
    }
  }, [])

  const shown = useMemo(() => {
    if (!items) return []
    if (filter === 'ongoing') return items.filter((i) => !i.finished)
    if (filter === 'finished') return items.filter((i) => i.finished)
    return items
  }, [items, filter])

  const counts = useMemo(
    () => ({
      all: items?.length ?? 0,
      ongoing: items?.filter((i) => !i.finished).length ?? 0,
      finished: items?.filter((i) => i.finished).length ?? 0,
    }),
    [items],
  )

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold tracking-tight text-gray-900">面试记录</h1>
        <p className="mt-1 text-sm text-gray-500">
          每场面试都可以重新打开：没答完的继续答，答完的看复盘报告。
        </p>
      </header>

      <div className="flex items-center gap-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded-xl px-3 py-1.5 text-xs transition ${
              filter === f.key
                ? 'bg-gray-900 font-medium text-white'
                : 'bg-white text-gray-600 ring-1 ring-gray-200 hover:bg-gray-50'
            }`}
          >
            {f.label}
            <span className={filter === f.key ? 'ml-1 text-white/60' : 'ml-1 text-gray-400'}>
              {counts[f.key]}
            </span>
          </button>
        ))}
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

      {!error && items !== null && shown.length === 0 && (
        <Empty
          title={filter === 'all' ? '还没有面试记录' : '这个筛选下没有记录'}
          hint={
            filter === 'all'
              ? '上传一份简历，系统会据此生成一整套面试题。'
              : '换一个筛选条件，或开始一场新的面试。'
          }
          action={
            <button
              onClick={onStart}
              className="rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700"
            >
              开始新面试
            </button>
          }
        />
      )}

      {shown.length > 0 && (
        <ul className="space-y-2.5">
          {shown.map((item) => (
            <li key={item.session_id}>
              <SessionRow item={item} onResume={onResume} onOpenReport={onOpenReport} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
