import { Fragment } from 'react'
import { goLedgerTarget, useConsole } from '@/state/console'
import { useRailDrag } from '@/lib/useRailDrag'
import { chipLabel, ellipsis, label, microSection, mono, serif } from '@/components/text'
import { Disclose, Fold } from '@/components/Fold'
import type { LedgerItem, SessionKind, SessionTone, ToolDetail } from '@/lib/api/types'

type TlKind = 'turn' | 'msg' | 'think' | 'plan' | 'tool' | 'write' | 'ask' | 'sub' | 'card' | 'end'

interface TlEvent {
  k: TlKind
  title: string
  sub: string
  t: string
  /** ledger DOM id the row jumps to */
  tgt: string
  /** file name when the event opens the review panel */
  file?: string
  badge?: { label: string; tone: 'gold' | 'terra' }
  /** a still-running tool — tag lights accent */
  live?: boolean
  /** resolved feeler — dot and tag turn sage */
  resolved?: boolean
}

interface DotSpec {
  tag: string
  tagColor: string
  rad: string
  fill: string
  bd: string
  bs: 'solid' | 'dashed'
  titleColor: string
}

/** Dot/tag styling per event kind — ported from the comp's KS map. */
const KS: Record<TlKind, DotSpec> = {
  turn: { tag: 'turn', tagColor: 'var(--color-teal)', rad: '9999px', fill: 'var(--color-teal)', bd: 'var(--color-teal)', bs: 'solid', titleColor: 'var(--fg-1)' },
  msg: { tag: 'msg', tagColor: 'var(--fg-2)', rad: '9999px', fill: 'var(--fg-1)', bd: 'var(--fg-1)', bs: 'solid', titleColor: 'var(--fg-1)' },
  think: { tag: 'think', tagColor: 'var(--fg-3)', rad: '9999px', fill: 'var(--card-bg)', bd: 'var(--fg-3)', bs: 'solid', titleColor: 'var(--fg-2)' },
  plan: { tag: 'plan', tagColor: 'var(--fg-3)', rad: '9999px', fill: 'var(--card-bg)', bd: 'var(--fg-3)', bs: 'solid', titleColor: 'var(--fg-2)' },
  tool: { tag: 'tool', tagColor: 'var(--fg-3)', rad: '0', fill: 'var(--card-bg)', bd: 'var(--color-accent)', bs: 'solid', titleColor: 'var(--color-accent)' },
  write: { tag: 'write', tagColor: 'var(--color-gold)', rad: '0', fill: 'var(--color-gold)', bd: 'var(--color-gold)', bs: 'solid', titleColor: 'var(--fg-1)' },
  ask: { tag: 'ask', tagColor: 'var(--color-gold)', rad: '0', fill: 'var(--color-gold)', bd: 'var(--color-gold)', bs: 'solid', titleColor: 'var(--fg-1)' },
  sub: { tag: 'sub', tagColor: 'var(--color-teal)', rad: '0', fill: 'var(--card-bg)', bd: 'var(--color-teal)', bs: 'dashed', titleColor: 'var(--fg-1)' },
  card: { tag: 'card', tagColor: 'var(--color-accent)', rad: '0', fill: 'var(--color-accent)', bd: 'var(--color-accent)', bs: 'solid', titleColor: 'var(--color-accent)' },
  end: { tag: 'end', tagColor: 'var(--fg-3)', rad: '9999px', fill: 'var(--line-color)', bd: 'var(--line-color)', bs: 'solid', titleColor: 'var(--fg-3)' },
}

const KIND_COLOR: Record<SessionKind, string> = {
  entry: 'var(--fg-3)',
  summon: 'var(--color-terra)',
  teleport: 'var(--color-teal)',
  ingest: 'var(--color-teal)',
}

const TONE_COLOR: Record<SessionTone, string> = {
  accent: 'var(--color-accent)',
  gold: 'var(--color-gold)',
  sage: 'var(--color-sage)',
  teal: 'var(--color-teal)',
  ghost: 'var(--fg-3)',
}

