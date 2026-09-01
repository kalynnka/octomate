/**
 * Folds the trunkline SSE stream (lib/api/events.ts) into ledger items — one
 * fold per run. The store hands it push/patch against the live overlay; the
 * fold keeps the per-run cursors (open text part, open thinking part, tool
 * cards by call id, the plan card, subagent cards) and maps every wire event
 * family onto the card language the comp defined.
 */
import type {
  ApiModelMessage,
  ModelResponsePart,
  ToolCallPart,
  WireDeferredApproval,
  WireDeferredQuestion,
  WireEvent,
  WireSegment,
  WireTodo,
} from '@/lib/api/events'
import type { LedgerItem, LedgerItemDraft } from '@/lib/api/types'

/**
 * A batch's unanswered actions as feeler cards. Both sources reach this: the
 * live stream's `action_batch` event and GET /threads/{id}/batches, which
 * carry the same action shapes under different batch-id spellings. `uid` is
 * filled by the caller.
 */
export function batchFeelers(
  batchId: string,
  questions: WireDeferredQuestion[],
  approvals: WireDeferredApproval[],
): LedgerItemDraft[] {
  const items: LedgerItemDraft[] = []
  for (const q of questions) {
    items.push({
      kind: 'ask',
      title: 'Question',
      body: q.args.question,
      options: (q.args.choices ?? []).map((choice) => ({
        label: choice,
        sum: choice,
        desc: '',
      })),
      tool: q.tool_name,
      meta: `${q.tool_name} · answer resumes the run`,
      state: 'waiting',
      batchId,
      actionId: q.id,
    })
  }
  for (const a of approvals) {
    items.push({
      kind: 'approval',
      title: a.args.title || 'Permission required',
      desc: a.args.description || JSON.stringify(a.args.args ?? {}),
      tool: a.args.tool_name,
      meta: `${a.args.tool_name} · approval resumes the run`,
      state: 'waiting',
      batchId,
      actionId: a.id,
    })
  }
  return items
}

export interface FoldSink {
  push(item: LedgerItemDraft): string
  patch(uid: string, patch: Partial<LedgerItem>): void
  /** called once, on run_result / run_error, after the closing items are pushed */
  done(): void
}

function segmentText(segment: WireSegment): string {
  // Text-ish segments carry `text`; reply segments carry `content`.
  const text = segment.data.text ?? segment.data.content
  return typeof text === 'string' ? text : JSON.stringify(segment.data)
}

function argsText(args: string | Record<string, unknown> | null): string {
  if (args == null) return ''
  return typeof args === 'string' ? args : JSON.stringify(args)
}

function contentText(content: unknown): string {
  if (content == null) return ''
  if (typeof content === 'string') return content
  try {
    return JSON.stringify(content)
  } catch {
    return String(content)
  }
}

const TRUNC = 700

function clip(text: string): string {
  return text.length > TRUNC ? `${text.slice(0, TRUNC)} …` : text
}

function toolIcon(name: string): 'search' | 'file-diff' | 'send' | 'file' {
  if (/search|grep|find|history/i.test(name)) return 'search'
  if (/edit|diff|write|apply/i.test(name)) return 'file-diff'
  if (/send|relay|message/i.test(name)) return 'send'
  return 'file'
}

/** A child run reachable from a parent's spawn call, keyed by that call's id —
 * how the replay knows a tool card is really a subagent card. */
export interface ReplayChild {
  agentId: string
}

/** One replayed card with the clock of the message that produced it, so the
 * caller can date each card at its own moment — the order a live stream would
 * have delivered — instead of piling a whole run onto its start time. */
export interface ReplayCard {
  at: string | null
  item: LedgerItemDraft
}

/**
 * A finished run's thinking, text, and tool cards, rebuilt from the model
 * messages the run recorded. This is the reload path: the chat ledger keeps what
 * was said, so a reader coming back to a thread would otherwise see a prompt, an
 * answer, and no sign of the work between them.
 *
 * The middle is rebuilt whole — including the narration between tool calls —
 * but the trailing answer text is left out: the thread ledger already carries
 * it, and pushing it again would double every answer. Returns are indexed first
 * so each tool card is born settled; one with no return stays `run`, which is
 * what a run that died mid-call actually left behind. A call a `children` entry
 * names is a subagent spawn and renders as a subagent card instead of a tool.
 */
