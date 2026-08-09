// The Trunkline wire union — a 1:1 mirror of the backend's SSE payloads
// (WireEvent in octomate/capabilities/harness/events.py). Every event carries an
// `event_kind` discriminator; the pydantic-ai members serialize with their own
// native field names, so these types follow the Python schema exactly.

// ---- pydantic-ai response parts -------------------------------------------

export interface TextPart {
  part_kind: 'text'
  content: string
  id?: string | null
}

export interface ThinkingPart {
  part_kind: 'thinking'
  content: string
  id?: string | null
  signature?: string | null
}

export interface ToolCallPart {
  part_kind: 'tool-call'
  tool_name: string
  args: string | Record<string, unknown> | null
  tool_call_id: string
  tool_kind?: string | null
}

export interface NativeToolCallPart {
  part_kind: 'builtin-tool-call'
  tool_name: string
  args: string | Record<string, unknown> | null
  tool_call_id: string
  tool_kind?: string | null
}

export interface NativeToolReturnPart {
  part_kind: 'builtin-tool-return'
  tool_name: string
  content: unknown
  tool_call_id: string
  outcome?: 'success' | 'failed' | 'denied' | 'interrupted'
  timestamp?: string
}

export interface CompactionPart {
  part_kind: 'compaction'
  content: string | null
  id?: string | null
}

export interface FilePart {
  part_kind: 'file'
  content: unknown
  id?: string | null
}

export type ModelResponsePart =
  | TextPart
  | ThinkingPart
  | ToolCallPart
  | NativeToolCallPart
  | NativeToolReturnPart
  | CompactionPart
  | FilePart

export interface ToolReturnPart {
  part_kind: 'tool-return'
  tool_name: string
  content: unknown
  tool_call_id: string
  outcome?: 'success' | 'failed' | 'denied' | 'interrupted'
  timestamp?: string
}

export interface RetryPromptPart {
  part_kind: 'retry-prompt'
  content: string | Array<Record<string, unknown>>
  tool_name: string | null
  tool_call_id: string
  timestamp?: string
}

export interface TextPartDelta {
  part_delta_kind: 'text'
  content_delta: string
}

export interface ThinkingPartDelta {
  part_delta_kind: 'thinking'
  content_delta: string | null
  signature_delta?: string | null
}

export interface ToolCallPartDelta {
  part_delta_kind: 'tool_call'
  tool_name_delta: string | null
  args_delta: string | Record<string, unknown> | null
  tool_call_id: string | null
}

export type ModelResponsePartDelta =
  | TextPartDelta
  | ThinkingPartDelta
  | ToolCallPartDelta

// ---- pydantic-ai stream events --------------------------------------------

export interface PartStartEvent {
  event_kind: 'part_start'
  index: number
  part: ModelResponsePart
  previous_part_kind?: ModelResponsePart['part_kind'] | null
}

export interface PartDeltaEvent {
  event_kind: 'part_delta'
  index: number
  delta: ModelResponsePartDelta
}

export interface PartEndEvent {
  event_kind: 'part_end'
  index: number
  part: ModelResponsePart
  next_part_kind?: ModelResponsePart['part_kind'] | null
}

export interface FinalResultEvent {
  event_kind: 'final_result'
  tool_name: string | null
  tool_call_id: string | null
}

export interface FunctionToolCallEvent {
  event_kind: 'function_tool_call'
  part: ToolCallPart
  args_valid?: boolean | null
}

export interface FunctionToolResultEvent {
  event_kind: 'function_tool_result'
  part: ToolReturnPart | RetryPromptPart
  content?: unknown
}

export interface OutputToolCallEvent {
  event_kind: 'output_tool_call'
  part: ToolCallPart
}

export interface OutputToolResultEvent {
  event_kind: 'output_tool_result'
  part: ToolReturnPart | RetryPromptPart
}

export interface DeferredToolRequestsEvent {
  event_kind: 'deferred_tool_requests'
  requests: unknown
}

export interface DeferredToolResultsEvent {
  event_kind: 'deferred_tool_results'
  results: unknown
}

export interface EnqueuedMessagesEvent {
  event_kind: 'enqueued_messages'
  enqueue_id: string
  messages: unknown[]
}

// ---- octomate extension events --------------------------------------------

export interface WireSegment {
  type: string
  data: Record<string, unknown>
}

export interface ResultSegmentEvent {
  event_kind: 'result_segment'
  segment: WireSegment
}

export interface ResultTextDeltaEvent {
  event_kind: 'result_text_delta'
  delta: string
}

export interface WireTodo {
  ref: string
  content: string
  status: string
  position: number
}

