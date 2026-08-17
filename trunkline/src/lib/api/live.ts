/**
 * Translation from the live trunkline API payloads (events.ts) to the shapes
 * the console renders (types.ts). One direction only — the console never posts
 * these shapes back.
 */
import type {
  ApiAgentRun,
  ApiConversation,
  ApiDeferredBatch,
  ApiHandoff,
  ApiProject,
  ApiThread,
  ApiThreadMessage,
} from './events'
import { batchFeelers, replayRun, type ReplayChild } from './fold'
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
  // (CLAUDE_NATIVE_ID / CODEX_NATIVE_ID / DEEPSEEK_NATIVE_ID) — not the agent
  // tentacle's own name. DeepSeek's tail reads the dsh gateway, not a file,
  // hence its tagline.
  'claude-native': { label: 'Claude', sub: 'hook · tail', native: true, brand: '#98796A' },
  'codex-native': { label: 'Codex', sub: 'hook · tail', native: true, brand: '#677D73' },
  'deepseek-native': { label: 'DeepSeek', sub: 'hook · gateway', native: true, brand: '#66709B' },
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
 * Agents with a VS Code extension to hand a thread over to. Inkling has none —
 * it runs inside the relay, so there is no session on this machine to reopen and
 * no directory it was ever working in. DeepSeek qualifies through the shared
 * daemon: its extension attaches to the same dsh the relay drives, so a driven
 * session is already in the extension's session list.
 */
const VSCODE_AGENTS = new Set(['claude', 'codex', 'deepseek'])

/**
 * Whether VS Code can pick this thread up. A native session always can: its own
 * extension is what recorded the thread, and `sessionLink` reopens it. A console
 * thread can only when the agent driving it is one of those runtimes — which is
 * a handoff away, since nothing files a native thread under one.
 */
function vscodeCanOpen(channel: string, agent: string | null): boolean {
  return channelMeta(channel).native === true || VSCODE_AGENTS.has(agent ?? '')
}

/**
 * The extension deep link that reopens a native session, read out of each
 * extension's own URI handler:
 *
 * - Claude routes `/open` to `claude-vscode.primaryEditor.open(session, prompt)`.
 * - Codex routes any path into its webview, whose thread route is `/local/<id>`.
 *
 * Both are keyed by the session id the relay files the thread under, so a thread
 * on any other channel has no link — nothing on this machine to reopen. The dsh
 * extension registers no URI handler, so a deepseek-native thread gets the
 * folder open only; its session waits in the extension's own list, which reads
 * the same shared daemon.
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

/**
 * The route that owns the thread: the last handoff's, since handoffs arrive
 * oldest first. A thread nothing has routed yet has none.
 */
function activeRoute(handoffs: ApiHandoff[]): {
  agent: string | null
  model: string | null
} {
  const last = handoffs.at(-1)
  return {
    agent: last?.to_agent_tentacle_id ?? null,
    model: last?.to_model ?? null,
  }
}

/**
 * What a thread goes by in the sidebar. The relay serves threads without their
 * messages — the ledger is its own request — so there is no first line to name
 * a thread after until it is opened, and the surface it lives on is the honest
 * stand-in: the platform thread key, or the chat when the surface is not a
 * thread (a DM, a native session's own id).
 */
function threadLabel(t: ApiThread): string {
  return t.channel_thread_id || t.chat_id || `#${t.id.slice(0, 6)}`
}

export function liveThreadSummary(t: ApiThread): ThreadSummary {
  const channel = channelMeta(t.channel_tentacle_id)
  const { agent, model } = activeRoute(t.handoffs)
  return {
    id: t.id,
    channelId: t.channel_tentacle_id,
    key: t.channel_thread_id ?? '',
    title: threadLabel(t),
    tone: channel.native ? 'native' : t.status === 'active' ? 'active' : 'idle',
    // Nothing routed a native session — the runtime it ran in is its agent, and
    // "unrouted" would read as a routing decision that never happened.
    agentLabel:
      agent === null && channel.native
        ? `${channel.label.toLowerCase()} · native`
        : routeLabel(agent, model),
    tag: `#${t.id.slice(0, 6)}`,
  }
}

/** The live all-channel listing, grouped the way the sidebar consumes it. */
export function groupLiveThreads(
  threads: ApiThread[],
): Record<string, ThreadSummary[]> {
  const grouped: Record<string, ThreadSummary[]> = {}
  for (const t of threads) {
    ;(grouped[t.channel_tentacle_id] ??= []).push(liveThreadSummary(t))
  }
  return grouped
}

