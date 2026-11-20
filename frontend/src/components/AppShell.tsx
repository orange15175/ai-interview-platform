/**
 * 应用外壳：左侧固定导航 + 右侧内容区。
 *
 * 存在意义：把"我登录后在哪、还能去哪"表达出来。之前所有页共用一个顶栏，
 * 一进来就是上传/答题，用户没有"回到首页"或"看历史"的入口。
 *
 * 移动端（<lg）收成顶部横条 + 横向导航，不做汉堡抽屉——导航只有三项，
 * 抽屉反而多一次点击。
 */

import type { ReactNode } from 'react'

import type { AuthUser } from '../api'

export type ViewName = 'home' | 'setup' | 'session' | 'history' | 'report'

interface NavItem {
  key: ViewName
  label: string
  icon: ReactNode
}

const NAV: NavItem[] = [
  { key: 'home', label: '首页', icon: <IconHome /> },
  { key: 'history', label: '面试记录', icon: <IconList /> },
]

export default function AppShell({
  user,
  current,
  onNavigate,
  onStart,
  onLogout,
  children,
}: {
  user: AuthUser
  current: ViewName
  onNavigate: (view: ViewName) => void
  onStart: () => void
  onLogout: () => void
  children: ReactNode
}) {
  const inSession = current === 'session'

  return (
    <div className="min-h-screen bg-gray-50 text-gray-900">
      {/* 侧边栏（lg 以上） */}
      <aside className="fixed inset-y-0 left-0 z-20 hidden w-60 flex-col border-r border-gray-200 bg-white lg:flex">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-blue-600 to-indigo-600 text-sm font-semibold text-white shadow-sm">
            面
          </div>
          <div className="leading-tight">
            <p className="text-sm font-semibold">AI 模拟面试</p>
            <p className="text-[11px] text-gray-400">简历驱动的技术面试练习</p>
          </div>
        </div>

        <nav className="mt-2 flex flex-col gap-1 px-3">
          {NAV.map((item) => {
            const active = current === item.key || (item.key === 'home' && current === 'report')
            return (
              <button
                key={item.key}
                onClick={() => onNavigate(item.key)}
                className={`flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm transition-colors ${
                  active
                    ? 'bg-blue-50 font-medium text-blue-700'
                    : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'
                }`}
              >
                <span className={active ? 'text-blue-600' : 'text-gray-400'}>{item.icon}</span>
                {item.label}
              </button>
            )
          })}
        </nav>

        <div className="px-3 pt-4">
          <button
            onClick={onStart}
            disabled={inSession}
            className="flex w-full items-center justify-center gap-1.5 rounded-xl bg-blue-600 px-3 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-200 disabled:text-gray-400 disabled:shadow-none"
            title={inSession ? '面试进行中，先结束或离开当前面试' : '上传简历开始一场新面试'}
          >
            <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M10 4v12M4 10h12" strokeLinecap="round" />
            </svg>
            开始新面试
          </button>
        </div>

        <div className="mt-auto border-t border-gray-100 p-3">
          <div className="flex items-center gap-2.5 rounded-xl px-2 py-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-gray-100 text-xs font-semibold uppercase text-gray-600">
              {user.account.slice(0, 1)}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium text-gray-800">{user.account}</p>
              <p className="text-[11px] text-gray-400">已登录</p>
            </div>
            <button
              onClick={onLogout}
              className="rounded-lg px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-100 hover:text-gray-800"
            >
              退出
            </button>
          </div>
        </div>
      </aside>

      {/* 移动端顶栏 */}
      <div className="sticky top-0 z-20 border-b border-gray-200 bg-white/90 backdrop-blur lg:hidden">
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-600 text-xs font-semibold text-white">
              面
            </div>
            <span className="text-sm font-semibold">AI 模拟面试</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="max-w-[7rem] truncate text-xs text-gray-500">{user.account}</span>
            <button
              onClick={onLogout}
              className="rounded-lg border border-gray-200 px-2 py-1 text-[11px] text-gray-500"
            >
              退出
            </button>
          </div>
        </div>
        <div className="flex gap-1 px-3 pb-2">
          {NAV.map((item) => {
            const active = current === item.key || (item.key === 'home' && current === 'report')
            return (
              <button
                key={item.key}
                onClick={() => onNavigate(item.key)}
                className={`rounded-lg px-3 py-1.5 text-xs ${
                  active ? 'bg-blue-50 font-medium text-blue-700' : 'text-gray-500'
                }`}
              >
                {item.label}
              </button>
            )
          })}
          <button
            onClick={onStart}
            disabled={inSession}
            className="ml-auto rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white disabled:bg-gray-200 disabled:text-gray-400"
          >
            开始面试
          </button>
        </div>
      </div>

      <main className="lg:pl-60">
        <div className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6 lg:py-8">{children}</div>
      </main>
    </div>
  )
}

function IconHome() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.7">
      <path d="M3 8.5 10 3l7 5.5V16a1 1 0 0 1-1 1h-4v-5H8v5H4a1 1 0 0 1-1-1V8.5Z" strokeLinejoin="round" />
    </svg>
  )
}

function IconList() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.7">
      <path d="M6 5h10M6 10h10M6 15h10M3.5 5h.01M3.5 10h.01M3.5 15h.01" strokeLinecap="round" />
    </svg>
  )
}
