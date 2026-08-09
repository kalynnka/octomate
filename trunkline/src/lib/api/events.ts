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

/*
 * The REST reads are the backend transmuters themselves (octomate/schemas/),
 * not a console-shaped copy — so these mirror the rows field for field. Two
 * relations never arrive: a thread's `messages` are their own request, and a
 * run's model transcript is the agent's own history and never leaves.
 */

/** One handoff: the point an agent-model route took the thread over. */
export interface ApiHandoff {
  id: string
  thread_id: string
  from_agent_tentacle_id: string | null
  to_agent_tentacle_id: string
  to_model: string | null
  reason: string
  hint: string
  brief: string
  created_at: string
}

export interface ApiThread {
  /** thread row id (uuid) — the read key for every /threads/{id} read */
  id: string
  kind: 'dm' | 'group' | 'thread' | 'native_thread'
  chat_type: string
  /** the chat as its channel names it; for a native session, its session id */
  chat_id: string
  channel_tentacle_id: string
  /** the platform's own thread id; for trunkline threads, the directive key */
  channel_thread_id: string | null
  /** the project this thread's work is in; read it with /threads/{id}/project */
  project_id: string | null
  status: 'active' | 'closed'
  created_at: string
  updated_at: string
  /** oldest first — the last one owns the thread */
  handoffs: ApiHandoff[]
}

export interface ApiUserProfile {
  id: string
  channel_tentacle_id: string
  channel_user_id: string
  name: string
  nickname: string | null
}

export interface ApiThreadMessage {
  id: string
  thread_id: string
  platform_message_id: string | null
  /** when it happened, and what the ledger orders on */
  happened_at: string
  direction: 'inbound' | 'outbound'
  actor_kind: 'human' | 'agent' | 'bot' | 'system'
  agent_tentacle_id: string | null
  sender: ApiUserProfile | null
  segments: WireSegment[]
  message_text: string | null
  created_at: string
}

/** One recorded run. `kind` tells an Octomate-driven run from one rebuilt out
 *  of an external runtime's transcript. */
export interface ApiAgentRun {
  id: string
  kind: 'octomate' | 'external'
  conversation_id: string
  name: string | null
  /** the directory this run ran in; "" when its source reported none */
  cwd: string
  parent_run_id: string | null
  parent_tool_call_id: string | null
  started_at: string | null
  external_session_id?: string | null
}

export interface ApiConversation {
  id: string
  external_id: string | null
  thread_id: string
  agent_tentacle_id: string
  /** "" for the agent's own conversation; a subagent's identity otherwise */
  subagent_id: string
  parent_conversation_id: string | null
  name: string | null
  status: string
  permission_mode: string
  allowed_tools: string[]
  /** oldest first, by start time */
  runs: ApiAgentRun[]
}

/** The project a thread's work is in. Read-only — the relay takes no writes. */
export interface ApiProject {
  id: string
  name: string
  origin: string
  root: string
  extra_roots: string[]
  description: string | null
}

/** An unanswered batch, in the same action shapes the stream's `action_batch`
 *  event carries — so a reload renders the waiting feelers identically. */
export interface ApiDeferredBatch {
  id: string
  conversation_id: string
  agent_tentacle_id: string
  status: string
  questions: WireDeferredQuestion[]
  approvals: WireDeferredApproval[]
  created_at: string
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