/** The 4-hex chip a session goes by, off the tail of the conversation it is. */
function sessionTag(conversationId: string): string {
  return `SES-${conversationId.replaceAll('-', '').slice(-4).toUpperCase()}`
}

/**
 * The sessions a thread had — one per conversation, which is the stretch of the
 * thread one agent owned. The thread is ours: a channel thread crosses agents as
 * summons move it, and each agent's own line through it is its conversation.
 *
 * A handoff opens a session and says why. A handoff back to an agent that already
 * has a conversation here re-claims that session rather than starting another,
 * because the agent resumes the context it left. A thread nothing routed is a
 * native runtime's session tailed in through the hooks: one conversation, whose
 * `external_session_id` is the runtime's own session id, and no handoff at all.
 */
function liveSessions(
  handoffs: ApiHandoff[],
  conversations: ApiConversation[],
  anchors: Map<string, string>,
): SessionInfo[] {
  const openedBy = new Map<string, ApiHandoff>()
  for (const handoff of handoffs) {
    const target = handoff.target_conversation_id
    if (target !== null && !openedBy.has(target)) openedBy.set(target, handoff)
  }
  return conversations
    .map((conversation) => {
      const handoff = openedBy.get(conversation.id)
      return {
        conversation,
        handoff,
        // When the session opened: the claim that opened it, or — for one nothing
        // claimed — the first turn its runtime wrote.
        when: handoff?.created_at ?? conversation.runs[0]?.started_at ?? null,
      }
    })
    .sort((a, b) => (a.when ? Date.parse(a.when) : 0) - (b.when ? Date.parse(b.when) : 0))
    .map(({ conversation, handoff, when }, index) => {
      const turns = conversation.runs.length
      // An ingest is a session nothing claimed whose turns were rebuilt from a
      // runtime's own transcript. Unclaimed alone is not enough: a channel that
      // dispatches by config records no handoff either, and that session was
      // still ours to run.
      const ingested = handoff === undefined && conversation.runs[0]?.kind === 'external'
      return {
        n: `S${index + 1}`,
        id: sessionTag(conversation.id),
        conversationId: conversation.id,
        route: handoff
          ? routeLabel(handoff.to_agent_tentacle_id, handoff.to_model)
          : conversation.agent_tentacle_id,
        agent: conversation.agent_tentacle_id,
        mode: conversation.permission_mode,
        kind: ingested ? 'ingest' : index === 0 ? 'entry' : 'summon',
        t: when ? clock(when) : '',
        reason: ingested
          ? `${turns} turn${turns === 1 ? '' : 's'} tailed from the session's own transcript`
          : handoff?.reason || 'route claimed',
        status: ingested ? 'ingested' : '',
        tone: ingested ? 'teal' : index === 0 ? 'accent' : 'gold',
        anchor: anchors.get(conversation.id),
      }
    })
}

/** One thread as the console reads it: the row, then the sub-resources that
 *  hang off it, each its own request. */
export interface ThreadReads {
  thread: ApiThread
  messages: ApiThreadMessage[]
  conversations: ApiConversation[]
  project: ApiProject | null
  batches: ApiDeferredBatch[]
}