export function replayRun(
  messages: ApiModelMessage[],
  children?: Map<string, ReplayChild>,
): ReplayCard[] {
  const returns = new Map<string, { result: string; failed: boolean }>()
  for (const message of messages) {
    for (const part of message.parts) {
      // A function tool answers in the next request; a native one answers in the
      // same response it was called from.
      if (part.part_kind === 'tool-return' || part.part_kind === 'builtin-tool-return') {
        returns.set(part.tool_call_id, {
          result: contentText(part.content),
          failed: part.outcome !== undefined && part.outcome !== 'success',
        })
      } else if (part.part_kind === 'retry-prompt') {
        returns.set(part.tool_call_id, {
          result: contentText(part.content),
          failed: true,
        })
      }
    }
  }

  const toolCard = (
    callId: string,
    name: string,
    args: string | Record<string, unknown> | null,
  ): LedgerItemDraft => {
    const settled = returns.get(callId)
    const clipped = clip(argsText(args))
    if (settled === undefined) {
      return {
        kind: 'tool',
        name,
        icon: toolIcon(name),
        status: 'run',
        detail: { type: 'plain', args: clipped, res: '' },
      }
    }
    return {
      kind: 'tool',
      name,
      icon: toolIcon(name),
      status: 'done',
      badge: settled.failed ? { label: 'failed', tone: 'terra' } : undefined,
      detail: {
        type: 'plain',
        args: clipped,
        res: clip(settled.result) || '(no output)',
      },
    }
  }

  const subCard = (
    callId: string,
    name: string,
    args: string | Record<string, unknown> | null,
    child: ReplayChild,
  ): LedgerItemDraft => {
    const settled = returns.get(callId)
    const route =
      typeof args === 'object' && args !== null && typeof args.subagent_type === 'string'
        ? args.subagent_type
        : name
    return {
      kind: 'sub',
      id: child.agentId.slice(-6).toUpperCase(),
      route,
      note:
        settled === undefined
          ? 'running'
          : clip(settled.result) || '(no output)',
    }
  }

  // Everything after the last real work is the answer the ledger already shows;
  // text before that mark is the run's own narration and must render, or every
  // block between two tool calls silently vanishes on reload.
  let lastWork = -1
  let index = 0
  for (const message of messages) {
    if (message.kind !== 'response') continue
    for (const part of message.parts) {
      if (part.part_kind !== 'text' && part.part_kind !== 'file') lastWork = index
      index += 1
    }
  }

  const cards: ReplayCard[] = []
  index = 0
  for (const message of messages) {
    if (message.kind !== 'response') continue
    const at = message.timestamp
    const push = (item: LedgerItemDraft) => cards.push({ at, item })
    for (const part of message.parts) {
      switch (part.part_kind) {
        case 'thinking':
          // No duration survives the row, and a made-up one would be a claim.
          // `content` is routinely empty: a Claude transcript signs each thinking
          // block and writes no text with it, so a replayed one has nothing to
          // show and the card says so rather than opening on a blank quote.
          push({ kind: 'think', dur: '', text: part.content, thinking: false })
          break
        case 'tool-call':
        case 'builtin-tool-call': {
          const child = children?.get(part.tool_call_id)
          push(
            child === undefined
              ? toolCard(part.tool_call_id, part.tool_name, part.args)
              : subCard(part.tool_call_id, part.tool_name, part.args, child),
          )
          break
        }
        case 'builtin-tool-return':
          break
        case 'compaction':
          push({ kind: 'divider', label: 'context compacted' })
          break
        case 'text':
          if (index < lastWork && part.content) {
            push({ kind: 'stream', text: part.content, streaming: false })
          }
          break
        case 'file':
          break
      }
      index += 1
    }
  }
  return cards
}