function toolSub(detail: ToolDetail): string {
  switch (detail.type) {
    case 'plain':
      return detail.res.split('\n')[0]
    case 'kv':
      return detail.heading
    case 'checks':
      return detail.rows.map((r) => `${r.name} ${r.verdict}`).join(' · ')
    case 'log':
      return detail.pre[0]?.text.split('\n')[0] ?? detail.heading
    case 'summon':
      return detail.rows.find((r) => r[0] === 'brief')?.[1] ?? detail.rows[0]?.[1] ?? ''
  }
}

/** Map one ledger/live item to a timeline event; null = not indexed. */
function eventOf(item: LedgerItem, agent?: string): TlEvent | null {
  const tgt = `pm-${item.uid}`
  switch (item.kind) {
    case 'user':
      return {
        k: 'turn',
        title: item.who,
        sub: (item.chips?.length ? `[${item.chips.length} note] ` : '') + item.text,
        t: item.t,
        tgt,
      }
    case 'agent':
      return {
        k: 'msg',
        title: item.label.split(' · ')[0],
        sub: item.blocks.find((b) => b.type === 'p')?.text ?? '',
        t: item.label.match(/\d\d:\d\d/)?.[0] ?? '',
        tgt,
      }
    case 'think':
      return { k: 'think', title: `thinking · ${item.dur}`, sub: item.text, t: '', tgt }
    case 'plan': {
      const active = item.steps.find((s) => s.status === 'active')
      return {
        k: 'plan',
        title: `plan · ${active?.n ?? '··'} / ${String(item.steps.length).padStart(2, '0')}`,
        sub: active ? `${active.text} — active` : '',
        t: '',
        tgt,
      }
    }
    case 'tool':
      return {
        k: 'tool',
        title: item.name,
        sub: item.status === 'run' ? 'running…' : toolSub(item.detail),
        t: '',
        tgt,
        badge: item.badge,
        live: item.status === 'run',
      }
    case 'approval':
      return {
        k: 'write',
        title: item.title,
        resolved: item.state !== 'waiting',
        sub:
          item.state === 'waiting'
            ? 'write feeler · waiting on kalynnka'
            : item.state === 'approved'
              ? 'approved by kalynnka — dispatched'
              : 'dismissed by kalynnka — dropped',
        t: item.resolvedT ?? '',
        tgt,
      }
    case 'ask':
      return {
        k: 'ask',
        title: item.title,
        resolved: item.state === 'answered',
        sub:
          item.state === 'answered'
            ? `answered — ${item.answer ?? ''}`
            : `ask feeler · ${item.options.length} options · waiting`,
        t: item.resolvedT ?? '',
        tgt,
      }
    case 'sub':
      return { k: 'sub', title: `${item.id} · ${item.route}`, sub: item.note, t: '', tgt }
    case 'file':
      return { k: 'card', title: item.name, sub: `${item.rev} · ${item.note}`, t: '', tgt, file: item.name }
    case 'end':
      return { k: 'end', title: 'end of turn', sub: item.label.replace(/^End of turn · /, ''), t: '', tgt }
    case 'stream':
      return {
        k: 'msg',
        title: agent || 'claude',
        sub: item.streaming ? 'reply streaming…' : item.text,
        t: '',
        tgt,
      }
    default:
      return null
  }
}

const skeletonBars = [
  { w: '52%', delay: '0s' },
  { w: '34%', delay: '.4s' },
  { w: '61%', delay: '.8s' },
]

