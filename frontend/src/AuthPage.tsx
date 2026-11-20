import { useState, type FormEvent } from 'react'
import {
  RequestFailed,
  loginAccount,
  registerAccount,
  type AuthUser,
} from './api'

/**
 * 登录 / 注册页（账号 + 密码，账号唯一）。
 *
 * 与前端的契约边界：
 * - 登录态由后端 HttpOnly Cookie 承载，本组件成功后只把 user 交给上层，
 *   绝不往 localStorage 写任何账号信息（写了就等于给了 XSS 一把钥匙）；
 * - 校验规则在这里先跑一遍**只为即时反馈**，真正的判定仍在后端：
 *   前端校验可被绕过，后端不校验才是事故；
 * - 错误文案按后端 code 分支，不靠 HTTP 状态码猜。
 */

const ACCOUNT_RE = /^[a-z0-9_.@+-]{3,64}$/
const ACCOUNT_HINT = '3~64 位，只能包含字母、数字和 _ . @ + -'
const PASSWORD_HINT = '至少 8 位，不能是纯数字或纯字母'

function checkAccount(raw: string): string | null {
  const account = raw.trim().toLowerCase()
  if (!account) return '请输入账号'
  if (!ACCOUNT_RE.test(account)) return `账号格式不对：${ACCOUNT_HINT}`
  return null
}

function checkPassword(pwd: string, needStrength: boolean): string | null {
  if (!pwd) return '请输入密码'
  if (!needStrength) return null // 登录只校验非空：规则可能变过，别拦老密码
  if (pwd.trim() !== pwd) return '密码首尾不能是空格'
  if (pwd.length < 8) return '密码至少 8 位'
  if (/^\d+$/.test(pwd) || /^[A-Za-z]+$/.test(pwd)) return '密码不能是纯数字或纯字母，请混合使用'
  return null
}

function explain(e: unknown): string {
  if (e instanceof RequestFailed) {
    switch (e.code) {
      case 'INVALID_CREDENTIALS':
        return '账号或密码不正确'
      case 'ACCOUNT_TAKEN':
        return '这个账号已被注册，换一个，或直接登录'
      case 'NETWORK_ERROR':
        return '连不上后端服务，请确认后端已启动'
      case 'INVALID_REQUEST':
        // 后端会带 "account: xxx" 这样的定位前缀，展示时去掉英文键名
        return e.message.replace(/^[a-z_]+:\s*/i, '')
      default:
        return e.message
    }
  }
  return e instanceof Error ? e.message : '未知错误'
}

export default function AuthPage({ onSuccess }: { onSuccess: (user: AuthUser) => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isRegister = mode === 'register'

  const switchMode = (next: 'login' | 'register') => {
    if (busy || next === mode) return
    setMode(next)
    setError(null)
    setPassword('')
    setConfirm('')
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy) return

    const accountErr = checkAccount(account)
    if (accountErr) return setError(accountErr)
    const pwdErr = checkPassword(password, isRegister)
    if (pwdErr) return setError(pwdErr)
    if (isRegister && password !== confirm) return setError('两次输入的密码不一致')

    const name = account.trim().toLowerCase()
    setBusy(true)
    setError(null)
    try {
      const { user } = isRegister
        ? await registerAccount(name, password)
        : await loginAccount(name, password)
      onSuccess(user)
    } catch (err) {
      setError(explain(err))
      setPassword('')
      setConfirm('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen flex-col bg-gray-50">
      <div className="flex flex-1 items-center justify-center px-4 py-10">
        <div className="w-full max-w-sm">
          <div className="mb-6 flex flex-col items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-blue-600 text-sm font-medium text-white">
              AI
            </div>
            <div className="text-center">
              <h1 className="text-base font-medium text-gray-900">AI 模拟面试</h1>
              <p className="mt-1 text-xs text-gray-500">
                {isRegister ? '创建账号，面试记录随时找回' : '登录后继续你的模拟面试'}
              </p>
            </div>
          </div>

          <div className="rounded-2xl border border-gray-200 bg-white px-6 py-6">
            <div className="mb-5 grid grid-cols-2 gap-1 rounded-xl bg-gray-100 p-1 text-xs">
              {(['login', 'register'] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => switchMode(m)}
                  className={`rounded-lg px-3 py-1.5 transition-colors ${
                    mode === m ? 'bg-white font-medium text-gray-900 shadow-sm' : 'text-gray-500'
                  }`}
                >
                  {m === 'login' ? '登录' : '注册'}
                </button>
              ))}
            </div>

            <form onSubmit={submit} className="space-y-4">
              <div>
                <label htmlFor="account" className="block text-xs font-medium text-gray-700">
                  账号
                </label>
                <input
                  id="account"
                  value={account}
                  onChange={(e) => setAccount(e.target.value)}
                  autoComplete="username"
                  autoCapitalize="off"
                  autoCorrect="off"
                  spellCheck={false}
                  maxLength={64}
                  placeholder="例如 zhangsan 或 me@mail.com"
                  className="mt-1.5 w-full rounded-xl border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500"
                />
              </div>

              <div>
                <label htmlFor="password" className="block text-xs font-medium text-gray-700">
                  密码
                </label>
                <input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={isRegister ? 'new-password' : 'current-password'}
                  maxLength={128}
                  placeholder={isRegister ? PASSWORD_HINT : '输入密码'}
                  className="mt-1.5 w-full rounded-xl border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500"
                />
              </div>

              {isRegister && (
                <div>
                  <label htmlFor="confirm" className="block text-xs font-medium text-gray-700">
                    确认密码
                  </label>
                  <input
                    id="confirm"
                    type="password"
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    autoComplete="new-password"
                    maxLength={128}
                    placeholder="再输一次"
                    className="mt-1.5 w-full rounded-xl border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500"
                  />
                </div>
              )}

              {error && (
                <p
                  role="alert"
                  className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700"
                >
                  {error}
                </p>
              )}

              <button
                type="submit"
                disabled={busy}
                className="w-full rounded-xl bg-blue-600 px-6 py-2.5 text-sm text-white transition-opacity disabled:opacity-40"
              >
                {busy ? '处理中…' : isRegister ? '创建账号并开始' : '登录'}
              </button>
            </form>

            <p className="mt-4 text-center text-[11px] leading-relaxed text-gray-400">
              {isRegister ? `账号：${ACCOUNT_HINT}` : '忘记密码暂不支持找回，可重新注册一个账号'}
            </p>
          </div>

          <p className="mt-4 text-center text-[11px] text-gray-400">
            登录态保存在 HttpOnly Cookie 中，脚本读不到；换设备登录同一账号即可找回记录
          </p>
        </div>
      </div>
    </div>
  )
}
