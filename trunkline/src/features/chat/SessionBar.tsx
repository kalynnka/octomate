import { useEffect, useState } from 'react'
import { useConsole } from '@/state/console'
import { channelMeta } from '@/lib/api/live'
import { mono } from '@/components/text'

const vline = <i style={{ width: 1, height: 12, background: 'var(--trk-vline)', flexShrink: 0 }} />

/** Live ledger clock — a functional status readout, ticking once a second. */
function useClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  return now.toLocaleTimeString('en-GB', { hour12: false })
}

export function SessionBar() {
  const detail = useConsole((s) => s.detail)
  const selChannel = useConsole((s) => s.selChannel)
  const selThreadId = useConsole((s) => s.selThreadId)
  const ntOn = useConsole((s) => s.ntOn)
  const ntStarted = useConsole((s) => s.ntStarted)
  const clock = useClock()

  const lastSes = detail?.sessions[detail.sessions.length - 1]
  const sesChip = ntOn
    ? ntStarted
      ? 'session live'
      : 'no session yet'
    : lastSes
      ? `${lastSes.id}${lastSes.status ? ` · ${lastSes.status}` : ''}`
      : '—'
  // The channel list is the connected tentacles only, so a native channel is
  // not in it; its display name is the same table the sidebar reads.
  const routeChip = `${channelMeta(selChannel).label.toLowerCase()}/${detail?.key ?? selThreadId}`
  // A thread that has not run yet is on nothing. For one that has, no read
  // reports its usage or context size (see README's gap list), so the slots
  // hold an em dash rather than a number nothing measured.
  const fresh = ntOn && !ntStarted
  const ctxK = fresh ? 0 : null
  const ctxW = ctxK === null ? '0%' : `${Math.round((ctxK / 200) * 100)}%`
  const usageChip = fresh ? 'Σ in 0 · cache 0 (—) · out 0' : '—'
  const msgChip = `${detail?.msgCount ?? 0} msg`

  const chip = (title: string, children: React.ReactNode, extra?: React.CSSProperties) => (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        height: 18,
        boxSizing: 'border-box',
        padding: '0 7px',
        letterSpacing: '.04em',
        textTransform: 'none',
        whiteSpace: 'nowrap',
        ...extra,
      }}
    >
      {children}
    </span>
  )

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        height: 30,
        boxSizing: 'border-box',
        margin: 0,
        padding: '0 17px',
        borderTop: '2px solid var(--trk-bracket)',
        background: 'color-mix(in srgb, var(--color-ink) 5%, transparent)',
        ...mono(7.5, 700),
        letterSpacing: '.1em',
        textTransform: 'uppercase',
        color: 'var(--fg-2)',
      }}
    >
      <span style={{ flex: '1 1 auto', minWidth: 0, height: 18, display: 'flex', flexWrap: 'wrap', alignContent: 'flex-start', alignItems: 'center', overflow: 'hidden' }}>
        {chip('current session of this thread', (
          <>
            <i style={{ width: 5, height: 5, borderRadius: 9999, background: 'var(--color-sage)' }} />
            {sesChip}
          </>
        ))}
        {vline}
        {chip('owning route', routeChip, { color: 'var(--fg-3)' })}
      </span>
      <span style={{ flex: '0 0 10px' }} />
      <span style={{ flex: '0 1 auto', height: 18, display: 'flex', flexFlow: 'row-reverse wrap', alignContent: 'flex-start', alignItems: 'center', overflow: 'hidden' }}>
        {chip('ledger clock', clock, { color: 'var(--fg-3)' })}
        {vline}
        {chip('total session usage — input · cache hits (rate) · output', usageChip)}
        {vline}
        {chip('context window', (
          <>
            <i
              style={{
                width: 56,
                height: 7,
                boxSizing: 'border-box',
                border: '1px solid var(--trk-vline)',
                background: 'repeating-linear-gradient(90deg, var(--trk-vline) 0 1px, transparent 1px 6px)',
                display: 'inline-flex',
                overflow: 'hidden',
              }}
            >
              <i style={{ width: ctxW, background: 'var(--color-ghost)' }} />
            </i>
            {ctxK === null ? '—' : `${ctxK}k`}/200k
          </>
        ))}
        {vline}
        {chip('messages in this thread', msgChip)}
      </span>
    </div>
  )
}
