import { scoreClass, type FeedbackCard } from '../shared'

// 上一题反馈卡片：评分 + 点评 + 待补充点。跳过时明确说"不计入评分"。
export default function FeedbackCardView({ card }: { card: FeedbackCard }) {
  const { evaluation, decision, questionLabel } = card
  if (!evaluation && !decision) return null
  return (
    <div className="rounded-2xl border border-gray-200 bg-white px-5 py-4">
      <div className="flex items-center gap-2 text-[11px] text-gray-500">
        <span className="font-medium text-gray-600">上一题反馈</span>
        <span className="text-gray-300">·</span>
        <span>{questionLabel}</span>
      </div>
      {evaluation ? (
        <div className="mt-2 flex items-start gap-3">
          <span
            className={`shrink-0 rounded-xl border px-2.5 py-1 text-sm font-semibold tabular-nums ${scoreClass(evaluation.score)}`}
          >
            {evaluation.score} 分
          </span>
          <div className="min-w-0 text-sm leading-relaxed text-gray-700">
            {evaluation.feedback}
            {!!evaluation.missing_points?.length && (
              <p className="mt-1.5 text-xs text-amber-700">
                待补充：{evaluation.missing_points.join('；')}
              </p>
            )}
          </div>
        </div>
      ) : (
        <p className="mt-2 text-sm text-gray-500">本题已跳过，不计入评分。</p>
      )}
    </div>
  )
}
