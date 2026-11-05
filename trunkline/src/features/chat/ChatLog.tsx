import { useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { nearChatBottom, useConsole } from '@/state/console'
import { useThreadActivity } from '@/state/activity'
import { useThreadSummary } from '@/lib/api/hooks'
import { botLabel } from '@/lib/api/fold'
import { LedgerRow, TraceGroupRow } from './cards'
import { traceRows } from './trace'
import { label, mono, display, statusNote } from '@/components/text'

const CARD_MAX = 'var(--trk-card-max)'

export function ChatLog() {
  const threadId = useConsole((s) => s.selThreadId)
  const channel = useConsole((s) => s.selChannel)
  const thread = useThreadSummary(threadId)
  const detail = useConsole((s) => s.detail)
  const live = useConsole((s) => s.live)
  const running = useConsole((s) => s.running)
  const notices = useConsole((s) => s.notices)
  const ledgerN = useConsole((s) => s.ledgerN)
  const ntOn = useConsole((s) => s.ntOn)
  const activity = useThreadActivity(threadId, ntOn ? threadId : channel === 'trunkline' ? thread?.key ?? detail?.sendKey : undefined, Boolean(threadId) || ntOn)
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntModel = useConsole((s) => s.ntModel)
  const sbFold = useConsole((s) => s.sbFold)
  const mgmtOpen = useConsole((s) => s.mgmtOpen)
  const traceOn = useConsole((s) => s.traceOn) ?? true
  const pvOpen = useConsole((s) => s.pvOpen)
  const { onChatScroll } = useConsole((s) => s.actions)
  const logRef = useRef<HTMLDivElement>(null)
  const ledgerRef = useRef<HTMLDivElement>(null)
  const following = useRef(true)
  const revealing = useRef<HTMLElement | null>(null)
  const dimensions = useRef({ height: 0, viewport: 0 })

  useLayoutEffect(() => {
    following.current = true
    revealing.current = null
    const log = logRef.current!
    const observer = new ResizeObserver(() => {
      if (following.current) log.scrollTop = log.scrollHeight
      dimensions.current = { height: log.scrollHeight, viewport: log.clientHeight }
    })
    observer.observe(log)
    observer.observe(ledgerRef.current!)
    return () => observer.disconnect()
  }, [threadId])

  const cardMax = ['64%', '78%', '88%', '92%'][
    (sbFold ? 0 : 1) + (mgmtOpen ? 1 : 0) + (traceOn && !pvOpen ? 1 : 0)
  ]
  const ledger = detail?.ledger ?? []
  // Latest page first; scroll-top reveals earlier pages (loadOlder), reading
  // them off the relay once the ones in hand run out.
  const visible = ledger.slice(-ledgerN)
  const saved = new Set(ledger.map((item) => item.uid))
  const rows = [...visible, ...live.filter((item) => !saved.has(item.uid))]
  const dispatch = rows.findLast((item) => item.kind === 'dots')
  const items = rows.filter((item) => item.kind !== 'dots')
  const status = activity ?? (dispatch && { text: dispatch.label, attention: false })
  const [shown, setShown] = useState({ threadId, status })
  if (shown.threadId !== threadId || (status && (status.text !== shown.status?.text || status.attention !== shown.status?.attention))) {
    setShown({ threadId, status })
  }
  const earlier = visible.length < ledger.length || detail?.earlier === true
  const blank = ntOn && live.length === 0
  let assistantLabel: string | undefined

  return (
    <div
      id="trk-chatlog"
      ref={logRef}
      className="lt-fade-in"
      onClickCapture={(event) => {
        const toggle = event.target instanceof Element ? event.target.closest<HTMLElement>('button[aria-expanded], summary') : null
        if (!toggle) return
        following.current = false
        revealing.current = null
        const card = toggle.closest<HTMLElement>('.trk-trace-row, details.trk-trace-group')
        const opening = toggle.tagName === 'SUMMARY' ? !card?.hasAttribute('open') : toggle.getAttribute('aria-expanded') === 'false'
        if (!card || !opening) return
        const log = event.currentTarget
        revealing.current = card
        requestAnimationFrame(async () => {
          const fold = card.querySelector(':scope > .lt-fold')
          await Promise.allSettled((fold?.getAnimations() ?? []).map(animation => animation.finished))
          if (revealing.current !== card || !card.isConnected) return
          revealing.current = null
          const viewport = log.getBoundingClientRect()
          const bounds = card.getBoundingClientRect()
          if (bounds.bottom <= viewport.bottom) return
          const scale = viewport.height / log.clientHeight
          // Reveal the bottom when it fits; keep the header visible for taller cards.
          const distance = Math.min(bounds.bottom - viewport.bottom + 8 * scale, bounds.top - viewport.top - 8 * scale) / scale
          if (distance > 0) {
            const behavior = log.style.scrollBehavior
            log.style.scrollBehavior = matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
            // Round up in layout pixels to preserve the reveal gap under CSS zoom.
            log.scrollTop += Math.ceil(distance)
            log.style.scrollBehavior = behavior
          }
        })
      }}
      onWheelCapture={() => { revealing.current = null }}
      onTouchMoveCapture={() => { revealing.current = null }}
      onKeyDownCapture={(event) => {
        if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(event.key)) revealing.current = null
      }}
      onScroll={(e) => {
        const log = e.currentTarget
        // Layout can fire a scroll before ResizeObserver updates the anchor.
        if (log.scrollHeight === dimensions.current.height && log.clientHeight === dimensions.current.viewport) {
          following.current = nearChatBottom()
        }
        onChatScroll(e.currentTarget.scrollTop)
      }}
      style={
        {
          '--trk-card-max': cardMax,
          position: 'relative',
          display: 'flex',
          flexDirection: 'column',
          flex: 1,
          minHeight: 0,
          overflowY: 'scroll',
          overflowX: 'hidden',
        } as CSSProperties
      }
    >
      {ledger.length > 0 && !ntOn && !earlier && (
        <div style={{ textAlign: 'center', ...label(8.5), color: 'var(--fg-3)', padding: '2px 0' }}>
          // start of thread
        </div>
      )}
      {ledger.length > 0 && !ntOn && earlier && (
        <div style={{ textAlign: 'center', ...statusNote, color: 'var(--fg-3)', padding: '2px 0' }}>
          ↑ scroll — earlier messages load step by step
        </div>
      )}
      <div ref={ledgerRef} className="trk-ledger-col">
        {blank && (
          <div
            className="lt-fade-in"
            style={{
              margin: 'auto',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 10,
              textAlign: 'center',
              padding: '30px 0',
            }}
          >
            <span style={{ ...label(8, '.22em'), color: 'var(--fg-3)' }}>// blank ledger</span>
            <span style={{ ...display(16, 600), color: 'var(--fg-1)' }}>New trunkline thread</span>
            <span style={{ ...mono(9), lineHeight: 1.8, color: 'var(--fg-3)', maxWidth: 340 }}>
              no session yet — the first directive registers the thread and summons{' '}
              <span style={{ color: 'var(--color-accent)', fontWeight: 700 }}>
                {ntAgent} · {ntModel}
              </span>
              . set agent · model · effort in the input card below.
            </span>
          </div>
        )}
        {traceRows(items, running).map((item, i) => {
          if (item.kind === 'trace') return <TraceGroupRow key={item.uid} group={item} cardMax={CARD_MAX} />
          let showAgentLabel = true
          if (item.kind === 'agent' || item.kind === 'stream') {
            const label = item.label ?? botLabel()
            showAgentLabel = label !== assistantLabel
            assistantLabel = label
          } else if (item.kind === 'user' || item.kind === 'end' || item.kind === 'session-open') {
            assistantLabel = undefined
          }
          // The newest page enters first; earlier reveals stay calm.
          return <LedgerRow key={item.uid} item={item} cardMax={CARD_MAX} showAgentLabel={showAgentLabel} i={Math.min(items.length - 1 - i, 7)} />
        })}
        {notices.map((item) => (
          <LedgerRow key={item.uid} item={item} cardMax={CARD_MAX} />
        ))}
        {shown.status && (
          <div
            key={`activity-${threadId}`}
            className="trk-activity-motion"
            data-exiting={status ? undefined : ''}
            aria-hidden={status ? undefined : true}
            onAnimationEnd={event => {
              if (event.target === event.currentTarget && !status) setShown({ threadId, status: undefined })
            }}
          >
            <LedgerRow item={{ kind: 'dots', uid: 'activity', label: shown.status.text, attention: shown.status.attention }} cardMax={CARD_MAX} />
          </div>
        )}
      </div>
    </div>
  )
}