export class TurnFold {
  private sink: FoldSink
  private streamUid: string | null = null
  private streamText = ''
  private thinkUid: string | null = null
  private thinkText = ''
  private thinkStarted = 0
  private toolUids = new Map<string, { uid: string; args: string }>()
  private planUid: string | null = null
  private todos = new Map<string, WireTodo>()
  private subUids = new Map<string, string>()
  private sawReply = false
  private ended = false

  constructor(sink: FoldSink) {
    this.sink = sink
  }

  private closeStream() {
    if (this.streamUid !== null) {
      this.sink.patch(this.streamUid, { streaming: false })
      this.streamUid = null
    }
  }

  private closeThink() {
    if (this.thinkUid !== null) {
      const secs = Math.max(1, Math.round((Date.now() - this.thinkStarted) / 1000))
      this.sink.patch(this.thinkUid, { dur: `${secs}s`, thinking: false })
      this.thinkUid = null
    }
  }

  private appendReply(text: string) {
    if (!text) return
    this.sawReply = true
    if (this.streamUid === null) {
      this.streamText = text
      this.streamUid = this.sink.push({
        kind: 'stream',
        text,
        streaming: true,
      })
      return
    }
    this.streamText += text
    this.sink.patch(this.streamUid, { text: this.streamText })
  }

  private openPart(part: ModelResponsePart) {
    switch (part.part_kind) {
      case 'text':
        this.closeThink()
        this.appendReply(part.content)
        break
      case 'thinking':
        this.closeStream()
        this.closeThink()
        this.thinkText = part.content
        this.thinkStarted = Date.now()
        this.thinkUid = this.sink.push({
          kind: 'think',
          dur: '…',
          text: part.content,
          thinking: true,
        })
        break
      case 'builtin-tool-call':
        this.openTool(part.tool_call_id, part.tool_name, argsText(part.args))
        break
      case 'builtin-tool-return':
        this.settleTool(part.tool_call_id, contentText(part.content))
        break
      case 'compaction':
        this.sink.push({ kind: 'divider', label: 'context compacted' })
        break
      case 'tool-call':
      case 'file':
        break
    }
  }

  private openTool(callId: string, name: string, args: string) {
    // Reply text after the tool belongs to a new block, not the pre-tool one.
    this.closeStream()
    const clipped = clip(args)
    const uid = this.sink.push({
      kind: 'tool',
      name,
      icon: toolIcon(name),
      status: 'run',
      detail: { type: 'plain', args: clipped, res: '' },
    })
    this.toolUids.set(callId, { uid, args: clipped })
  }

  private settleTool(callId: string, result: string, failed = false) {
    const open = this.toolUids.get(callId)
    if (open === undefined) return
    this.toolUids.delete(callId)
    this.sink.patch(open.uid, {
      status: 'done',
      badge: failed ? { label: 'failed', tone: 'terra' } : undefined,
      detail: {
        type: 'plain',
        args: open.args,
        res: clip(result) || '(no output)',
      },
    })
  }

  private refreshPlan() {
    const steps = [...this.todos.values()]
      .sort((a, b) => a.position - b.position)
      .map((todo, index) => ({
        n: String(index + 1).padStart(2, '0'),
        text: todo.content,
        status:
          todo.status === 'completed'
            ? ('done' as const)
            : todo.status === 'in_progress'
              ? ('active' as const)
              : ('idle' as const),
      }))
    if (this.planUid === null) {
      this.planUid = this.sink.push({ kind: 'plan', steps })
    } else {
      this.sink.patch(this.planUid, { steps })
    }
  }