export interface TodoEvent {
  event_kind:
    | 'todo_created'
    | 'todo_updated'
    | 'todo_status_changed'
    | 'todo_completed'
    | 'todo_deleted'
  todo: WireTodo
  previous?: WireTodo | null
}

export interface MessageSentEvent {
  event_kind: 'message_sent'
  segments: WireSegment[]
  destination?: Record<string, unknown> | null
}

export interface OAuthAuthorizationEvent {
  event_kind: 'oauth_authorization' | 'oauth_device_authorization'
  connector_id: string
  label: string
  authorization_uri: string
  user_code?: string
}

export interface WireQuestionArgs {
  question: string
  choices?: string[] | null
  hint?: string
}

export interface WireApprovalArgs {
  tool_name: string
  args?: Record<string, unknown>
  title?: string
  description?: string
}

export interface WireDeferredAction {
  id: string
  status: string
  tool_name: string
  tool_call_id: string
  position?: number
}

export interface WireDeferredQuestion extends WireDeferredAction {
  kind: 'question'
  args: WireQuestionArgs
}

export interface WireDeferredApproval extends WireDeferredAction {
  kind: 'approval'
  args: WireApprovalArgs
}

export interface ActionBatchEvent {
  event_kind: 'action_batch'
  batch_id: string
  questions: WireDeferredQuestion[]
  approvals: WireDeferredApproval[]
}

// ---- transport events ------------------------------------------------------

export interface SubagentStartedEvent {
  event_kind: 'subagent_started'
  invocation_id: string
  kind: 'commission' | 'whisper'
  name: string
}

export interface SubagentSettledEvent {
  event_kind: 'subagent_settled'
  invocation_id: string
  status: 'completed' | 'failed' | 'timed_out' | 'cancelled'
  detail?: string | null
  response: string
}

export interface RunUsage {
  requests: number
  tool_calls: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
}

export interface RunResultEvent {
  event_kind: 'run_result'
  output: string | WireSegment[] | null
  usage: RunUsage
}

export interface RunErrorEvent {
  event_kind: 'run_error'
  message: string
}

export type WireEvent =
  | PartStartEvent
  | PartDeltaEvent
  | PartEndEvent
  | FinalResultEvent
  | FunctionToolCallEvent
  | FunctionToolResultEvent
  | OutputToolCallEvent
  | OutputToolResultEvent
  | DeferredToolRequestsEvent
  | DeferredToolResultsEvent
  | EnqueuedMessagesEvent
  | ResultSegmentEvent
  | ResultTextDeltaEvent
  | TodoEvent
  | MessageSentEvent
  | OAuthAuthorizationEvent
  | ActionBatchEvent
  | SubagentStartedEvent
  | SubagentSettledEvent
  | RunResultEvent
  | RunErrorEvent

// ---- REST payloads ---------------------------------------------------------

export interface ApiChannelInfo {
  id: string
  kind: string // tentacle class name — SlackTentacle, …
}

export interface ApiRoute {
  id: string // opaque — model names may embed ':', never split this
  agent: string
  model: string
}

export interface ApiThreadSummary {
  /** thread row id (uuid) — the read key for GET /threads/{id} */
  id: string
  /** owning channel tentacle id */
  channel: string
  /** platform thread id; for trunkline threads, the directive key */
  thread_key: string
  /** the chat as its channel names it; for a native session, its session id */
  chat_id: string
  title: string
  status: 'active' | 'closed'
  agent: string | null
  model: string | null
  updated_at: string
  message_count: number
}

export interface ApiLedgerEntry {
  id: string
  direction: 'inbound' | 'outbound'
  actor_kind: 'human' | 'agent' | 'bot' | 'system'
  agent: string | null
  sender: string
  happened_at: string
  text: string
}

export interface ApiSessionEntry {
  id: string
  from_agent: string | null
  to_agent: string
  model: string | null
  reason: string
  created_at: string
}

/**
 * One recorded agent run, replayed by the relay as the wire events its live
 * stream carried — reload folds through the same TurnFold as live streaming.
 */
export interface ApiRunReplay {
  id: string
  agent: string
  started_at: string | null
  /** the directory this run ran in; "" when its source reported none */
  cwd: string
  events: WireEvent[]
}

/** The project a thread's work is in. Read-only — the relay takes no writes. */
export interface ApiProject {
  name: string
  root: string
}

export interface ApiThreadDetail extends ApiThreadSummary {
  entries: ApiLedgerEntry[]
  project: ApiProject | null
  sessions: ApiSessionEntry[]
  runs: ApiRunReplay[]
  pending: ActionBatchEvent[]
}

export interface DirectiveBody {
  text: string
  message_id?: string
  model?: string
}

export interface BatchResponseBody {
  answers?: Record<string, string>
  approvals?: Record<string, boolean>
  allow_session?: boolean
}
