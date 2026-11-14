import { Fragment, useLayoutEffect, useRef, type CSSProperties } from 'react'
import { nearChatBottom, useConsole } from '@/state/console'
import { useAgents } from '@/lib/api/hooks'
import { botLabel } from '@/lib/api/fold'
import { AgentLabel, LedgerRow, TraceGroupRow } from './cards'
import { MessageContext } from './MessageReferences'
import { traceRows } from './trace'
import { label, mono, display, statusNote } from '@/components/text'

const CARD_MAX = 'var(--trk-card-max)'

export function ChatLog() {
  const threadId = useConsole((s) => s.selThreadId)
  const { data: agents } = useAgents(true, false)
  const detail = useConsole((s) => s.detail)
  const live = useConsole((s) => s.live)
  const running = useConsole((s) => s.running)
  const notices = useConsole((s) => s.notices)
  const ledgerN = useConsole((s) => s.ledgerN)
  const ntOn = useConsole((s) => s.ntOn)
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
  const opened = useRef<HTMLElement | null>(null)
  const dimensions = useRef({ height: 0, viewport: 0 })

  useLayoutEffect(() => {
    following.current = true
    revealing.current = null
    opened.current = null
    const log = logRef.current!
    let ledgerHeight = 0  // The ledger's height as last laid out, to tell what a swap took away.
    const observer = new ResizeObserver(() => {
      if (following.current) {
        // Following never carries a card the reader just opened off the top: once it
        // would, the card's header is held there and the tail is theirs to return to.
        const card = opened.current?.isConnected ? opened.current : null
        const viewport = log.getBoundingClientRect()
        const room = card ? (card.getBoundingClientRect().top - viewport.top) * log.clientHeight / viewport.height - 8 : Infinity
        if (log.scrollHeight - log.clientHeight - log.scrollTop > room) {
          log.scrollTop += Math.max(0, Math.floor(room))
          following.current = false
        } else log.scrollTop = log.scrollHeight
      }
      dimensions.current = { height: log.scrollHeight, viewport: log.clientHeight }
      ledgerHeight = ledgerRef.current!.offsetHeight
    })
    observer.observe(log)
    observer.observe(ledgerRef.current!)
    // A row that joins the tail of the ledger, or of an open group, unfolds, so what is
    // above it slides up to make room instead of jumping. A row swapped in for a taller
    // one, as when a lone call leaves its group, gives the difference back over the same
    // moment. History, older pages and replays land as they are.
    const arrivals = new MutationObserver(records => {
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) return
      const added = new Set(records.flatMap(record => [...record.addedNodes]))
      const tokens = getComputedStyle(log)
      const timing = { duration: parseFloat(tokens.getPropertyValue('--motion-base')), easing: tokens.getPropertyValue('--ease-out').trim() }
      const swapped: HTMLElement[] = []
      for (const list of new Set(records.map(record => record.target))) {
        if (!(list instanceof HTMLElement) || !list.matches('.trk-ledger-col, .trk-trace-items')) continue
        const rows = [...list.children].filter(row => row instanceof HTMLElement)
        const fresh = rows.filter(row => added.has(row))
        const removed = records.filter(record => record.target === list).flatMap(record => [...record.removedNodes]).filter(node => node instanceof HTMLElement).length
        // One row may have taken another's place; the rest must be new rows at the tail.
        const joining = fresh.slice(removed)
        const first = rows.indexOf(joining[0])
        if (removed > 1 || removed > fresh.length || joining.length > 8 || (joining.length && (first < 1 || rows.length - first !== joining.length))) continue
        swapped.push(...fresh.slice(0, removed))
        const gap = parseFloat(getComputedStyle(list).rowGap)
        for (const row of joining) {
          // The row keeps its own height while a shrinking margin and clip reveal it,
          // so text that streams in, or a fold that starts meanwhile, is never fought.
          row.animate([
            { offset: 0, marginTop: `${parseFloat(getComputedStyle(row).marginTop) - gap}px`, marginBottom: `${parseFloat(getComputedStyle(row).marginBottom) - row.offsetHeight}px`, opacity: 0, clipPath: 'inset(0 0 100% 0)' },
            { offset: 1, clipPath: 'inset(0)' },
          ], timing)
        }
      }
      const collapsed = swapped.length === 1 ? ledgerHeight - ledgerRef.current!.offsetHeight : 0
      if (collapsed > 2) swapped[0].animate([{ offset: 0, marginBottom: `${parseFloat(getComputedStyle(swapped[0]).marginBottom) + collapsed}px`, opacity: 0 }], timing)
    })
    arrivals.observe(ledgerRef.current!, { childList: true, subtree: true })
    const selectHistory = (event: MouseEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest('#trk-timeline [data-uid]')) return
      following.current = false
      revealing.current = null
    }
    // Release the tail before a timeline selection opens a card and resizes it.
    document.addEventListener('click', selectHistory, true)
    return () => {
      observer.disconnect()
      arrivals.disconnect()
      document.removeEventListener('click', selectHistory, true)
    }
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
  const items = rows.filter((item) => item.kind !== 'dots')
  const renderedRows = traceRows(items, running)
  const firstLive = live.find(item => item.kind !== 'dots' && !saved.has(item.uid))
  const copyable = new Set<string>()
  let lastOutput: string | undefined
  for (const item of items) {
    if (item === firstLive || item.kind === 'user' || item.kind === 'end' || item.kind === 'session-open') {
      if (lastOutput) copyable.add(lastOutput)
      lastOutput = undefined
    }
    if (item.kind === 'agent' || item.kind === 'stream') lastOutput = item.uid
  }
  if (!running && lastOutput) copyable.add(lastOutput)
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
        revealing.current = null
        const card = toggle.closest<HTMLElement>('.trk-trace-row, details.trk-trace-group')
        const opening = toggle.tagName === 'SUMMARY' ? !card?.hasAttribute('open') : toggle.getAttribute('aria-expanded') === 'false'
        if (following.current) {
          // A fold or unfold is not the reader scrolling away, so the tail stays pinned
          // through it; a card being opened is watched for as long as its unfolding lasts.
          const watched = opening ? card ?? toggle.closest<HTMLElement>('.trk-ledger-col > *') : null
          opened.current = watched
          if (watched) window.setTimeout(() => { if (opened.current === watched) opened.current = null }, 600)
          return
        }
        if (!card || !opening) return
        const log = event.currentTarget
        revealing.current = card
        requestAnimationFrame(async () => {
          // The disclosure shares the native group's timing; its pseudo-element isn't exposed by getAnimations in every browser.
          const motion = card.querySelector(':scope > .lt-fold, :scope > summary > .lt-disclose')
          await Promise.allSettled((motion?.getAnimations() ?? []).map(animation => animation.finished))
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
        {renderedRows.map((item, i) => {
          const first = item.kind === 'trace' ? item.items[0] : item.kind === 'annotated' ? item.item : item
          let heading
          if (first.kind === 'agent' || first.kind === 'stream' || first.kind === 'think' || first.kind === 'tool' || first.kind === 'context') {
            const label = first.kind === 'agent' || first.kind === 'stream' ? first.label ?? botLabel(first.agentId) : botLabel(first.agentId)
            const author = first.agentId ?? label
            if (author !== assistantLabel) {
              const agent = agents?.find(agent => agent.id === first.agentId)
              heading = <AgentLabel label={label} iconUrl={agent?.icon_url} />
            }
            assistantLabel = author
          } else if (first.kind === 'user' || first.kind === 'end' || first.kind === 'session-open') {
            assistantLabel = undefined
          }
          // The newest page enters first; earlier reveals stay calm.
          return <Fragment key={item.uid}>{heading}{item.kind === 'trace'
            ? <TraceGroupRow group={item} cardMax={CARD_MAX} />
            : item.kind === 'context' ? <MessageContext item={item} cardMax={CARD_MAX} />
            : <LedgerRow item={item.kind === 'annotated' ? item.item : item} cardMax={CARD_MAX} showAgentLabel={false} showCopy={copyable.has(item.uid)} i={Math.min(renderedRows.length - 1 - i, 7)}>
              {item.kind === 'annotated' && item.after.map(context => <MessageContext key={context.uid} item={context} cardMax={CARD_MAX} />)}
            </LedgerRow>}</Fragment>
        })}
        {notices.map((item) => (
          <LedgerRow key={item.uid} item={item} cardMax={CARD_MAX} />
        ))}
      </div>
    </div>
  )
}
