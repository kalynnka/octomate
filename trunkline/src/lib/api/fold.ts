/**
 * Folds a thread's live socket (lib/api/events.ts) into ledger items — one
 * fold per run. The store hands it push/patch against the live overlay; the
 * fold keeps the per-run cursors (open text part, open thinking part, tool
 * cards by call id, the plan card, subagent cards) and maps every wire event
 * family onto the card language the comp defined.
 */
import type {
  ApiModelMessage,
  ApiUserProfile,
  CommandStreamEvent,
  ModelResponsePart,
  ToolCallPart,
  WireDeferredApproval,
  WireDeferredQuestion,
  WireSegment,
  WireTodo,
} from '@/lib/api/events'
import type { AskAnswer, AskQuestion, LedgerItem, LedgerItemDraft } from '@/lib/api/types'

/**
 * A batch's unanswered actions as feeler cards. Both sources reach this: the
 * live stream's `action_batch` event and GET /threads/{id}/batches, which
 * carry the same action shapes under different batch-id spellings. `uid` is
 * filled by the caller.
 */
/** An answer as one line of text, for showing it. */
export function answerText(answer: AskAnswer): string {
  return typeof answer === 'string' ? answer : answer.join(', ')
}

/** Native question tools are observations; only deferred batches can be answered here. */
function nativeQuestion(
  callId: string,
  name: string,
  args: string | Record<string, unknown> | null,
  agent: string,
): Extract<LedgerItemDraft, { kind: 'ask' }> | null {
  if (!['AskUserQuestion', 'ask_user_question', 'request_user_input', 'request_user_input_async'].includes(name)) return null
  let payload: unknown
  try {
    payload = typeof args === 'string' ? JSON.parse(args) : args
  } catch {
    return null
  }
  if (!payload || typeof payload !== 'object' || !('questions' in payload) || !Array.isArray(payload.questions)) return null
  const questions: AskQuestion[] = []
  const entries: unknown[] = payload.questions
  for (const question of entries) {
    if (!question || typeof question !== 'object') return null
    const body = name === 'request_user_input_async' && 'title' in question ? question.title
      : 'question' in question ? question.question : null
    if (typeof body !== 'string') return null
    const options: unknown = 'options' in question ? question.options : []
    if (!Array.isArray(options)) return null
    const choices: AskQuestion['options'] = []
    const rawOptions: unknown[] = options
    for (const option of rawOptions) {
      if (name === 'request_user_input_async' && typeof option === 'string') {
        choices.push({ label: option, sum: '', desc: '' })
      } else if (option && typeof option === 'object' && 'label' in option && typeof option.label === 'string') {
        choices.push({ label: option.label, sum: '', desc: 'description' in option && typeof option.description === 'string' ? option.description : '' })
      } else return null
    }
    const nativeId = 'id' in question && typeof question.id === 'string' ? question.id : undefined
    if ((name === 'ask_user_question' || name === 'request_user_input') && nativeId === undefined) return null
    questions.push({ body, options: choices, nativeId, multiSelect: ('multiSelect' in question && question.multiSelect === true) || ('multi_select' in question && question.multi_select === true) })
  }
  return questions.length ? {
    kind: 'ask', title: questions.length === 1 ? 'Question' : 'Questions',
    questions, tool: name, meta: `Answer in ${agent}`, state: 'waiting', nativeCallId: callId, nativeAgent: agent,
  } : null
}

/** DeepSeek uses the same answer batch in tool results and replies after a timeout. */
function deepseekAnswers(payload: unknown): { questionId: string; answer: string[] }[] | null {
  if (!payload || typeof payload !== 'object' || !('answers' in payload) || !Array.isArray(payload.answers)) return null
  const answers: { questionId: string; answer: string[] }[] = []
  const entries: unknown[] = payload.answers
  for (const item of entries) {
    if (!item || typeof item !== 'object' || !('id' in item) || typeof item.id !== 'string' || !('selected' in item) || !Array.isArray(item.selected) || !item.selected.every((value): value is string => typeof value === 'string')) return null
    if ('custom' in item && typeof item.custom !== 'string') return null
    const custom = 'custom' in item && typeof item.custom === 'string' ? [item.custom] : []
    answers.push({ questionId: item.id, answer: [...item.selected, ...custom] })
  }
  return answers
}

