/**
 * 通用小组件。刻意保持很小：只收三个以上页面真正复用的东西，
 * 不为"以后可能用到"提前抽象。
 */

import type { ReactNode } from 'react'

import { STATUS_LABEL, statusClass } from '../shared'

export function Spinner({ className = '' }: { className?: string }) {
  return (
    <span
      className={`inline-block h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-gray-600 ${className}`}
      aria-label="加载中"
    />
  )
}

/** 会话状态胶囊。文案集中在 shared.ts，避免每个页面各写一份。 */
export function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${statusClass(status)}`}
    >
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

export function Empty({
  title,
  hint,
  action,
}: {
  title: string
  hint?: string
  action?: ReactNode
}) {
  return (
    <div className="rounded-2xl border border-dashed border-gray-200 bg-white/60 px-8 py-12 text-center">
      <p className="text-sm font-medium text-gray-700">{title}</p>
      {hint && <p className="mx-auto mt-1 max-w-sm text-xs leading-relaxed text-gray-500">{hint}</p>}
      {action && <div className="mt-5 flex justify-center">{action}</div>}
    </div>
  )
}

export function SectionTitle({
  title,
  extra,
}: {
  title: string
  extra?: ReactNode
}) {
  return (
    <div className="mb-3 flex items-baseline justify-between">
      <h2 className="text-sm font-semibold text-gray-900">{title}</h2>
      {extra}
    </div>
  )
}
