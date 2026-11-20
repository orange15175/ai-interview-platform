/**
 * 开始面试：上传简历 /（可选）JD、选题量。
 *
 * 从原 Workspace 里拆出来的原因：这一步是"准备"，不是"面试"。
 * 混在答题页里会让用户以为一登录就开始答题，也没地方放说明文案。
 *
 * 校验规则与后端保持一致，前端的检查只为即时反馈——
 * 真正的判定在后端（create_interview 在 202 之前就会拒掉不合规的输入）。
 */

import { useState } from 'react'

import { RequestFailed, createInterview, type ApiError, type AuthUser } from '../api'
import { JD_TEXT_MIN_CHARS, MAX_FILE_MB, RESUME_ACCEPT, writePrepStartedAt, writePending } from '../shared'

const QUESTION_COUNTS = [5, 10, 15, 20]

export default function SetupPage({
  user,
  onCreated,
}: {
  user: AuthUser
  onCreated: (sessionId: string) => void
}) {
  const [resumeFile, setResumeFile] = useState<File | null>(null)
  const [jdFile, setJdFile] = useState<File | null>(null)
  const [jdText, setJdText] = useState('')
  const [questionCount, setQuestionCount] = useState(15)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const jdTooShort = jdText.trim().length > 0 && jdText.trim().length < JD_TEXT_MIN_CHARS

  const pickResume = (file: File | null) => {
    if (file && file.size > MAX_FILE_MB * 1024 * 1024) {
      setError({ code: 'FILE_TOO_LARGE', message: `简历文件不能超过 ${MAX_FILE_MB}MB`, retryable: false })
      return
    }
    setError(null)
    setResumeFile(file)
  }

  const pickJdFile = (file: File | null) => {
    if (file) setJdText('') // 文件与文字互斥，UI 层先保证
    setJdFile(file)
  }

  const submit = async () => {
    if (!resumeFile || uploading) return
    if (!jdFile && jdTooShort) {
      setError({
        code: 'INVALID_JD_TEXT',
        message: `岗位描述文字至少 ${JD_TEXT_MIN_CHARS} 个字，或直接留空（只按简历出题）`,
        retryable: false,
      })
      return
    }
    setUploading(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('resume', resumeFile)
      if (jdFile) form.append('jd', jdFile)
      else if (jdText.trim()) form.append('jd_text', jdText.trim())
      form.append('question_count', String(questionCount))

      const { session_id } = await createInterview(form)
      writePending(null)
      writePrepStartedAt(session_id, Date.now())
      onCreated(session_id)
    } catch (e) {
      setError(toApiError(e))
    } finally {
      setUploading(false)
    }
  }

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight text-gray-900">开始一场新面试</h1>
        <p className="mt-1 text-sm text-gray-500">
          上传简历后会先解析、再生成一整套题。整个过程通常几十秒，期间可以离开，回来会自动接上。
        </p>
      </header>

      {error && (
        <div className="flex items-start justify-between gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs leading-relaxed text-red-700">
          <span>
            {error.message}
            <span className="ml-1 text-red-400">({error.code})</span>
          </span>
          <button onClick={() => setError(null)} className="shrink-0 text-red-400 hover:text-red-600">
            关闭
          </button>
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <div className="rounded-2xl border border-gray-200 bg-white p-6">
          {/* 简历 */}
          <label className="block text-sm font-medium text-gray-800">
            简历 <span className="ml-1 text-xs font-normal text-red-500">必填</span>
          </label>
          <p className="mt-0.5 text-[11px] text-gray-400">
            支持 DOCX / PDF / 图片，单个文件 ≤ {MAX_FILE_MB}MB
          </p>
          <FilePicker
            accept={RESUME_ACCEPT}
            file={resumeFile}
            onPick={pickResume}
            placeholder="点击选择简历文件"
          />

          {/* JD */}
          <div className="mt-6 flex items-baseline justify-between">
            <label className="text-sm font-medium text-gray-800">
              岗位描述 JD <span className="ml-1 text-xs font-normal text-gray-400">可选</span>
            </label>
            <span className="text-[11px] text-gray-400">留空则只按简历出题</span>
          </div>
          <textarea
            value={jdText}
            onChange={(e) => {
              setJdText(e.target.value)
              if (e.target.value) setJdFile(null)
            }}
            disabled={!!jdFile}
            rows={4}
            placeholder="粘贴岗位描述文字；也可以直接上传 JD 文件"
            className={`mt-2 w-full resize-none rounded-xl border px-3.5 py-2.5 text-sm leading-relaxed outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-100 disabled:bg-gray-50 disabled:text-gray-400 ${
              jdTooShort ? 'border-amber-300' : 'border-gray-300'
            }`}
          />
          <div className="mt-1 flex items-center justify-between">
            <p className={`text-[11px] ${jdTooShort ? 'text-amber-600' : 'text-gray-400'}`}>
              {jdTooShort
                ? `至少 ${JD_TEXT_MIN_CHARS} 个字才能作为岗位描述`
                : `填写则至少 ${JD_TEXT_MIN_CHARS} 个字`}
            </p>
            <span className="text-[11px] text-gray-300">{jdText.trim().length} 字</span>
          </div>

          <div className="mt-3">
            <FilePicker
              accept={RESUME_ACCEPT}
              file={jdFile}
              onPick={pickJdFile}
              placeholder="或上传 JD 文件（.docx / .pdf / 图片）"
              compact
            />
          </div>

          {/* 题量 */}
          <div className="mt-6">
            <label className="text-sm font-medium text-gray-800">题目数量</label>
            <div className="mt-2 grid grid-cols-4 gap-2">
              {QUESTION_COUNTS.map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setQuestionCount(n)}
                  className={`rounded-xl border px-3 py-2 text-sm transition ${
                    questionCount === n
                      ? 'border-blue-500 bg-blue-50 font-medium text-blue-700'
                      : 'border-gray-200 text-gray-600 hover:border-gray-300'
                  }`}
                >
                  {n} 题
                </button>
              ))}
            </div>
            <p className="mt-1.5 text-[11px] text-gray-400">
              追问由评分结果决定，会在主问题之外额外增加 1~2 题
            </p>
          </div>

          <button
            onClick={submit}
            disabled={!resumeFile || uploading || (!jdFile && jdTooShort)}
            className="mt-7 flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 px-6 py-3 text-sm font-medium text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-200 disabled:text-gray-400 disabled:shadow-none"
          >
            {uploading ? (
              <>
                <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
                正在上传…
              </>
            ) : (
              '上传并生成面试题'
            )}
          </button>
          <p className="mt-2.5 text-center text-[11px] text-gray-400">
            本场面试绑定到账号 {user.account}，换设备登录同一账号也能找回
          </p>
        </div>

        {/* 说明 */}
        <aside className="space-y-3">
          <div className="rounded-2xl border border-gray-200 bg-white p-4">
            <h2 className="text-xs font-semibold text-gray-800">简历怎么准备</h2>
            <ul className="mt-2 space-y-1.5 text-[11px] leading-relaxed text-gray-500">
              <li>· 写清项目背景、你的角色、技术选型与结果</li>
              <li>· 有数字最好（QPS、耗时、成本、规模）</li>
              <li>· 图片简历也能识别，但清晰度会影响抽取质量</li>
            </ul>
          </div>
          <div className="rounded-2xl border border-gray-200 bg-white p-4">
            <h2 className="text-xs font-semibold text-gray-800">评分口径</h2>
            <ul className="mt-2 space-y-1.5 text-[11px] leading-relaxed text-gray-500">
              <li>· 每题按评分要点 + 五个维度打分</li>
              <li>· 跳过不计入平均分，评分失败不算 0 分</li>
              <li>· 报告只聚合已落库评分，不会重新问模型</li>
            </ul>
          </div>
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4">
            <h2 className="text-xs font-semibold text-amber-800">注意</h2>
            <p className="mt-1.5 text-[11px] leading-relaxed text-amber-700">
              每题只能提交一次，提交后立刻计分。答题中离开页面不会丢，回来可以继续。
            </p>
          </div>
        </aside>
      </div>
    </div>
  )
}