/** Match results to their original questions, never treating acceptance or timeout as an answer. */
function nativeQuestionResult(
  card: Extract<LedgerItemDraft, { kind: 'ask' }>, result: string, failed: boolean,
): Extract<LedgerItemDraft, { kind: 'ask' }> {
  const callId = card.nativeCallId
  if (!callId) return card
  if (failed) return { ...card, state: 'failed', meta: result }
  if (card.tool === 'request_user_input_async') return card
  if (card.tool === 'AskUserQuestion') {
    const prefix = 'Your questions have been answered: '
    const suffix = '. You can now continue with these answers in mind.'
    if (!result.startsWith(prefix) || !result.endsWith(suffix)) return { ...card, meta: result }
    let remaining = result.slice(prefix.length, -suffix.length)
    const replies: NativeQuestionReply[] = []
    for (const [index, question] of card.questions.entries()) {
      const start = `"${question.body}"="`
      if (!remaining.startsWith(start)) return { ...card, meta: result }
      remaining = remaining.slice(start.length)
      const next = card.questions[index + 1]
      const end = next ? remaining.indexOf(`", "${next.body}"="`) : remaining.length - 1
      if (end < 0 || (!next && !remaining.endsWith('"'))) return { ...card, meta: result }
      replies.push({ callId, index, answer: remaining.slice(0, end) })
      remaining = remaining.slice(end + 3)
    }
    return nativeQuestionAnswered(card, replies)
  }
  let payload: unknown
  try { payload = JSON.parse(result) } catch { return { ...card, meta: result } }
  if (card.tool === 'ask_user_question') {
    const answers = deepseekAnswers(payload)
    return answers ? nativeQuestionAnswered(card, answers.map(answer => ({ ...answer, callId }))) : card
  }
  if (!payload || typeof payload !== 'object' || !('answers' in payload) || !payload.answers || typeof payload.answers !== 'object') return card
  const replies: NativeQuestionReply[] = []
  const entries: [string, unknown][] = Object.entries(payload.answers)
  for (const [questionId, item] of entries) {
    if (!item || typeof item !== 'object' || !('answers' in item) || !Array.isArray(item.answers) || !item.answers.every((answer: unknown): answer is string => typeof answer === 'string')) return card
    replies.push({ callId, questionId, answer: item.answers })
  }
  return nativeQuestionAnswered(card, replies)
}

export interface NativeQuestionReply {
  callId: string
  index?: number
  questionId?: string
  answer: AskAnswer
}

/** Only unwrap complete native reply envelopes. Other user text stays verbatim. */
export function nativeQuestionReplies(text: string): NativeQuestionReply[] | null {
  const match = text.trim().match(/^<send_user_message_question_reply>\s*([\s\S]*?)\s*<\/send_user_message_question_reply>$/)
  try {
    if (!match) {
      const payload: unknown = JSON.parse(text)
      if (!payload || typeof payload !== 'object' || !('kind' in payload) || payload.kind !== 'answer_to_pending_question' || !('tool' in payload) || payload.tool !== 'ask_user_question' || !('callId' in payload) || typeof payload.callId !== 'string') return null
      const callId = payload.callId
      const answers = deepseekAnswers(payload)
      return answers?.length ? answers.map(answer => ({ ...answer, callId })) : null
    }
    const payload: unknown = JSON.parse(match[1])
    if (!Array.isArray(payload) || !payload.length) return null
    const replies: NativeQuestionReply[] = []
    const entries: unknown[] = payload
    for (const reply of entries) {
      if (!reply || typeof reply !== 'object' || !('questionItemId' in reply) || typeof reply.questionItemId !== 'string' || !('question' in reply) || typeof reply.question !== 'string' || !('answer' in reply) || typeof reply.answer !== 'string') return null
      const id: unknown = JSON.parse(reply.questionItemId)
      if (!Array.isArray(id) || id.length !== 3 || id[0] !== 'request_user_input_async' || typeof id[1] !== 'string' || typeof id[2] !== 'number' || !Number.isInteger(id[2]) || id[2] < 0) return null
      replies.push({ callId: id[1], index: id[2], answer: reply.answer })
    }
    return replies
  } catch {
    // Malformed envelopes are still user messages; never lose their contents.
    return null
  }
}

