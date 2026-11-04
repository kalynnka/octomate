import { ellipsis, mono } from '@/components/text'
import { useThreadActivity } from '@/state/activity'

export function ThreadActivity({ id, threadKey, enabled = true, fallback }: {
  id: string
  threadKey?: string
  enabled?: boolean
  fallback?: string
}) {
  const activity = useThreadActivity(id, threadKey, enabled)
  if (!activity && !fallback) return null
  return (
    <span
      data-thread-activity={activity ? id : undefined}
      title={activity?.text}
      style={{ ...mono(8.5), ...ellipsis, minWidth: 0, color: activity?.attention ? 'var(--color-gold)' : activity ? 'var(--color-accent)' : 'var(--fg-3)' }}
    >
      {activity?.text ?? fallback}
    </span>
  )
}
