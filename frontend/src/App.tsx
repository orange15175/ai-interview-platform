/**
 * 应用入口：登录闸门 + 页面路由。
 *
 * 页面划分（为什么不是一登录就进答题）：
 *   home    落地页：上次面得怎样、有没有没面完的、要不要开始新的
 *   setup   开始面试：上传简历 / JD、选题量
 *   session 答题：准备中 → 逐题作答，结束后跳报告
 *   report  报告：总分、五维、薄弱点 + 逐条记录
 *   history 记录：全部历史，可继续或看报告
 *
 * 身份：必须先登录。登录态是后端 HttpOnly Cookie，前端只通过 /api/auth/me
 * 问"我是谁"，不在 localStorage 存任何账号信息。
 *
 * 没有引入 react-router：路由状态只有 5 个值、且不需要可分享的 URL。
 * 等真的需要"链接直达某份报告"时再引，现在引只是多一层依赖。
 */

import { useCallback, useEffect, useState } from 'react'

import AuthPage from './AuthPage'
import AppShell, { type ViewName } from './components/AppShell'
import HistoryPage from './pages/HistoryPage'
import HomePage from './pages/HomePage'
import ReportPage from './pages/ReportPage'
import SessionPage from './pages/SessionPage'
import SetupPage from './pages/SetupPage'
import { getMe, logoutAccount, type AuthUser } from './api'

type View =
  | { name: 'home' }
  | { name: 'setup' }
  | { name: 'session'; sessionId: string }
  | { name: 'report'; sessionId: string }
  | { name: 'history' }

export default function App() {
  // undefined = 还在问后端"我是谁"（不要闪一下登录页）；null = 确实没登录
  const [user, setUser] = useState<AuthUser | null | undefined>(undefined)

  useEffect(() => {
    let alive = true
    getMe()
      .then((res) => alive && setUser(res.user))
      // 后端不可达时按未登录处理：登录页会把 NETWORK_ERROR 原样告诉用户，
      // 比停在一个转圈的空屏上更可诊断。
      .catch(() => alive && setUser(null))
    return () => {
      alive = false
    }
  }, [])

  if (user === undefined) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 text-sm text-gray-400">
        正在确认登录状态…
      </div>
    )
  }

  if (user === null) {
    return <AuthPage onSuccess={(u) => setUser(u)} />
  }

  return (
    <Workspace
      user={user}
      onLogout={async () => {
        try {
          await logoutAccount()
        } catch {
          /* 登出失败也要回登录页：Cookie 清不掉时后端下次请求会当游客处理 */
        }
        setUser(null)
      }}
    />
  )
}

function Workspace({ user, onLogout }: { user: AuthUser; onLogout: () => void }) {
  const [view, setView] = useState<View>({ name: 'home' })

  const goHome = useCallback(() => setView({ name: 'home' }), [])
  const goStart = useCallback(() => setView({ name: 'setup' }), [])
  const goHistory = useCallback(() => setView({ name: 'history' }), [])

  const openSession = useCallback(
    (sessionId: string) => setView({ name: 'session', sessionId }),
    [],
  )
  const openReport = useCallback(
    (sessionId: string) => setView({ name: 'report', sessionId }),
    [],
  )

  // 面试结束（答完最后一题或提前结束）：直接进报告页，答题页不渲染报告
  const handleFinished = useCallback(
    (sessionId: string) => setView({ name: 'report', sessionId }),
    [],
  )

  const navigate = useCallback((next: ViewName) => {
    if (next === 'home') setView({ name: 'home' })
    else if (next === 'history') setView({ name: 'history' })
    else if (next === 'setup') setView({ name: 'setup' })
    // session / report 需要参数，走 openSession / openReport
  }, [])

  return (
    <AppShell
      user={user}
      current={view.name}
      onNavigate={navigate}
      onStart={goStart}
      onLogout={onLogout}
    >
      {view.name === 'home' && (
        <HomePage
          user={user}
          onStart={goStart}
          onResume={openSession}
          onOpenReport={openReport}
          onShowHistory={goHistory}
        />
      )}

      {view.name === 'setup' && (
        <SetupPage
          user={user}
          onCreated={(sessionId) => {
            setView({ name: 'session', sessionId })
          }}
        />
      )}

      {view.name === 'session' && (
        <SessionPage
          // key 换会话时重建组件：否则上一场的 pendingRef / 轮询计时会带过来
          key={view.sessionId}
          sessionId={view.sessionId}
          onFinished={handleFinished}
          onExit={goHome}
        />
      )}

      {view.name === 'report' && (
        <ReportPage
          key={view.sessionId}
          sessionId={view.sessionId}
          onBack={goHistory}
          onRestart={goStart}
        />
      )}

      {view.name === 'history' && (
        <HistoryPage onResume={openSession} onOpenReport={openReport} onStart={goStart} />
      )}
    </AppShell>
  )
}