export function liveThreadDetail(reads: ThreadReads): ThreadDetail {
  const { thread, messages, conversations, project, batches } = reads
  let uid = 0
  const next = () => `h${++uid}`

  // `session` is the conversation the card belongs to, which is what the
  // timeline buckets on: each session's first card is where it starts.
  type Dated = { at: number; item: LedgerItemDraft; session?: string }
  const dated: Dated[] = []

  thread.handoffs.forEach((handoff, index) => {
    const opened = handoff.target_conversation_id
    dated.push({
      at: Date.parse(handoff.created_at),
      session: opened ?? undefined,
      item: {
        kind: 'session-open',
        // The session the claim opened, not the claim — a re-summon reopens a
        // session that already has this tag, and says so by carrying it.
        sessionId: sessionTag(opened ?? handoff.id),
        text: routeLabel(handoff.to_agent_tentacle_id, handoff.to_model),
        tone: index === 0 ? 'plain' : 'summon',
      },
    })
  })

  // The chat ledger is what was said — the prompt and the answer. The work
  // between them comes from the runs below, so nothing here pushes a card the
  // replay will push again.
  for (const message of messages) {
    const at = Date.parse(message.happened_at)
    const text = message.message_text ?? ''
    if (message.direction === 'inbound' && message.actor_kind === 'human') {
      dated.push({
        at,
        item: {
          kind: 'user',
          t: clock(message.happened_at),
          who: message.sender?.name || 'operator',
          text,
        },
      })
    } else if (message.actor_kind === 'system') {
      dated.push({ at, item: { kind: 'system', text } })
    } else {
      dated.push({
        at,
        item: {
          kind: 'agent',
          label: message.agent_tentacle_id ?? 'agent',
          blocks: [{ type: 'p', text }],
        },
      })
    }
  }

  // The agent's own conversations: a subagent's runs surface through the
  // parent's tool call, not as the thread's own history — the spawn call each
  // child run names is what the replay renders as its subagent card.
  const own = conversations.filter((conversation) => !conversation.subagent_id)
  const children = new Map<string, ReplayChild>()
  for (const conversation of conversations) {
    if (!conversation.subagent_id) continue
    for (const run of conversation.runs) {
      if (run.parent_tool_call_id) {
        children.set(run.parent_tool_call_id, { agentId: conversation.subagent_id })
      }
    }
  }

  // The work between a prompt and its answer, rebuilt from what each run
  // recorded. Each card is dated at its own message's clock — the order a live
  // stream would have delivered it — so a card can never sort above the prompt
  // that caused it, whatever clock stamped that prompt's ledger row.
  for (const conversation of own) {
    for (const run of conversation.runs) {
      const started = run.started_at ? Date.parse(run.started_at) : 0
      for (const card of replayRun(run.messages ?? [], children)) {
        dated.push({
          at: card.at !== null ? Date.parse(card.at) : started,
          item: card.item,
          session: conversation.id,
        })
      }
    }
  }

  // Stable sort: a handoff and the directive that caused it can share a
  // millisecond, and insertion order settles the tie.
  dated.sort((a, b) => a.at - b.at)
  const ledger: LedgerItem[] = dated.map(
    ({ item }) => ({ ...item, uid: next() }) as LedgerItem,
  )
  // Where each session starts in the ledger — its first card, which the timeline
  // jumps to and splits its buckets on.
  const anchors = new Map<string, string>()
  dated.forEach(({ session }, index) => {
    if (session !== undefined && !anchors.has(session)) {
      anchors.set(session, ledger[index].uid)
    }
  })
  for (const batch of batches) {
    for (const item of batchFeelers(batch.id, batch.questions, batch.approvals)) {
      ledger.push({ ...item, uid: next() } as LedgerItem)
    }
  }

  // The agent's own line, oldest first. Each conversation arrives sorted;
  // merging two of them is what needs the sort — and a run whose source
  // reported no start sorts first, never against a timestamp.
  const startedAt = (run: ApiAgentRun) => (run.started_at ? Date.parse(run.started_at) : 0)
  const runs = own
    .flatMap((conversation) => conversation.runs)
    .sort((a, b) => startedAt(a) - startedAt(b))

  // The directory to open: the project's root when the thread is filed under one,
  // else the directory the session opened in — its first run's, not its last,
  // which may have drifted into a subdirectory nobody works from.
  const openDir = project?.root || runs[0]?.cwd || ''

  // Where the last run ran, kept only when it is not the project root itself —
  // and then as the part below the root, which is the whole of what drifted. A
  // cwd outside the root has nothing to trim and shows in full.
  const root = project?.root
  const lastCwd = runs.at(-1)?.cwd
  const drift =
    root === undefined || !lastCwd || lastCwd === root
      ? undefined
      : lastCwd.startsWith(`${root}/`)
        ? lastCwd.slice(root.length + 1)
        : lastCwd

  const { agent, model } = activeRoute(thread.handoffs)
  return {
    key: thread.channel_thread_id || `#${thread.id.slice(0, 6)}`,
    live: true,
    channel: thread.channel_tentacle_id,
    project:
      project === null
        ? undefined
        : { name: project.name, path: project.root, cwd: drift },
    vscode:
      openDir && vscodeCanOpen(thread.channel_tentacle_id, agent)
      ? {
          dir: openDir,
          // `vscode://file/<path>` opens a folder as readily as a file, and
          // focuses the window already holding it rather than opening a second.
          folder: `vscode://file${openDir}`,
          session: sessionLink(thread.channel_tentacle_id, thread.chat_id),
        }
      : undefined,
    // Directives only go to the console's own channel; anything else is a
    // read-only view of that channel's thread.
    sendKey:
      thread.channel_tentacle_id === 'trunkline'
        ? (thread.channel_thread_id ?? undefined)
        : undefined,
    msgCount: messages.length,
    sessions: liveSessions(thread.handoffs, own, anchors),
    ledger,
    ctxK: 8,
    usage: {
      chip: routeLabel(agent, model),
      tok: `${messages.length} msg`,
    },
  }
}