export function nativeQuestionAnswered(
  card: Extract<LedgerItemDraft, { kind: 'ask' }>,
  replies: NativeQuestionReply[],
): Extract<LedgerItemDraft, { kind: 'ask' }> {
  const answers = replies.filter(reply => reply.callId === card.nativeCallId)
  if (!answers.length) return card
  const questions = card.questions.map((question, index) => {
    const answer = answers.findLast(reply => reply.index === index || (reply.questionId !== undefined && reply.questionId === question.nativeId))
    return answer ? { ...question, answer: answer.answer } : question
  })
  return { ...card, questions, state: questions.every(question => question.answer !== undefined) ? 'answered' : 'waiting' }
}

/** A reply envelope echoes the agent's question; only its answers are user text. */
export function promptText(text: string): string {
  return nativeQuestionReplies(text)?.map(reply => answerText(reply.answer) || 'Skipped').join('\n\n') ?? text
}

export function batchFeelers(
  batchId: string,
  questions: WireDeferredQuestion[],
  approvals: WireDeferredApproval[],
  closed = false,
): LedgerItemDraft[] {
  const items: LedgerItemDraft[] = []
  // A batch's questions are one card: answered together, they resume the run once.
  const ordered = [...questions].sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
  if (ordered.length > 0) {
    const tool = ordered[0].tool_name
    items.push({
      kind: 'ask',
      title: ordered.length === 1 ? 'Question' : 'Questions',
      questions: ordered.map((q) => ({
        body: q.args.question,
        multiSelect: q.args.multi_select ?? false,
        options: (q.args.choices ?? []).map((choice) => ({
          label: choice,
          sum: choice,
          desc: '',
        })),
        actionId: q.id,
        answer: q.result ?? undefined,
        error: q.args.error,
      })),
      tool,
      meta: `${tool} · ${ordered.length === 1 ? 'answer resumes' : 'submitting every answer resumes'} the run`,
      state: closed ? ordered.every(q => q.status === 'answered') ? 'answered' : 'closed' : 'waiting',
      batchId,
    })
  }
  for (const a of approvals) {
    items.push({
      kind: 'approval',
      title: a.args.title || 'Permission required',
      // A description says what the request risks; it never stands in for the request.
      desc: [a.args.description, JSON.stringify(a.args.args ?? {})].filter(Boolean).join(' '),
      tool: a.args.tool_name,
      meta: `${a.args.tool_name} · approval resumes the run`,
      state: a.status === 'approved' ? 'approved' : a.status === 'denied' ? 'dismissed' : closed ? 'closed' : 'waiting',
      batchId,
      actionId: a.id,
    })
  }
  return items
}

export interface FoldSink {
  push(item: LedgerItemDraft & { uid?: string }): string
  patch(uid: string, patch: Partial<LedgerItem>): void
  /** called once, on run_result / run_error, after the closing items are pushed */
  done(): void
}

