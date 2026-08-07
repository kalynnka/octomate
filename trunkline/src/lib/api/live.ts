/**
 * Translation from the live trunkline API payloads (events.ts) to the shapes
 * the console renders (types.ts). One direction only — the console never posts
 * these shapes back.
 */
import type {
  ActionBatchEvent,
  ApiSessionEntry,
  ApiThreadDetail,
  ApiThreadSummary,
} from './events'
import type {
  LedgerItem,
  LedgerItemDraft,
  SessionInfo,
  ThreadDetail,
  ThreadSummary,
} from './types'

/** "deepseek:deepseek-v4-pro" → "deepseek-v4-pro" for tight route chips. */
export function shortModel(model: string): string {
  const cut = model.lastIndexOf(':')
  return cut >= 0 ? model.slice(cut + 1) : model
}

export function routeLabel(agent: string | null, model: string | null): string {
  if (!agent) return 'unrouted'
  return model ? `${agent} · ${shortModel(model)}` : agent
}

function clock(iso: string): string {
  const d = new Date(iso)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export function liveThreadSummary(t: ApiThreadSummary): ThreadSummary {
  return {
    id: t.id,
    channelId: t.channel,
    title: t.title,
    tone: t.status === 'active' ? 'active' : 'idle',
    agentLabel: routeLabel(t.agent, t.model),
    tag: t.thread_key || `#${t.id.slice(0, 6)}`,
  }
}

/** The live all-channel listing, grouped the way the sidebar consumes it. */
export function groupLiveThreads(
  threads: ApiThreadSummary[],
): Record<string, ThreadSummary[]> {
  const grouped: Record<string, ThreadSummary[]> = {}
  for (const t of threads) {
    ;(grouped[t.channel] ??= []).push(liveThreadSummary(t))
  }
  return grouped
}

function liveSession(s: ApiSessionEntry, index: number): SessionInfo {
  return {
    n: `S${index + 1}`,
    id: `SES-${s.id.replaceAll('-', '').slice(-4).toUpperCase()}`,
    route: routeLabel(s.to_agent, s.model),
    kind: index === 0 ? 'entry' : 'summon',
    t: clock(s.created_at),
    reason: s.reason || 'route claimed',
    status: '',
    tone: index === 0 ? 'accent' : 'gold',
  }
}

/**
 * A pending batch as feeler cards, shared by hydration (detail.pending) and
 * the live stream's `action_batch` events. `uid` is filled by the caller.
 */
export function batchFeelers(batch: ActionBatchEvent): LedgerItemDraft[] {
  const items: LedgerItemDraft[] = []
  for (const q of batch.questions) {
    items.push({
      kind: 'ask',
      title: 'Question',
      body: q.args.question,
      options: (q.args.choices ?? []).map((choice) => ({
        label: choice,
        sum: choice,
        desc: '',
      })),
      meta: `${q.tool_name} · answer resumes the run`,
      state: 'waiting',
      batchId: batch.batch_id,
      actionId: q.id,
    })
  }
  for (const a of batch.approvals) {
    items.push({
      kind: 'approval',
      title: a.args.title || 'Permission required',
      desc: a.args.description || JSON.stringify(a.args.args ?? {}),
      meta: `${a.args.tool_name} · approval resumes the run`,
      state: 'waiting',
      batchId: batch.batch_id,
      actionId: a.id,
    })
  }
  return items
}

export function liveThreadDetail(detail: ApiThreadDetail): ThreadDetail {
  let uid = 0
  const next = () => `h${++uid}`

  type Dated = { at: number; item: LedgerItemDraft }
  const dated: Dated[] = []

  detail.sessions.forEach((session, index) => {
    dated.push({
      at: Date.parse(session.created_at),
      item: {
        kind: 'session-open',
        sessionId: `SES-${session.id.replaceAll('-', '').slice(-4).toUpperCase()}`,
        text: routeLabel(session.to_agent, session.model),
        tone: index === 0 ? 'plain' : 'summon',
      },
    })
  })

  for (const entry of detail.entries) {
    const at = Date.parse(entry.happened_at)
    if (entry.direction === 'inbound' && entry.actor_kind === 'human') {
      dated.push({
        at,
        item: {
          kind: 'user',
          t: clock(entry.happened_at),
          who: entry.sender || 'operator',
          text: entry.text,
        },
      })
    } else if (entry.actor_kind === 'system') {
      dated.push({ at, item: { kind: 'system', text: entry.text } })
    } else {
      dated.push({
        at,
        item: {
          kind: 'agent',
          label: entry.agent ?? 'agent',
          blocks: [{ type: 'p', text: entry.text }],
        },
      })
    }
  }

  dated.sort((a, b) => a.at - b.at)
  const ledger: LedgerItem[] = dated.map(
    ({ item }) => ({ ...item, uid: next() }) as LedgerItem,
  )
  for (const batch of detail.pending) {
    for (const item of batchFeelers(batch)) {
      ledger.push({ ...item, uid: next() } as LedgerItem)
    }
  }

  return {
    key: detail.thread_key || `#${detail.id.slice(0, 6)}`,
    live: true,
    channel: detail.channel,
    // Directives only go to the console's own channel; anything else is a
    // read-only view of that channel's thread.
    sendKey: detail.channel === 'trunkline' ? detail.thread_key : undefined,
    msgCount: detail.message_count,
    sessions: detail.sessions.map(liveSession),
    ledger,
    history: [],
    ctxK: 8,
    usage: {
      chip: routeLabel(detail.agent, detail.model),
      tok: `${detail.message_count} msg`,
    },
  }
}
