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
  ApiModelMessage,
  ApiPage,
  ApiProject,
  ApiThread,
  ApiThreadMessage,
  ApiUsage,
} from './events'
import { batchFeelers, replayRun, segmentText, type ReplayChild } from './fold'
import type {
  ChannelMeta,
  LedgerItem,
  SessionInfo,
  ThreadDetail,
  ThreadSummary,
  ThreadUsage,
} from './types'

/**
 * Presentation metadata per known channel id — labels, taglines, brand inks
 * from the comp. The list itself is live (/api/trunkline/channels); an
 * unknown id falls back to its own name in neutral ink.
 */
const CHANNEL_DISPLAY: Record<string, Omit<ChannelMeta, 'id'>> = {
  trunkline: { label: 'Trunkline', sub: 'mention-free', brand: 'var(--color-accent)' },
  discord: { label: 'Discord', sub: 'gateway', brand: '#5865F2' },
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
  const display = CHANNEL_DISPLAY[id] ?? {
    label: id.charAt(0).toUpperCase() + id.slice(1), sub: '', brand: 'var(--fg-3)',
  }
  return { id, ...display, brand: `color-mix(in srgb, ${display.brand} 50%, var(--fg-1))` }
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
 * The current conversation owns an imported session; explicit handoffs can change its agent.
 */
function activeRoute(thread: ApiThread): {
  agent: string | null
  model: string | null
} {
  const last = thread.handoffs.at(-1)
  return {
    agent: last?.to_agent_tentacle_id ?? thread.active_agent_tentacle_id ?? null,
    model: last?.to_model ?? null,
  }
}

/**
 * What a thread goes by in the sidebar. The row carries a name — the runtime's
 * own where it grabbed one, else the line the thread opened with — and falls
 * back to the surface it lives on for a thread nothing has been said in: the
 * platform thread key, or the chat when the surface is not a thread (a DM, a
 * native session's own id).
 */
function threadLabel(t: ApiThread): string {
  return t.title || t.channel_thread_id || t.chat_id || threadTag(t.id)
}

export function liveThreadSummary(t: ApiThread): ThreadSummary {
  const channel = channelMeta(t.channel_tentacle_id)
  const { agent, model } = activeRoute(t)
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
    tag: threadTag(t.id),
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

/** The 6-hex chip a thread goes by, off the tail of the row id — the head of a
 *  uuid7 is a clock, so every thread of the same few hours shares it. */
function threadTag(threadId: string): string {
  return `#${threadId.replaceAll('-', '').slice(-6)}`
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
 * `native_session_id` is the runtime's own session id, and no handoff at all.
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
      const latest = conversation.runs.at(-1)
      const model = conversation.runs.findLast((run) => run.model_name)?.model_name ?? null
      // An ingest is a session nothing claimed whose turns were rebuilt from a
      // runtime's own transcript. Unclaimed alone is not enough: a channel that
      // dispatches by config records no handoff either, and that session was
      // still ours to run.
      const ingested = handoff === undefined && conversation.runs[0]?.kind === 'external'
      return {
        n: `S${index + 1}`,
        id: sessionTag(conversation.id),
        name: conversation.name ?? undefined,
        conversationId: conversation.id,
        route: routeLabel(
          conversation.agent_tentacle_id,
          handoff?.to_model ?? model,
        ),
        agent: conversation.agent_tentacle_id,
        model: handoff?.to_model ?? model,
        effort: conversation.effort,
        mode: conversation.permission_mode ?? latest?.permission_mode ?? null,
        kind: ingested ? 'ingest' : index === 0 ? 'entry' : 'summon',
        t: when ? clock(when) : '',
        reason: ingested
          ? `${turns} turn${turns === 1 ? '' : 's'} tailed from the session's own transcript`
          : handoff?.reason || 'route claimed',
        status: ingested ? 'ingested' : conversation.status,
        tone: ingested ? 'teal' : index === 0 ? 'accent' : 'gold',
        anchor: anchors.get(conversation.id),
      }
    })
}

/** One thread as the console reads it: the row, the newest stretch of its
 *  two paged reads, and the sub-resources that hang off it, each its own request. */
export interface ThreadReads {
  thread: ApiThread
  /** the model messages of the thread's own conversations read so far, oldest first */
  modelMessages: ApiModelMessage[]
  /** its chat rows no model message carries, read so far, oldest first */
  unbound: ApiThreadMessage[]
  /** where each read goes on from; null once it has reached the thread's start */
  cursors: Record<ThreadRead, string | null>
  /** how many rows each read holds on the relay */
  totals: Record<ThreadRead, number>
  conversations: ApiConversation[]
  project: ApiProject | null
  batches: ApiDeferredBatch[]
  usage: ApiUsage
}

export type ThreadRead = 'model' | 'unbound'