export function segmentText(segment: WireSegment): string {
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

/** One replayed card with the message that produced it, which is where it sorts:
 * message ids are uuid7 and a run is only appended to, so their order is the
 * order a live stream would have delivered. `id` names the message and part it
 * came from, the same on every replay. */
export interface ReplayCard {
  id: string
  message: string
  item: LedgerItemDraft
}

/**
 * A finished run's thinking, text, and tool cards, rebuilt from the model
 * messages the run recorded. This is the reload path: what a live stream showed
 * of the run, read back.
 *
 * The narration between tool calls renders as the run's own text; what follows
 * its last real work is its answer, and renders as `agent`'s message. Returns are
 * indexed first so each tool card is born settled; one with no return stays
 * `run`, which is what a run that died mid-call actually left behind. A call a
 * `children` entry names is a subagent spawn and renders as a subagent card
 * instead of a tool.
 *
 * `messages` may be only the run's newest stretch, since its messages are paged
 * back from the end; both rules hold on one, as a return follows its call and
 * the answer closes the run.
 */
export function replayRun(
  messages: ApiModelMessage[],
  agent: string,
  children?: Map<string, ReplayChild>,
  native = false,
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
    const question = native ? nativeQuestion(callId, name, args, agent) : null
    if (question) return settled ? nativeQuestionResult(question, settled.result, settled.failed) : question
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

  // Everything after the last real work is the answer; text before that mark is
  // the run's own narration between its tool calls.
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
    for (const [n, part] of message.parts.entries()) {
      const push = (item: LedgerItemDraft) =>
        cards.push({ id: `${message.id}.${n}`, message: message.id, item })
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
          if (!part.content) break
          push(
            index < lastWork
              ? { kind: 'stream', text: part.content, streaming: false }
              : { kind: 'agent', label: agent, blocks: [{ type: 'p', text: part.content }] },
          )
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
  private sender: ApiUserProfile | null = null
  private questions = new Map<string, Extract<LedgerItem, { kind: 'ask' }>>()

  private nativeAgent?: string

  constructor(sink: FoldSink, nativeAgent?: string, history: LedgerItem[] = []) {
    this.sink = sink
    this.nativeAgent = nativeAgent
    for (const item of history) {
      if (item.kind === 'ask' && item.nativeCallId) this.questions.set(item.nativeCallId, item)
    }
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
    const question = this.nativeAgent !== undefined ? nativeQuestion(callId, name, args, this.nativeAgent) : null
    if (question) {
      const uid = this.sink.push(question)
      this.questions.set(callId, { ...question, uid })
      return
    }
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
    const question = this.questions.get(callId)
    if (question) {
      const answered = { ...nativeQuestionResult(question, result, failed), uid: question.uid }
      this.questions.set(callId, answered)
      this.sink.patch(question.uid, answered)
      return
    }
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

  feed(event: CommandStreamEvent) {
    // No early return on `ended`: the suspend path emits its action_batch
    // after run_result, and those feeler cards must still land.
    switch (event.event_kind) {
      case 'run_started':
        this.sender = event.sender ?? null
        break
      case 'run_ended':
        this.closeThink()
        this.closeStream()
        break
      case 'enqueued_messages': {
        const requests = event.messages.filter(message => message.kind === 'request')
        const prompts = requests.flatMap(message => message.parts.filter(part => part.part_kind === 'user-prompt'))
        if (!prompts.length) break
        this.closeThink()
        this.closeStream()
        const timestamp = requests[0]?.timestamp
        const at = timestamp ? new Date(timestamp) : null
        const text = prompts.map(part => typeof part.content === 'string' ? part.content : part.content.filter(c => typeof c === 'string').join('\n')).join('\n')
        for (const reply of nativeQuestionReplies(text) ?? []) {
          const card = this.questions.get(reply.callId)
          if (!card) continue
          const answered = { ...nativeQuestionAnswered(card, [reply]), uid: card.uid }
          this.questions.set(reply.callId, answered)
          this.sink.patch(card.uid, answered)
        }
        this.sink.push({
          kind: 'user', uid: event.enqueue_id, who: this.sender?.name || 'operator', userId: this.sender?.user_id,
          text: promptText(text),
          t: at ? `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}` : '',
        })
        break
      }
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
        this.sink.push({ kind: 'notice', text: `run failed — ${event.message}`, tone: 'error' })
        this.ended = true
        this.sink.done()
        break
      // A command's feedback, refusal included, has already streamed as a channel
      // message; its outcome only says the command is done.
      case 'command_outcome':
        if (this.ended) break
        this.closeThink()
        this.closeStream()
        this.ended = true
        this.sink.done()
        break
      case 'final_result':
      case 'output_tool_call':
      case 'output_tool_result':
      case 'deferred_tool_requests':
      case 'deferred_tool_results':
        break
    }
  }

  /** The stream closed without a terminal event (transport drop). */
  abort(message: string, tone?: Extract<LedgerItem, { kind: 'notice' }>['tone']) {
    if (this.ended) return
    this.closeThink()
    this.closeStream()
    this.sink.push({ kind: 'notice', text: message, tone })
    this.ended = true
    this.sink.done()
  }
}
