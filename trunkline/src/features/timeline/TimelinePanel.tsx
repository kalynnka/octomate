import { Fragment, useRef } from 'react'
import { useAuth } from '@/state/auth'
import { goLedgerTarget, syncRails, useConsole } from '@/state/console'
import { useRailDrag } from '@/lib/useRailDrag'
import { chipLabel, ellipsis, label, microSection, mono, serif } from '@/components/text'
import { Disclose, Fold } from '@/components/Fold'
import { answerText } from '@/lib/api/fold'
import type {
  LedgerItem,
  SessionInfo,
  SessionKind,
  SessionTone,
  ToolDetail,
} from '@/lib/api/types'

type TlKind = 'turn' | 'msg' | 'think' | 'plan' | 'tool' | 'write' | 'ask' | 'sub' | 'card' | 'end' | 'wait'

interface TlEvent {
  k: TlKind
  title: string
  sub: string
  t: string
  /** the ledger card this row indexes — what the two rails sync on */
  uid: string
  /** ledger DOM id the row jumps to */
  tgt: string
  /** file name when the event opens the review panel */
  file?: string
  badge?: { label: string; tone: 'gold' | 'terra' }
  /** a still-running tool — dot and tag light accent */
  live?: boolean
  /** resolved feeler — dot and tag turn sage */
  resolved?: boolean
  /** a turn that is the summoning agent's brief rather than a user directive */
  chip?: 'brief'
}

/** A turn and the messages it drew; a run with no head holds what came before
 *  the conversation's first turn. */
interface TlRun {
  head?: TlEvent
  events: TlEvent[]
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
  wait: { tag: 'next', tagColor: 'var(--color-accent)', rad: '0', fill: 'var(--card-bg)', bd: 'var(--color-accent)', bs: 'dashed', titleColor: 'var(--fg-3)' },
}

/** Conversation-header tick colour per kind; a kind not listed ticks in --fg-1. */
const TICK: Partial<Record<TlKind, string>> = {
  turn: 'var(--color-teal)',
  msg: 'var(--fg-1)',
  think: 'var(--fg-3)',
  plan: 'var(--fg-3)',
  tool: 'var(--color-accent)',
  write: 'var(--color-gold)',
  ask: 'var(--color-gold)',
  sub: 'var(--color-teal)',
  card: 'var(--color-accent)',
}

/** Trailing row of the live run — not a ledger card, so it neither jumps nor counts. */
const WAIT_ROW: TlEvent = { k: 'wait', title: 'awaiting next message…', sub: '', t: '', uid: '', tgt: '' }

const DASH_V = 'repeating-linear-gradient(180deg, var(--trk-vline) 0 3px, transparent 3px 6px)'
const DASH_H = 'repeating-linear-gradient(90deg, var(--trk-vline) 0 2px, transparent 2px 4px)'
const FOLD_MOTION = 'opacity var(--motion-base) var(--ease-out), transform var(--motion-base) var(--ease-out)'
const COVER_STEP = 60
const COVER_WIDTHS = ['58%', '44%', '66%', '50%', '38%']

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

/** Map one ledger/live item to a timeline event; null = not indexed. The uid is
 *  the item's own, and the caller stamps it. `operator` is who the thread's
 *  feelers wait on — the signed-in account, the thread being theirs alone. */
function eventOf(item: LedgerItem, operator: string, agent?: string): Omit<TlEvent, 'uid'> | null {
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
            ? `${item.tool} · waiting on ${operator}`
            : item.state === 'approved'
              ? `approved by ${operator} — ${item.tool} dispatched`
              : `dismissed by ${operator} — ${item.tool} dropped`,
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
            ? `answered — ${item.questions.map((q) => (q.answer === undefined ? '' : answerText(q.answer))).join(' · ')}`
            : `ask feeler · ${item.questions.length === 1 ? `${item.questions[0].options.length} options` : `${item.questions.length} questions`} · waiting`,
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
  { w: '52%', delay: '.4s' },
  { w: '34%', delay: '.8s' },
  { w: '61%', delay: '1.2s' },
]