function FilePicker({
  accept,
  file,
  onPick,
  placeholder,
  compact,
}: {
  accept: string
  file: File | null
  onPick: (file: File | null) => void
  placeholder: string
  compact?: boolean
}) {
  return (
    <label
      className={`mt-2 flex cursor-pointer items-center justify-between gap-3 rounded-xl border border-dashed transition hover:border-blue-300 hover:bg-blue-50/40 ${
        compact ? 'px-3 py-2' : 'px-3.5 py-3'
      } ${file ? 'border-blue-300 bg-blue-50/50' : 'border-gray-300'}`}
    >
      <input
        type="file"
        accept={accept}
        onChange={(e) => onPick(e.target.files?.[0] ?? null)}
        className="hidden"
      />
      <span className="flex min-w-0 items-center gap-2">
        <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0 text-gray-400" fill="none" stroke="currentColor" strokeWidth="1.6">
          <path d="M10 14V4m0 0L6 8m4-4 4 4M4 15.5h12" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span className={`truncate ${compact ? 'text-[11px]' : 'text-xs'} ${file ? 'text-blue-800' : 'text-gray-500'}`}>
          {file ? file.name : placeholder}
        </span>
      </span>
      {file ? (
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault()
            onPick(null)
          }}
          className="shrink-0 text-[11px] text-gray-400 hover:text-red-500"
        >
          移除
        </button>
      ) : (
        <span className="shrink-0 rounded-lg bg-gray-100 px-2 py-1 text-[11px] text-gray-600">选择</span>
      )}
    </label>
  )
}

function toApiError(e: unknown): ApiError {
  if (e instanceof RequestFailed) return e
  return { code: 'UNKNOWN', message: e instanceof Error ? e.message : '未知错误', retryable: false }
}