/** One more page of either read, newest first as the relay serves it. */
export type OlderPage =
  | { read: 'model'; page: ApiPage<ApiModelMessage> }
  | { read: 'unbound'; page: ApiPage<ApiThreadMessage> }

/**
 * The read holding the other back: of those with more to give, the one whose
 * oldest row in hand is newest. Nothing older than that row can show yet — the
 * other read may still hold rows newer than it — and it is the one to page next.
 */
export function holdingBack(reads: ThreadReads): ThreadRead | null {
  const model = reads.cursors.model === null ? undefined : reads.modelMessages[0]?.id
  const unbound = reads.cursors.unbound === null ? undefined : reads.unbound[0]?.id
  if (model === undefined) return unbound === undefined ? null : 'unbound'
  return unbound === undefined || model > unbound ? 'model' : 'unbound'
}

export function liveThreadDetail(reads: ThreadReads): ThreadDetail {
  const { thread, modelMessages, unbound, conversations, project, batches, usage: spent } = reads

  // Every row is keyed by its uuid7, which is when it was written; a run and the
  // ledger are only appended to, so that is the order things happened in. A
  // card's uid comes from the row it was read off, so reading further back never
  // renames the cards already shown. `session` is the conversation the card
  // belongs to, which is what the timeline buckets on.
  type Keyed = { key: string; item: LedgerItem; session?: string }
  const keyed: Keyed[] = []

  thread.handoffs.forEach((handoff, index) => {
    const opened = handoff.target_conversation_id
    keyed.push({
      key: handoff.id,
      session: opened ?? undefined,
      item: {
        kind: 'session-open',
        uid: handoff.id,
        // The session the claim opened, not the claim — a re-summon reopens a
        // session that already has this tag, and says so by carrying it.
        sessionId: sessionTag(opened ?? handoff.id),
        text: routeLabel(handoff.to_agent_tentacle_id, handoff.to_model),
        tone: index === 0 ? 'plain' : 'summon',
      },
    })
  })

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
  const ranIn = new Map(
    own.flatMap((conversation) => conversation.runs.map((run) => [run.id, conversation] as const)),
  )

  // What was said: a row no model message carries stands at its own place, and
  // the rows a request was built from stand at the request's — in their senders'
  // own words, not the prompt's dressing. A row an agent said is answered by its
  // own response, so only what came in is read off a request.
  const said = [
    ...unbound.map((message) => ({ key: message.id, message, session: undefined })),
    ...modelMessages.flatMap((request) =>
      request.kind !== 'request'
        ? []
        : request.thread_messages
            .filter((message) => message.direction === 'inbound')
            .map((message) => ({ key: request.id, message, session: ranIn.get(request.run_id)?.id })),
    ),
  ]
  for (const { key, message, session } of said) {
    const text = message.message_text ?? ''
    if (message.direction === 'inbound' && message.actor_kind === 'human') {
      keyed.push({
        key,
        session,
        item: {
          kind: 'user',
          uid: message.id,
          t: clock(message.happened_at),
          who: message.sender?.name || 'operator',
          text,
        },
      })
    } else if (message.actor_kind === 'system') {
      // Each paragraph gets one bullet; line breaks within it stay together.
      for (const [n, paragraph] of text.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean).entries()) {
        keyed.push({
          key,
          session,
          item: { kind: 'system', uid: `${message.id}.${n}`, text: paragraph, tone: n === 0 ? 'info' : undefined },
        })
      }
    } else {
      keyed.push({
        key,
        item: {
          kind: 'agent',
          uid: message.id,
          label: message.agent_tentacle_id ?? 'agent',
          blocks: [{ type: 'p', text }],
        },
      })
    }
    if (message.kind === 'command' && message.outcome) {
      const outcome = message.outcome
      const feedback = outcome.status === 'completed' ? outcome.segments.map(segmentText) : [outcome.message]
      if (feedback.length) keyed.push({
        key,
        session,
        item: {
          kind: 'agent',
          uid: `${message.id}.outcome`,
          label: 'relay',
          blocks: feedback.map((text) => ({ type: 'p', text })),
        },
      })
    }
  }
  // A request nothing was bound to speaks for itself — a turn whose chat row was
  // never tied to it.
  for (const request of modelMessages) {
    if (request.kind !== 'request' || request.thread_messages.some((m) => m.direction === 'inbound')) continue
    for (const [n, part] of request.parts.entries()) {
      if (part.part_kind !== 'user-prompt') continue
      const text = typeof part.content === 'string' ? part.content : part.content.filter((c) => typeof c === 'string').join('\n')
      keyed.push({
        key: request.id,
        session: ranIn.get(request.run_id)?.id,
        item: { kind: 'user', uid: `${request.id}.${n}`, t: request.timestamp ? clock(request.timestamp) : '', who: 'operator', text },
      })
    }
  }

  // The work between a prompt and its answer, and the answer, rebuilt from what
  // each run recorded — as much of it as the pages in hand reach.
  const recorded = new Map<string, ApiModelMessage[]>()
  for (const message of modelMessages) {
    const run = recorded.get(message.run_id)
    if (run === undefined) recorded.set(message.run_id, [message])
    else run.push(message)
  }
  for (const [runId, messages] of recorded) {
    const conversation = ranIn.get(runId)
    for (const card of replayRun(messages, conversation?.agent_tentacle_id ?? 'agent', children)) {
      keyed.push({ key: card.message, item: { ...card.item, uid: card.id } as LedgerItem, session: conversation?.id })
    }
  }

  // Stable sort, so the cards of one message keep the order of its parts. Rows
  // older than what the read holding the other back has reached wait for it.
  const back = holdingBack(reads)
  const horizon = back === null ? '' : back === 'model' ? modelMessages[0].id : unbound[0].id
  keyed.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
  const shown = new Set<string>()
  const ledger: LedgerItem[] = []
  // Where each session starts in the ledger — its first card, which the timeline
  // jumps to and splits its buckets on.
  const anchors = new Map<string, string>()
  for (const { key, item, session } of keyed) {
    if (key < horizon || shown.has(item.uid)) continue
    shown.add(item.uid)
    ledger.push(item)
    if (session !== undefined && !anchors.has(session)) anchors.set(session, item.uid)
  }
  for (const batch of batches) {
    for (const [n, item] of batchFeelers(batch.id, batch.questions, batch.approvals).entries()) {
      ledger.push({ ...item, uid: `${batch.id}.${n}` } as LedgerItem)
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

  // What the thread cost, as the relay summed it over every response, and what
  // its last turn was carrying — the newest response's own input, which is the
  // context it ran with. Summing those would add every turn's context together,
  // which is not a window.
  const usage: ThreadUsage = {
    input: spent.input_tokens ?? 0,
    output: spent.output_tokens ?? 0,
    cacheRead: spent.cache_read_tokens ?? 0,
    cacheWrite: spent.cache_write_tokens ?? 0,
    cacheRate: null,
  }
  const read = usage.input + usage.cacheRead + usage.cacheWrite
  usage.cacheRate = read > 0 ? usage.cacheRead / read : null
  const turn = modelMessages.findLast(
    (message): message is Extract<ApiModelMessage, { kind: 'response' }> =>
      message.kind === 'response' && !!message.usage,
  )?.usage
  const context =
    (turn?.input_tokens ?? 0) + (turn?.cache_read_tokens ?? 0) + (turn?.cache_write_tokens ?? 0)

  const { agent } = activeRoute(thread)
  return {
    key: thread.channel_thread_id || threadTag(thread.id),
    kind: thread.kind,
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
      thread.kind !== 'native_thread' && thread.channel_tentacle_id === 'trunkline'
        ? (thread.channel_thread_id ?? undefined)
        : undefined,
    msgCount: reads.totals.model + reads.totals.unbound,
    earlier: reads.cursors.model !== null || reads.cursors.unbound !== null,
    reads,
    sessions: liveSessions(thread.handoffs, own, anchors),
    ledger,
    usage,
    ctxK: Math.round(context / 1000),
  }
}

/**
 * `detail` with one more page of either read folded on. The cards it already
 * shows stay as they are — an answer marked on a feeler survives — and the new
 * ones go above them: a page only reaches further back, and a row waits until
 * both reads are past it. Sessions keep what was changed on them and take their
 * anchors afresh, since a session's first card may be on this page.
 */
export function withOlderPage(detail: ThreadDetail, reads: ThreadReads, older: OlderPage): ThreadDetail {
  const cursor = older.page.has_more ? older.page.next_cursor : null
  const cursors = { ...reads.cursors, [older.read]: cursor }
  const totals = { ...reads.totals, [older.read]: older.page.total }
  const fresh = liveThreadDetail(
    older.read === 'model'
      ? { ...reads, cursors, totals, modelMessages: [...older.page.items].reverse().concat(reads.modelMessages) }
      : { ...reads, cursors, totals, unbound: [...older.page.items].reverse().concat(reads.unbound) },
  )
  const shown = new Set(detail.ledger.map((item) => item.uid))
  return {
    ...detail,
    msgCount: fresh.msgCount,
    earlier: fresh.earlier,
    reads: fresh.reads,
    ledger: [...fresh.ledger.filter((item) => !shown.has(item.uid)), ...detail.ledger],
    sessions: detail.sessions.map((session) => ({
      ...session,
      anchor: fresh.sessions.find((each) => each.conversationId === session.conversationId)?.anchor,
    })),
  }
}