  feed(event: WireEvent) {
    // No early return on `ended`: the suspend path emits its action_batch
    // after run_result, and those feeler cards must still land.
    switch (event.event_kind) {
      case 'part_start':
        this.openPart(event.part)
        break
      case 'part_delta':
        switch (event.delta.part_delta_kind) {
          case 'text':
            this.appendReply(event.delta.content_delta)
            break
          case 'thinking':
            if (this.thinkUid !== null && event.delta.content_delta) {
              this.thinkText += event.delta.content_delta
              this.sink.patch(this.thinkUid, { text: this.thinkText })
            }
            break
          case 'tool_call':
            break
        }
        break
      case 'part_end':
        if (event.part.part_kind === 'thinking') this.closeThink()
        break
      case 'function_tool_call': {
        const part: ToolCallPart = event.part
        this.closeThink()
        this.openTool(part.tool_call_id, part.tool_name, argsText(part.args))
        break
      }
      case 'function_tool_result':
        if (event.part.part_kind === 'retry-prompt') {
          this.settleTool(event.part.tool_call_id, contentText(event.part.content), true)
        } else {
          this.settleTool(
            event.part.tool_call_id,
            contentText(event.part.content),
            event.part.outcome !== undefined && event.part.outcome !== 'success',
          )
        }
        break
      case 'result_segment':
        this.appendReply(
          (this.streamUid !== null ? '\n\n' : '') + segmentText(event.segment),
        )
        break
      case 'result_text_delta':
        this.appendReply(event.delta)
        break
      case 'todo_created':
      case 'todo_updated':
      case 'todo_status_changed':
      case 'todo_completed':
        this.todos.set(event.todo.ref, event.todo)
        this.refreshPlan()
        break
      case 'todo_deleted':
        this.todos.delete(event.todo.ref)
        this.refreshPlan()
        break
      case 'message_sent':
        this.closeStream()
        this.sink.push({
          kind: 'agent',
          label: 'relay',
          blocks: event.segments.map((segment) => ({
            type: 'p',
            text: segmentText(segment),
          })),
        })
        break
      case 'oauth_authorization':
      case 'oauth_device_authorization':
        this.closeStream()
        this.closeThink()
        this.sink.push({
          kind: 'oauth',
          label: event.label,
          uri: event.authorization_uri,
          code: event.user_code,
          connectorId: event.connector_id,
        })
        break
      case 'action_batch':
        this.closeStream()
        this.closeThink()
        for (const item of batchFeelers(
          event.batch_id,
          event.questions,
          event.approvals,
        )) {
          this.sink.push(item)
        }
        break
      case 'subagent_started':
        this.subUids.set(
          event.invocation_id,
          this.sink.push({
            kind: 'sub',
            id: event.invocation_id.slice(-6).toUpperCase(),
            route: event.kind,
            note: `${event.name} · running`,
          }),
        )
        break
      case 'subagent_settled': {
        const uid = this.subUids.get(event.invocation_id)
        if (uid !== undefined) {
          this.sink.patch(uid, { note: event.detail || event.status })
        }
        break
      }
      case 'run_result': {
        if (this.ended) break
        this.closeThink()
        if (!this.sawReply && event.output !== null) {
          const text =
            typeof event.output === 'string'
              ? event.output
              : event.output.map(segmentText).join('\n\n')
          this.appendReply(text)
        }
        this.closeStream()
        const u = event.usage
        this.sink.push({
          kind: 'end',
          label:
            `run complete · ${u.requests} ${u.requests === 1 ? 'request' : 'requests'}` +
            ` · ${u.input_tokens.toLocaleString('en-US')} tokens in · ${u.output_tokens.toLocaleString('en-US')} out`,
        })
        this.ended = true
        this.sink.done()
        break
      }
      case 'run_error':
        if (this.ended) break
        this.closeThink()
        this.closeStream()
        this.sink.push({ kind: 'notice', text: `run failed — ${event.message}` })
        this.ended = true
        this.sink.done()
        break
      case 'final_result':
      case 'output_tool_call':
      case 'output_tool_result':
      case 'deferred_tool_requests':
      case 'deferred_tool_results':
      case 'enqueued_messages':
        break
    }
  }

  /** The stream closed without a terminal event (transport drop). */
  abort(message: string) {
    if (this.ended) return
    this.closeThink()
    this.closeStream()
    this.sink.push({ kind: 'notice', text: message })
    this.ended = true
    this.sink.done()
  }
}

