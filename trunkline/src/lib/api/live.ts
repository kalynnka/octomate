/**
 * Translation from the live trunkline API payloads (events.ts) to the shapes
 * the console renders (types.ts). One direction only — the console never posts
 * these shapes back.
 */
import type { ApiSessionEntry, ApiThreadDetail, ApiThreadSummary } from './events'
import { batchFeelers, foldRunReplay } from './fold'
import type {
  ChannelMeta,
  LedgerItem,
  LedgerItemDraft,
  SessionInfo,
  ThreadDetail,
  ThreadSummary,
} from './types'

/**
 * Presentation metadata per known channel id — labels, taglines, brand inks
 * from the comp. The list itself is live (/api/trunkline/channels); an
 * unknown id falls back to its own name in neutral ink.
 */
const CHANNEL_DISPLAY: Record<string, Omit<ChannelMeta, 'id'>> = {
  trunkline: { label: 'Trunkline', sub: 'mention-free', brand: 'var(--color-accent)' },
  slack: { label: 'Slack', sub: 'socket', brand: '#746576' },
  lark: { label: 'Lark', sub: 'webhook', brand: '#666D82' },
  napcat: { label: 'Napcat', sub: 'ws', brand: '#6A828B' },
  // Keyed by the channel id the relay files a native session's thread under
  // (CLAUDE_NATIVE_ID / CODEX_NATIVE_ID) — not the agent tentacle's own name.
  'claude-native': { label: 'Claude', sub: 'hook · tail', native: true, brand: '#98796A' },
  'codex-native': { label: 'Codex', sub: 'hook · tail', native: true, brand: '#677D73' },
}

export function channelMeta(id: string): ChannelMeta {
  const display = CHANNEL_DISPLAY[id] ?? { label: id, sub: '', brand: 'var(--fg-3)' }
  return { id, ...display }
}

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

/**
 * The extension deep link that reopens a native session, read out of each
 * extension's own URI handler:
 *
 * - Claude routes `/open` to `claude-vscode.primaryEditor.open(session, prompt)`.
 * - Codex routes any path into its webview, whose thread route is `/local/<id>`.
 *
 * Both are keyed by the session id the relay files the thread under, so a thread
 * on any other channel has no link — nothing on this machine to reopen.
 */
function sessionLink(channel: string, chatId: string): string | undefined {
  if (!chatId) return undefined
  const id = encodeURIComponent(chatId)
  if (channel === 'claude-native') {
    return `vscode://anthropic.claude-code/open?session=${id}`
  }
  if (channel === 'codex-native') return `vscode://openai.chatgpt/local/${id}`
  return undefined
}

export function liveThreadSummary(t: ApiThreadSummary): ThreadSummary {
  const channel = channelMeta(t.channel)
  return {
    id: t.id,
    channelId: t.channel,
    title: t.title,
    tone: channel.native ? 'native' : t.status === 'active' ? 'active' : 'idle',
    // Nothing routed a native session — the runtime it ran in is its agent, and
    // "unrouted" would read as a routing decision that never happened.
    agentLabel:
      t.agent === null && channel.native
        ? `${channel.label.toLowerCase()} · native`
        : routeLabel(t.agent, t.model),
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

  // With recorded runs, agent turns replay from the model ledger below — the
  // chat ledger's outbound text rows would render each reply twice.
  const hasRuns = detail.runs.length > 0

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
    } else if (!hasRuns) {
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

  for (const run of detail.runs) {
    const at = run.started_at ? Date.parse(run.started_at) : 0
    for (const item of foldRunReplay(run.events)) {
      dated.push({ at, item })
    }
  }

  // Stable sort: a run's items share its start time, and a user directive at
  // the same millisecond was pushed first — insertion order settles both.
  dated.sort((a, b) => a.at - b.at)
  const ledger: LedgerItem[] = dated.map(
    ({ item }) => ({ ...item, uid: next() }) as LedgerItem,
  )
  for (const batch of detail.pending) {
    for (const item of batchFeelers(batch)) {
      ledger.push({ ...item, uid: next() } as LedgerItem)
    }
  }

  // Where the last run ran, kept only when it is not the project root itself —
  // and then as the part below the root, which is the whole of what drifted. A
  // cwd outside the root has nothing to trim and shows in full.
  // The directory to open: the project's root when the thread is filed under one,
  // else the directory the session opened in — its first run's, not its last,
  // which may have drifted into a subdirectory nobody works from.
  const openDir = detail.project?.root || detail.runs[0]?.cwd || ''

  const root = detail.project?.root
  const lastCwd = detail.runs.at(-1)?.cwd
  const drift =
    root === undefined || !lastCwd || lastCwd === root
      ? undefined
      : lastCwd.startsWith(`${root}/`)
        ? lastCwd.slice(root.length + 1)
        : lastCwd

  return {
    key: detail.thread_key || `#${detail.id.slice(0, 6)}`,
    live: true,
    channel: detail.channel,
    project:
      detail.project === null
        ? undefined
        : {
            name: detail.project.name,
            path: detail.project.root,
            cwd: drift,
          },
    vscode: openDir
      ? {
          dir: openDir,
          // `vscode://file/<path>` opens a folder as readily as a file, and
          // focuses the window already holding it rather than opening a second.
          folder: `vscode://file${openDir}`,
          session: sessionLink(detail.channel, detail.chat_id),
        }
      : undefined,
    // Directives only go to the console's own channel; anything else is a
    // read-only view of that channel's thread.
    sendKey: detail.channel === 'trunkline' ? detail.thread_key : undefined,
    msgCount: detail.message_count,
    sessions: detail.sessions.map(liveSession),
    ledger,
    ctxK: 8,
    usage: {
      chip: routeLabel(detail.agent, detail.model),
      tok: `${detail.message_count} msg`,
    },
  }
}
