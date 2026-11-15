import { useEffect, useRef, useState } from 'react'
import { useConsole } from '@/state/console'
import { useThreadActivity } from '@/state/activity'
import { useThreadSummary } from '@/lib/api/hooks'
import { LedgerRow } from './cards'

/** How long a phrase stays before the next may replace it, so a burst of changes reads as lines, not a blur. */
const DWELL = 700

/** The thread's live status, docked on the composer's top edge where it does not scroll with the ledger. */
export function ActivityLine() {
  const threadId = useConsole((s) => s.selThreadId)
  const channel = useConsole((s) => s.selChannel)
  const thread = useThreadSummary(threadId)
  const detail = useConsole((s) => s.detail)
  const live = useConsole((s) => s.live)
  const ntOn = useConsole((s) => s.ntOn)
  const activity = useThreadActivity(threadId, ntOn ? threadId : channel === 'trunkline' ? thread?.key ?? detail?.sendKey : undefined, Boolean(threadId) || ntOn)
  const text = activity?.text ?? live.findLast((item) => item.kind === 'dots')?.label
  const attention = activity?.attention ?? false
  const [shown, setShown] = useState({ threadId, status: text === undefined ? undefined : { text, attention } })
  const changed = text !== undefined && (text !== shown.status?.text || attention !== shown.status.attention)
  // The first phrase, another thread and a call for attention are not held back.
  const urgent = shown.threadId !== threadId || (changed && (!shown.status || attention !== shown.status.attention))
  if (urgent) setShown({ threadId, status: text === undefined ? undefined : { text, attention } })
  const since = useRef(0)
  useEffect(() => {
    since.current = Date.now()
  }, [shown])
  useEffect(() => {
    if (!changed || urgent) return
    const timer = setTimeout(() => setShown({ threadId, status: { text, attention } }), since.current + DWELL - Date.now())
    return () => clearTimeout(timer)
  }, [changed, urgent, threadId, text, attention])
  if (!shown.status) return null
  return (
    <div
      className="trk-activity-dock"
      data-exiting={text === undefined ? '' : undefined}
      aria-hidden={text === undefined ? true : undefined}
      onAnimationEnd={(event) => {
        // Gone once the last of its exit animations has played, however many the motion preference gives it.
        const last = getComputedStyle(event.currentTarget).animationName.split(', ').at(-1)
        if (event.target === event.currentTarget && text === undefined && event.animationName === last) setShown({ threadId, status: undefined })
      }}
    >
      <div>
        <LedgerRow item={{ kind: 'dots', uid: 'activity', label: shown.status.text, attention: shown.status.attention }} cardMax="100%" />
      </div>
    </div>
  )
}