/** The right-hand timeline rail — per-session event index of the thread. */
export function TimelinePanel() {
  const selThreadId = useConsole((s) => s.selThreadId)
  const detail = useConsole((s) => s.detail)
  const live = useConsole((s) => s.live)
  const tlFold = useConsole((s) => s.tlFold)
  const ntOn = useConsole((s) => s.ntOn)
  const ntStarted = useConsole((s) => s.ntStarted)
  const traceOn = useConsole((s) => s.traceOn)
  const mgmtSec = useConsole((s) => s.mgmtSec)
  const pvOpen = useConsole((s) => s.pvOpen)
  const traceW = useConsole((s) => s.widths.trace)
  const railDrag = useConsole((s) => s.railDrag)
  const { toggleTimelineFold, openFile } = useConsole((s) => s.actions)
  const dragTrace = useRailDrag('trace', 'trk-trace-panel', 200, 480, true)

  const show = (traceOn ?? true) && !mgmtSec && !pvOpen
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntModel = useConsole((s) => s.ntModel)
  const ntEffort = useConsole((s) => s.ntEffort)
  // A booted new thread indexes its live session; before boot the skeleton shows.
  const sessions = ntOn
    ? ntStarted
      ? [
          {
            n: '01',
            id: 'SES-0533',
            route: `${ntAgent} · ${ntModel}`,
            kind: 'entry' as const,
            t: '',
            reason: `entry route — trunkline console · effort ${ntEffort}`,
            status: 'active',
            tone: 'accent' as const,
          },
        ]
      : []
    : (detail?.sessions ?? [])

  const buckets: TlEvent[][] = sessions.map(() => [])
  if (detail && buckets.length) {
    let si = 0
    let opens = 0
    for (const item of detail.ledger) {
      const isOpen = item.kind === 'session-open'
      if (isOpen) opens++
      const next = sessions[si + 1]
      if (next && (next.anchor === item.uid || (isOpen && opens > 1))) {
        si++
        if (isOpen && item.tone === 'summon') {
          const from = sessions[si - 1].route.split(' ')[0]
          buckets[si].push({
            k: 'turn',
            title: `brief · from ${from}`,
            sub: 'findings so far + plan · thread ledger shared',
            t: sessions[si].t,
            tgt: `pm-${item.uid}`,
          })
        }
      }
      if (isOpen) continue
      const e = eventOf(item, sessions[si]?.route.split(' ')[0])
      if (e) buckets[si].push(e)
    }
  }
  if (buckets.length) {
    const agent = sessions[sessions.length - 1]?.route.split(' ')[0]
    for (const item of live) {
      const e = eventOf(item, agent)
      if (e) buckets[buckets.length - 1].push(e)
    }
  }
  const eventCount = buckets.reduce((n, b) => n + b.length, 0)

  return (
    <aside
      id="trk-trace-panel"
      className="trk-rail"
      data-folded={show ? undefined : ''}
      data-dragging={railDrag === 'trace' ? 'true' : undefined}
      style={{
        width: traceW ? `${traceW}px` : 'clamp(240px,25%,330px)',
        flexShrink: 0,
        backgroundColor: 'var(--card-bg)',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        position: 'relative',
        boxShadow: 'inset 1px 0 0 var(--line-divider)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '0 14px',
          height: 44,
          boxSizing: 'border-box',
          borderBottom: '1px solid var(--line-divider)',
          flexShrink: 0,
        }}
      >
        <span style={{ ...label(10, '.22em'), color: 'var(--fg-1)' }}>Timeline</span>
        <span style={{ ...label(8, '.14em'), color: 'var(--fg-3)' }}>of this thread</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...mono(8.5, 700), color: 'var(--color-accent)' }}>{detail?.key ?? selThreadId}</span>
      </div>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 7,
          padding: '7px 14px',
          borderBottom: '1px solid var(--line-divider)',
          flexShrink: 0,
          ...mono(8),
          color: 'var(--fg-3)',
        }}
      >
        <span style={{ ...ellipsis, minWidth: 0 }}>
          key: {ntOn ? 'trunkline / direct / console / —' : (detail?.key ?? '—')}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ whiteSpace: 'nowrap', flexShrink: 0 }}>
          {sessions.length} ses · {eventCount} events
        </span>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', minHeight: 0, padding: '0 0 12px' }}>
        {sessions.map((ses, si) => {
          const kColor = KIND_COLOR[ses.kind]
          const events = buckets[si]
          const folded = !!tlFold[ses.id]
          return (
            <Fragment key={ses.id}>
              <div
                onClick={() => goLedgerTarget(ses.anchor)}
                title="Jump to session start"
                className="hov-wash"
                style={{
                  position: 'sticky',
                  top: 0,
                  zIndex: 3,
                  padding: '8px 12px 7px 14px',
                  background: 'color-mix(in srgb, var(--color-ink) 6%, var(--card-bg))',
                  borderBottom: '1px solid var(--line-divider)',
                  cursor: 'pointer',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                  <span
                    onClick={(e) => {
                      e.stopPropagation()
                      toggleTimelineFold(ses.id)
                    }}
                    title="Fold / unfold session"
                    className="hov-accent"
                    style={{ color: 'var(--fg-3)', cursor: 'pointer', flexShrink: 0 }}
                  >
                    <Disclose open={!folded} style={{ ...mono(10), width: 11, textAlign: 'center' }} />
                  </span>
                  <span
                    style={{
                      width: 8,
                      height: 8,
                      boxSizing: 'border-box',
                      border: `2px solid ${kColor}`,
                      background: 'var(--card-bg)',
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ ...mono(10, 700), color: 'var(--color-accent)', flexShrink: 0 }}>{ses.id}</span>
                  <span style={{ ...mono(8.5), color: 'var(--fg-2)', flex: 1, minWidth: 0, ...ellipsis }}>
                    {ses.route}
                  </span>
                  <span
                    style={{
                      ...chipLabel,
                      color: kColor,
                      border: `1px solid ${kColor}`,
                      padding: '1px 4px',
                      flexShrink: 0,
                    }}
                  >
                    {ses.kind}
                  </span>
                  <span style={{ ...label(7.5, '.12em'), color: TONE_COLOR[ses.tone], flexShrink: 0 }}>
                    ● {ses.status}
                  </span>
                </div>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, marginTop: 3, paddingLeft: 33 }}>
                  <span
                    style={{
                      ...serif(11),
                      color: 'var(--fg-3)',
                      fontStyle: 'italic',
                      lineHeight: 1.45,
                      flex: 1,
                      minWidth: 0,
                    }}
                  >
                    {ses.reason}
                  </span>
                  <span style={{ ...mono(8), color: 'var(--fg-3)', flexShrink: 0 }}>{ses.t}</span>
                </div>
              </div>
              <div style={{ borderBottom: '1px solid var(--line-divider)' }}>
                <Fold open={!folded}>
                  <div style={{ position: 'relative', padding: '5px 0 7px' }}>
                    <span
                      style={{
                        position: 'absolute',
                        left: 20,
                        top: 0,
                        bottom: 0,
                        width: 1,
                        background: 'var(--line-color)',
                      }}
                    />
                    {ses.hop && (
                      <div
                        style={{
                          position: 'relative',
                          display: 'flex',
                          alignItems: 'flex-start',
                          gap: 8,
                          padding: '4px 12px 4px 14px',
                        }}
                      >
                        <span
                          style={{
                            position: 'relative',
                            zIndex: 1,
                            width: 13,
                            display: 'flex',
                            justifyContent: 'center',
                            flexShrink: 0,
                            paddingTop: 3,
                          }}
                        >
                          <i
                            style={{
                              width: 7,
                              height: 7,
                              boxSizing: 'border-box',
                              background: 'var(--card-bg)',
                              border: '1px dashed var(--color-teal)',
                            }}
                          />
                        </span>
                        <span
                          style={{ flex: 1, minWidth: 0, ...mono(8, 700), color: 'var(--color-teal)', lineHeight: 1.5 }}
                        >
                          {ses.hop}
                        </span>
                      </div>
                    )}
                    {events.map((ev, ei) => {
                      const ks = KS[ev.k]
                      const file = ev.file
                      const resolved = (ev.k === 'write' || ev.k === 'ask') && ev.resolved
                      const fill = resolved ? 'var(--color-sage)' : ks.fill
                      const bd = resolved ? 'var(--color-sage)' : ks.bd
                      const tagColor = ev.live
                        ? 'var(--color-accent)'
                        : resolved
                          ? 'var(--color-sage)'
                          : ks.tagColor
                      const badgeColor =
                        ev.badge?.tone === 'terra' ? 'var(--color-terra)' : 'var(--color-gold)'
                      return (
                        <div
                          key={`${ev.tgt}-${ei}`}
                          onClick={() => goLedgerTarget(ev.tgt)}
                          title="Jump to this moment in the ledger"
                          className="hov-wash"
                          style={{
                            position: 'relative',
                            display: 'flex',
                            alignItems: 'flex-start',
                            gap: 8,
                            padding: `4px 12px 4px ${ev.k === 'sub' ? '32px' : '14px'}`,
                            cursor: 'pointer',
                          }}
                        >
                          <span
                            style={{
                              position: 'relative',
                              zIndex: 1,
                              width: 13,
                              display: 'flex',
                              justifyContent: 'center',
                              flexShrink: 0,
                              paddingTop: 4,
                            }}
                          >
                            {ev.k === 'sub' ? (
                              <span
                                style={{
                                  ...mono(10, 700),
                                  lineHeight: '8px',
                                  color: 'var(--color-teal)',
                                  transform: 'scaleX(-1)',
                                }}
                              >
                                ↵
                              </span>
                            ) : (
                              <i
                                style={{
                                  width: 7,
                                  height: 7,
                                  boxSizing: 'border-box',
                                  borderRadius: ks.rad,
                                  background: fill,
                                  borderWidth: 1,
                                  borderColor: bd,
                                  borderStyle: ks.bs,
                                }}
                              />
                            )}
                          </span>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                              <span style={{ width: 27, flexShrink: 0, ...label(6.5, '.14em'), color: tagColor }}>
                                {ks.tag}
                              </span>
                              <span style={{ ...mono(9, 700), color: ks.titleColor, ...ellipsis, minWidth: 0 }}>
                                {ev.title}
                              </span>
                              {ev.badge && (
                                <span
                                  style={{
                                    ...label(6.5, '.08em'),
                                    color: badgeColor,
                                    border: `1px solid ${badgeColor}`,
                                    padding: '0 3px',
                                    flexShrink: 0,
                                  }}
                                >
                                  {ev.badge.label}
                                </span>
                              )}
                              <span style={{ flex: 1 }} />
                              <span style={{ ...mono(7.5), color: 'var(--fg-3)', flexShrink: 0 }}>{ev.t}</span>
                            </div>
                            {ev.sub && (
                              <div
                                style={{
                                  marginTop: 1,
                                  paddingLeft: 33,
                                  ...mono(8),
                                  color: 'var(--fg-3)',
                                  lineHeight: 1.5,
                                  ...ellipsis,
                                }}
                              >
                                {ev.sub}
                              </div>
                            )}
                          </div>
                          {file && (
                            <span
                              onClick={(e) => {
                                e.stopPropagation()
                                openFile(file)
                              }}
                              title="Open in review panel"
                              className="hov-border-accent"
                              style={{
                                position: 'relative',
                                zIndex: 1,
                                ...mono(9, 700),
                                color: 'var(--color-accent)',
                                flexShrink: 0,
                                padding: '1px 4px',
                                border: '1px solid transparent',
                              }}
                            >
                              →
                            </span>
                          )}
                        </div>
                      )
                    })}
                    {events.length === 0 && (
                      <div
                        style={{
                          position: 'relative',
                          padding: '3px 12px 3px 35px',
                          ...serif(11),
                          fontStyle: 'italic',
                          color: 'var(--fg-3)',
                          lineHeight: 1.5,
                        }}
                      >
                        no events indexed for this session
                      </div>
                    )}
                  </div>
                </Fold>
                {folded && (
                  <div
                    onClick={() => toggleTimelineFold(ses.id)}
                    className="hov-accent"
                    style={{ padding: '4px 12px 5px 33px', ...microSection, color: 'var(--fg-3)', cursor: 'pointer' }}
                  >
                    {events.length} events · folded
                  </div>
                )}
              </div>
            </Fragment>
          )
        })}
        {ntOn && !ntStarted && (
          <div className="lt-fade-in">
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 7,
                padding: '9px 12px 8px 14px',
                borderBottom: '1px solid var(--line-divider)',
                background: 'var(--trk-wash)',
              }}
            >
              <span style={{ width: 11, flexShrink: 0 }} />
              <span
                style={{
                  width: 8,
                  height: 8,
                  boxSizing: 'border-box',
                  border: '2px dashed var(--fg-3)',
                  opacity: 0.55,
                  flexShrink: 0,
                }}
              />
              <span style={{ ...mono(10, 700), color: 'var(--fg-3)', letterSpacing: '.04em' }}>SES-····</span>
              <span style={{ flex: 1 }} />
              <span
                style={{
                  ...chipLabel,
                  color: 'var(--fg-3)',
                  border: '1px dashed var(--line-divider)',
                  padding: '1px 4px',
                  flexShrink: 0,
                }}
              >
                entry
              </span>
              <span style={{ ...label(7.5, '.12em'), color: 'var(--fg-3)', flexShrink: 0 }}>○ pending</span>
            </div>
            <div style={{ position: 'relative', padding: '7px 0 9px' }}>
              <span
                style={{
                  position: 'absolute',
                  left: 20,
                  top: 3,
                  bottom: 3,
                  width: 1,
                  background: 'repeating-linear-gradient(180deg, var(--trk-vline) 0 4px, transparent 4px 8px)',
                }}
              />
              {skeletonBars.map((b, i) => (
                <div
                  key={i}
                  style={{
                    position: 'relative',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '5px 12px 5px 14px',
                    animation: `trkPulse 2.4s ease-in-out ${b.delay} infinite`,
                  }}
                >
                  <span
                    style={{
                      position: 'relative',
                      zIndex: 1,
                      width: 13,
                      display: 'flex',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <i
                      style={{
                        width: 7,
                        height: 7,
                        boxSizing: 'border-box',
                        background: 'var(--card-bg)',
                        border: '1px dashed var(--fg-3)',
                      }}
                    />
                  </span>
                  <span style={{ width: 27, height: 5, background: 'var(--surface-sunken)', flexShrink: 0 }} />
                  <span style={{ width: b.w, height: 5, background: 'var(--surface-sunken)' }} />
                  <span style={{ flex: 1 }} />
                  <span style={{ width: 26, height: 5, background: 'var(--surface-sunken)', flexShrink: 0 }} />
                </div>
              ))}
            </div>
            <div
              style={{
                padding: '4px 14px 0 14px',
                ...serif(11),
                fontStyle: 'italic',
                color: 'var(--fg-3)',
                lineHeight: 1.6,
              }}
            >
              events index here once the first directive boots a session — sends, tools, files, approvals.
            </div>
          </div>
        )}
      </div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          height: 30,
          boxSizing: 'border-box',
          flexShrink: 0,
          borderTop: '1px solid var(--line-divider)',
          padding: '0 14px',
        }}
      >
        <span style={{ ...mono(8, 700), letterSpacing: '.1em', color: 'var(--fg-1)', whiteSpace: 'nowrap' }}>
          {eventCount} events indexed
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ ...mono(7.5), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>// end of timeline</span>
      </div>
      <span
        onMouseDown={dragTrace}
        title="Drag to resize"
        className="hov-resize"
        style={{ position: 'absolute', top: 0, left: 0, bottom: 0, width: 6, cursor: 'col-resize', zIndex: 40 }}
      />
    </aside>
  )
}