/** Load-in skeleton over a row from `left` rightward: its bars draw in, then
 *  the cover fades to reveal the row beneath. */
function Cover({ left, delay, bars }: { left: number; delay: number; bars: [top: number, width: string][] }) {
  return (
    <span
      data-trk-cover=""
      style={{
        position: 'absolute',
        zIndex: 2,
        top: 0,
        bottom: 0,
        left,
        right: 0,
        background: 'var(--card-bg)',
        pointerEvents: 'none',
        animation: `trkCover 640ms var(--ease-out) ${delay}ms both`,
      }}
    >
      {bars.map(([top, width]) => (
        <i
          key={top}
          style={{
            position: 'absolute',
            left: 0,
            top,
            height: 5,
            width,
            background: 'var(--skeleton-bed)',
            animation: `trkBar 640ms var(--ease-out) ${delay}ms both`,
          }}
        />
      ))}
    </span>
  )
}

/** The right-hand timeline rail — per-session event index of the thread. */
export function TimelinePanel() {
  const selThreadId = useConsole((s) => s.selThreadId)
  const detail = useConsole((s) => s.detail)
  const live = useConsole((s) => s.live)
  const running = useConsole((s) => s.running)
  const tlFold = useConsole((s) => s.tlFold)
  const ledgerN = useConsole((s) => s.ledgerN)
  const ntOn = useConsole((s) => s.ntOn)
  const ntStarted = useConsole((s) => s.ntStarted)
  const traceOn = useConsole((s) => s.traceOn)
  const mgmtSec = useConsole((s) => s.mgmtSec)
  const pvOpen = useConsole((s) => s.pvOpen)
  const traceW = useConsole((s) => s.widths.trace)
  const railDrag = useConsole((s) => s.railDrag)
  const { toggleTimelineFold, openFile } = useConsole((s) => s.actions)
  const operator = useAuth((s) => s.user?.username ?? 'operator')
  const dragTrace = useRailDrag('trace', 'trk-trace-panel', 240, 480, true)
  const opened = useRef({ thread: selThreadId, at: Date.now() })
  if (opened.current.thread !== selThreadId) opened.current = { thread: selThreadId, at: Date.now() }
  // For two seconds after a thread opens its rows draw in one after another; a
  // row arriving later, such as a live message, draws in at once.
  const stagger = Date.now() - opened.current.at < 2000 ? COVER_STEP : 0

  const show = (traceOn ?? true) && !mgmtSec && !pvOpen
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntModel = useConsole((s) => s.ntModel)
  const ntEffort = useConsole((s) => s.ntEffort)
  const ntPermissionMode = useConsole((s) => s.ntPermissionMode)
  // A booted new thread indexes its live session; before boot the skeleton shows.
  // Its conversation row is the relay's and unknown here until the thread is
  // reopened, so the id stays empty and the posture is the one the console holds.
  const sessions: SessionInfo[] = ntOn
    ? ntStarted
      ? [
          {
            n: '01',
            id: '',
            conversationId: '',
            route: `${ntAgent} · ${ntModel}`,
            agent: ntAgent,
            model: null,
            effort: null,
            mode: ntPermissionMode,
            kind: 'entry',
            t: '',
            reason: `entry route — trunkline console · effort ${ntEffort}`,
            status: 'active',
            tone: 'accent',
          },
        ]
      : []
    : (detail?.sessions ?? [])

  const buckets: TlEvent[][] = sessions.map(() => [])
  let indexed = 0
  if (detail && buckets.length) {
    // The index follows the chat's window. The chat renders the ledger's last
    // `ledgerN` cards and pages back from there, so a row outside that window
    // points at a card that is not in the document — nothing to jump to, and
    // nothing for the rails to line up on. A thread of a thousand turns then
    // costs a page of rows rather than a thousand of them.
    const page = Math.max(0, detail.ledger.length - ledgerN)
    let si = 0
    detail.ledger.forEach((item, index) => {
      const isOpen = item.kind === 'session-open'
      // A session starts at its own first card, whatever kind that is: the claim
      // that opened it, or — for a session nothing claimed — its first turn. The
      // walk covers the whole ledger, so a window opening mid-thread still knows
      // which session it opened in.
      const opening = sessions[si + 1]?.anchor === item.uid
      if (opening) si++
      const e = isOpen ? null : eventOf(item, operator, sessions[si]?.route.split(' ')[0])
      // A turn heads its run rather than being one of its messages.
      if (e && e.k !== 'turn') indexed++
      if (index < page) return
      if (opening && isOpen && item.tone === 'summon') {
        const from = sessions[si - 1].route.split(' ')[0]
        buckets[si].push({
          k: 'turn',
          title: from,
          chip: 'brief',
          sub: 'findings so far + plan · thread ledger shared',
          t: sessions[si].t,
          uid: item.uid,
          tgt: `pm-${item.uid}`,
        })
      }
      if (e) buckets[si].push({ ...e, uid: item.uid })
    })
  }
  if (buckets.length) {
    const agent = sessions[sessions.length - 1]?.route.split(' ')[0]
    for (const item of live) {
      const e = eventOf(item, operator, agent)
      if (e) {
        buckets[buckets.length - 1].push({ ...e, uid: item.uid })
        if (e.k !== 'turn') indexed++
      }
    }
  }
  const eventCount = buckets.reduce((n, b) => n + b.filter((ev) => ev.k !== 'turn').length, 0)
  // What the window holds, over what the thread has, when they differ — an index
  // that silently showed 40 of a thousand would read as a short thread.
  const eventTally = eventCount === indexed ? `${eventCount}` : `${eventCount}/${indexed}`
  const runsBySession = buckets.map((events) => {
    const runs: TlRun[] = []
    for (const ev of events) {
      if (ev.k === 'turn') runs.push({ head: ev, events: [] })
      else if (runs.length) runs[runs.length - 1].events.push(ev)
      else runs.push({ events: [ev] })
    }
    return runs
  })
  const runCount = runsBySession.reduce((n, runs) => n + runs.length, 0)
  // Every cover draws in one step after the one above it, so `cover` counts
  // them in render order: a conversation's header, then each run head and its rows.
  let cover = 0

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
        <span style={{ ...label(10, '.22em'), color: 'var(--fg-1)', whiteSpace: 'nowrap', flexShrink: 0 }}>Timeline</span>
        <span style={{ ...label(8, '.14em'), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>of this thread</span>
        <span style={{ flex: 1 }} />
        {/* The full key sits in the row below; this one truncates when squashed. */}
        <span style={{ ...mono(8.5, 700), color: 'var(--color-accent)', ...ellipsis, minWidth: 0 }}>{detail?.key ?? selThreadId}</span>
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
          {sessions.length} cnv · {runCount} runs · {eventTally} msg
        </span>
      </div>
      <div
        id="trk-timeline"
        onScroll={() => syncRails('timeline')}
        // Folding a conversation can end the overflow; a reserved gutter keeps
        // the rows from widening as the scrollbar goes and narrowing as it returns.
        style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', scrollbarGutter: 'stable', minHeight: 0, padding: '0 0 12px' }}
      >
        <div style={{ paddingTop: 4 }}>
          {sessions.map((ses, si) => {
            const kColor = KIND_COLOR[ses.kind]
            const runs = runsBySession[si]
            const folded = !!tlFold[ses.id]
            const msgCount = runs.reduce((n, run) => n + run.events.length, 0)
            return (
              <Fragment key={ses.id}>
                {/* The sticky header needs an opaque bed; the hover wash rides on top of it. */}
                <div style={{ position: 'sticky', top: 0, zIndex: 3, background: 'var(--card-bg)' }}>
                  <div
                    onClick={() => toggleTimelineFold(ses.id)}
                    title="Fold / unfold conversation"
                    className="hov-wash"
                    style={{ position: 'relative', padding: '8px 10px 8px 28px', cursor: 'pointer' }}
                  >
                    <i
                      style={{
                        position: 'absolute',
                        left: 10,
                        top: 11,
                        width: 9,
                        height: 9,
                        boxSizing: 'border-box',
                        border: `2px solid ${kColor}`,
                        background: 'var(--card-bg)',
                      }}
                    />
                    <Cover
                      left={28}
                      delay={stagger * cover++}
                      bars={[
                        [9, '48%'],
                        [24, '72%'],
                        [41, '36%'],
                      ]}
                    />
                    <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                      <Disclose
                        open={!folded}
                        style={{ ...mono(10), color: 'var(--fg-3)', width: 11, textAlign: 'center', flexShrink: 0 }}
                      />
                      <span title={ses.name ?? ses.id} style={{ ...mono(10, 700), color: 'var(--color-accent)', minWidth: 0, ...ellipsis }}>
                        {ses.name ?? ses.id}
                      </span>
                      <span style={{ ...mono(8.5), color: 'var(--fg-2)', flex: 1, minWidth: 0, ...ellipsis }}>
                        {ses.route}
                      </span>
                      <span
                        style={{
                          ...chipLabel,
                          color: kColor,
                          border: `1px solid ${kColor}`,
                          padding: '1px 4px',
                          minWidth: 0,
                          maxWidth: '25%',
                          ...ellipsis,
                        }}
                      >
                        {ses.kind}
                      </span>
                      <span style={{ ...label(7.5, '.12em'), color: TONE_COLOR[ses.tone], minWidth: 0, maxWidth: '25%', ...ellipsis }}>
                        ● {ses.status}
                      </span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, marginTop: 3, paddingLeft: 18 }}>
                      <span
                        style={{
                          ...serif(11),
                          color: 'var(--fg-3)',
                          fontStyle: 'italic',
                          lineHeight: 1.45,
                          flex: 1,
                          minWidth: 0,
                          overflowWrap: 'anywhere',
                          display: '-webkit-box',
                          WebkitBoxOrient: 'vertical',
                          WebkitLineClamp: 2,
                          overflow: 'hidden',
                        }}
                      >
                        {ses.reason}
                      </span>
                      <span style={{ ...mono(8), color: 'var(--fg-3)', flexShrink: 0 }}>{ses.t}</span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 7, paddingLeft: 18 }}>
                      <span style={{ ...mono(7.5), color: 'var(--fg-3)', flex: '1 0 auto', whiteSpace: 'nowrap' }}>
                        {runs.length} {runs.length === 1 ? 'run' : 'runs'} · {msgCount} msg
                      </span>
                      {/* A long conversation's strip outgrows the rail; it gives up its oldest runs first. */}
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'flex-end',
                          gap: 4,
                          minWidth: 0,
                          overflow: 'hidden',
                        }}
                      >
                        {runs.map(
                          (run, ri) =>
                            run.events.length > 0 && (
                              <span
                                key={ri}
                                title={`R${String(ri + 1).padStart(2, '0')}`}
                                style={{
                                  display: 'flex',
                                  alignItems: 'flex-end',
                                  gap: 2,
                                  flexShrink: 0,
                                  paddingLeft: 4,
                                  borderLeft: '1px solid var(--line-divider)',
                                }}
                              >
                                {run.events.map((ev, ei) => (
                                  <i
                                    key={ei}
                                    style={{
                                      display: 'block',
                                      width: 3,
                                      height: ev.k === 'sub' ? 9 : ev.k === 'msg' ? 7 : 5,
                                      background: ev.live ? 'var(--color-accent)' : (TICK[ev.k] ?? 'var(--fg-1)'),
                                    }}
                                  />
                                ))}
                              </span>
                            ),
                        )}
                      </div>
                    </div>
                  </div>
                </div>
                <Fold open={!folded}>
                  <div style={{ padding: '2px 0 8px' }}>
                    {runs.map((run, ri) => {
                      const key = `${ses.id}/${ri}`
                      const runFolded = !!tlFold[key]
                      const isLive = running && si === sessions.length - 1 && ri === runs.length - 1
                      const rows = isLive ? [...run.events, WAIT_ROW] : run.events
                      const chip = run.head ? run.head.chip : ses.hop ? 'brief' : 'open'
                      // Only a user's own turn heads its run without a chip.
                      const user = chip === undefined
                      return (
                        <Fragment key={ri}>
                          <div
                            data-uid={run.head?.uid}
                            onClick={() => toggleTimelineFold(key)}
                            title="Fold / unfold run"
                            className="hov-wash"
                            style={{
                              position: 'relative',
                              padding: '6px 12px 6px 48px',
                              cursor: 'pointer',
                              opacity: folded ? 0 : 1,
                              transform: folded ? 'translateY(-4px)' : 'none',
                              transition: FOLD_MOTION,
                              transitionDelay: `${40 + ri * 50}ms`,
                            }}
                          >
                            {!runFolded && rows.length > 0 && (
                              <span style={{ position: 'absolute', left: 34, top: 13, bottom: 0, width: 1, background: DASH_V }} />
                            )}
                            <i
                              style={{
                                position: 'absolute',
                                left: 30,
                                top: 9,
                                width: 9,
                                height: 9,
                                boxSizing: 'border-box',
                                borderRadius: user ? 9999 : 0,
                                background: user ? 'var(--color-teal)' : 'var(--card-bg)',
                                border: `1.5px solid ${user ? 'var(--color-teal)' : 'var(--color-terra)'}`,
                              }}
                            />
                            <Cover
                              left={48}
                              delay={stagger * cover++}
                              bars={[
                                [7, '42%'],
                                [21, '74%'],
                              ]}
                            />
                            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                              <span
                                style={{
                                  ...mono(7.5, 700),
                                  letterSpacing: '.06em',
                                  color: isLive ? 'var(--color-accent)' : 'var(--fg-3)',
                                  flexShrink: 0,
                                }}
                              >
                                R{String(ri + 1).padStart(2, '0')}
                              </span>
                              <span style={{ ...mono(9.5, 700), color: 'var(--fg-1)', ...ellipsis, minWidth: 0 }}>
                                {run.head?.title ?? (ses.hop ? 'brief' : 'agent')}
                              </span>
                              {chip && (
                                <span
                                  style={{
                                    ...label(6.5, '.1em'),
                                    color: 'var(--color-terra)',
                                    border: '1px solid var(--color-terra)',
                                    padding: '0 3px',
                                    flexShrink: 0,
                                  }}
                                >
                                  {chip}
                                </span>
                              )}
                              <span style={{ flex: 1 }} />
                              {isLive && (
                                <span style={{ ...mono(7.5, 700), color: 'var(--color-accent)', flexShrink: 0 }}>● live</span>
                              )}
                              <span style={{ ...mono(7.5), color: 'var(--fg-3)', flexShrink: 0 }}>{run.head?.t ?? ses.t}</span>
                              <Disclose
                                open={!runFolded}
                                style={{ ...mono(9), color: 'var(--fg-3)', width: 9, textAlign: 'center', flexShrink: 0 }}
                              />
                            </div>
                            <div
                              style={{
                                marginTop: 2,
                                ...serif(11.5),
                                fontStyle: 'italic',
                                lineHeight: 1.45,
                                color: 'var(--fg-2)',
                                display: '-webkit-box',
                                WebkitBoxOrient: 'vertical',
                                WebkitLineClamp: 2,
                                overflow: 'hidden',
                              }}
                            >
                              {run.head?.sub ?? (ses.hop || ses.reason)}
                            </div>
                            {runFolded && (
                              <div className="lt-fade-in" style={{ marginTop: 3, ...microSection, color: 'var(--fg-3)' }}>
                                {run.events.length} msg · folded
                              </div>
                            )}
                          </div>
                          <Fold open={!runFolded}>
                            {rows.map((ev, ei) => {
                              const ks = KS[ev.k]
                              const file = ev.file
                              const wait = ev.k === 'wait'
                              const resolved = (ev.k === 'write' || ev.k === 'ask') && ev.resolved
                              const tone = ev.live ? 'var(--color-accent)' : resolved ? 'var(--color-sage)' : undefined
                              const badgeColor =
                                ev.badge?.tone === 'terra' ? 'var(--color-terra)' : 'var(--color-gold)'
                              return (
                                <div
                                  key={`${ev.tgt}-${ei}`}
                                  data-uid={wait ? undefined : ev.uid}
                                  onClick={wait ? undefined : () => goLedgerTarget(ev.tgt)}
                                  title={
                                    wait
                                      ? 'Waiting for the next message from the live run'
                                      : 'Jump to this moment in the ledger'
                                  }
                                  className="hov-wash"
                                  style={{
                                    position: 'relative',
                                    padding: '4px 12px 4px 60px',
                                    cursor: wait ? undefined : 'pointer',
                                    animation: wait ? 'trkWait 1.6s ease-in-out infinite' : undefined,
                                    opacity: runFolded ? 0 : 1,
                                    transform: runFolded ? 'translateY(-4px)' : 'none',
                                    transition: FOLD_MOTION,
                                    transitionDelay: runFolded ? '0ms' : `${40 + ei * 28}ms`,
                                  }}
                                >
                                  <span
                                    style={{
                                      position: 'absolute',
                                      left: 34,
                                      top: 0,
                                      bottom: ei === rows.length - 1 ? 'calc(100% - 11px)' : 0,
                                      width: 1,
                                      background: DASH_V,
                                    }}
                                  />
                                  <span style={{ position: 'absolute', left: 34, top: 10, width: 13, height: 1, background: DASH_H }} />
                                  <i
                                    style={{
                                      position: 'absolute',
                                      left: 47,
                                      top: 7,
                                      width: 7,
                                      height: 7,
                                      boxSizing: 'border-box',
                                      borderRadius: ks.rad,
                                      background: tone ?? ks.fill,
                                      borderWidth: 1,
                                      borderColor: tone ?? ks.bd,
                                      borderStyle: ks.bs,
                                    }}
                                  />
                                  <Cover
                                    left={60}
                                    delay={stagger * cover++}
                                    bars={[
                                      [6, COVER_WIDTHS[ei % COVER_WIDTHS.length]],
                                      [17, ev.sub || wait ? COVER_WIDTHS[(ei + 2) % COVER_WIDTHS.length] : '0'],
                                    ]}
                                  />
                                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                                    <span style={{ width: 28, flexShrink: 0, ...label(6.5, '.14em'), color: tone ?? ks.tagColor }}>
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
                                    {file && (
                                      <span
                                        onClick={(e) => {
                                          e.stopPropagation()
                                          openFile(file)
                                        }}
                                        title="Open in review panel"
                                        className="hov-border-accent"
                                        style={{
                                          ...mono(9, 700),
                                          color: 'var(--color-accent)',
                                          flexShrink: 0,
                                          padding: '0 4px',
                                          border: '1px solid transparent',
                                        }}
                                      >
                                        →
                                      </span>
                                    )}
                                  </div>
                                  {wait && (
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 4, paddingLeft: 34 }}>
                                      <span style={{ width: '46%', height: 5, background: 'var(--surface-sunken)' }} />
                                      <span style={{ width: '18%', height: 5, background: 'var(--surface-sunken)' }} />
                                    </div>
                                  )}
                                  {ev.sub && (
                                    <div
                                      style={{
                                        marginTop: 1,
                                        paddingLeft: 34,
                                        ...mono(8),
                                        color: ev.live ? 'var(--color-accent)' : 'var(--fg-3)',
                                        lineHeight: 1.5,
                                        ...ellipsis,
                                      }}
                                    >
                                      {ev.sub}
                                    </div>
                                  )}
                                </div>
                              )
                            })}
                          </Fold>
                        </Fragment>
                      )
                    })}
                    {runs.length === 0 && (
                      <div
                        style={{
                          padding: '3px 12px 3px 46px',
                          ...serif(11),
                          fontStyle: 'italic',
                          color: 'var(--fg-3)',
                          lineHeight: 1.5,
                        }}
                      >
                        no messages indexed for this conversation
                      </div>
                    )}
                  </div>
                </Fold>
              </Fragment>
            )
          })}
        </div>
        {ntOn && !ntStarted && (
          <div className="lt-fade-in">
            <div style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 7, padding: '12px 10px 8px 28px' }}>
              <i
                style={{
                  position: 'absolute',
                  left: 10,
                  top: 15,
                  width: 9,
                  height: 9,
                  boxSizing: 'border-box',
                  border: '2px dashed var(--fg-3)',
                  background: 'var(--card-bg)',
                }}
              />
              <span style={{ width: 11, flexShrink: 0 }} />
              <span style={{ ...mono(10, 700), color: 'var(--fg-3)', letterSpacing: '.04em' }}>CNV-····</span>
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
            <div style={{ padding: '2px 0 8px' }}>
              <div style={{ position: 'relative', padding: '8px 12px 6px 48px', animation: 'trkPulse 2.4s ease-in-out infinite' }}>
                <span
                  style={{
                    position: 'absolute',
                    left: 34,
                    top: 14,
                    bottom: 0,
                    width: 1,
                    background: 'repeating-linear-gradient(180deg, var(--trk-vline) 0 4px, transparent 4px 8px)',
                  }}
                />
                <i
                  style={{
                    position: 'absolute',
                    left: 30,
                    top: 10,
                    width: 9,
                    height: 9,
                    boxSizing: 'border-box',
                    borderRadius: 9999,
                    border: '1.5px dashed var(--fg-3)',
                    background: 'var(--card-bg)',
                  }}
                />
                <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                  <span style={{ width: 16, height: 5, background: 'var(--surface-sunken)', flexShrink: 0 }} />
                  <span style={{ width: '46%', height: 6, background: 'var(--surface-sunken)' }} />
                  <span style={{ flex: 1 }} />
                  <span style={{ width: 26, height: 5, background: 'var(--surface-sunken)', flexShrink: 0 }} />
                </div>
                <span style={{ display: 'block', marginTop: 7, width: '78%', height: 5, background: 'var(--surface-sunken)' }} />
              </div>
              {skeletonBars.map((b, i) => (
                <div
                  key={i}
                  style={{
                    position: 'relative',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '6px 12px 6px 60px',
                    animation: `trkPulse 2.4s ease-in-out ${b.delay} infinite`,
                  }}
                >
                  <span
                    style={{
                      position: 'absolute',
                      left: 34,
                      top: 0,
                      bottom: i === skeletonBars.length - 1 ? '50%' : 0,
                      width: 1,
                      background: 'repeating-linear-gradient(180deg, var(--trk-vline) 0 4px, transparent 4px 8px)',
                    }}
                  />
                  <span style={{ position: 'absolute', left: 34, top: '50%', width: 12, height: 1, background: DASH_H }} />
                  <i
                    style={{
                      position: 'absolute',
                      left: 47,
                      top: 'calc(50% - 3px)',
                      width: 7,
                      height: 7,
                      boxSizing: 'border-box',
                      border: '1px dashed var(--fg-3)',
                      background: 'var(--card-bg)',
                    }}
                  />
                  <span style={{ width: 27, height: 5, background: 'var(--surface-sunken)', flexShrink: 0 }} />
                  <span style={{ width: b.w, height: 5, background: 'var(--surface-sunken)' }} />
                  <span style={{ flex: 1 }} />
                  <span style={{ width: 22, height: 5, background: 'var(--surface-sunken)', flexShrink: 0 }} />
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
              messages index here once the first directive starts a conversation — sends, tools, files, approvals.
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
          {eventTally} messages indexed
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
