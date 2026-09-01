/**
 * Domain types for the Trunkline console.
 *
 * Shapes follow the octomate backend schemas (Thread / Handoff / Conversation /
 * DeferredAction / Project — see octomate/schemas/) flattened to what the
 * console renders. Mock adapters fill them today; real endpoints can replace
 * the adapters without touching the UI.
 */

export type ChannelId = string

export interface ChannelMeta {
  id: ChannelId
  label: string
  /** short transport note shown beside the channel name, e.g. "socket" */
  sub: string
  /** brand color (CSS color) used for the channel letter tile and labels */
  brand: string
  /** hook-ingested native agent channel (claude / codex) */
  native?: boolean
}

/** Visual tone of a thread's status dot. */
export type ThreadTone = 'active' | 'idle' | 'native'

export interface ThreadSummary {
  id: string
  channelId: ChannelId
  /** the platform's own thread id — what a directive is addressed to, and the only
   *  handle the new-thread flow has for a thread before it knows the row. Empty for
   *  a surface that is not a thread */
  key: string
  /** the surface the thread is, which is all a listing knows to call it by —
   *  the relay serves threads without their messages */
  title: string
  tone: ThreadTone
  /** owning route, e.g. "inkling → claude" or "codex · gpt-5.3" */
  agentLabel: string
  /** short display chip — the thread row id, which every read is keyed by */
  tag?: string
}

export type SessionKind = 'entry' | 'summon' | 'teleport' | 'ingest'
export type SessionTone = 'accent' | 'gold' | 'sage' | 'teal' | 'ghost'

export interface SessionInfo {
  n: string
  id: string
  /** the conversation row this session is — what a posture switch is written to */
  conversationId: string
  route: string
  /** the agent that owns it, which says which approval vocabulary applies */
  agent: string
  /** its approval posture; null = nothing declared, so the agent's own default
   *  decides, and an agent with no vocabulary is always null */
  mode: string | null
  kind: SessionKind
  t: string
  reason: string
  status: string
  tone: SessionTone
  /** mid-session surface hop note, e.g. a teleport record */
  hop?: string
  /** ledger anchor uid of the session's first item (for timeline jumps) */
  anchor?: string
}

/* ---------------------------------------------------------------------------
 * Ledger — the chat body of one thread. A discriminated union per rendered
 * item; feelers (approval / ask) are deferred actions awaiting the operator.
 * ------------------------------------------------------------------------- */

export interface QueueChip {
  qid: string
  kind: 'quote' | 'cmt'
  /** comment id when kind === 'cmt' */
  ref?: string
  label: string
  body: string
}

export interface DiffLine {
  tone: 'ctx' | 'add' | 'del'
  text: string
}

export interface DiffCard {
  file: string
  adds: number
  dels: number
  lines: DiffLine[]
}

/** One block of an agent message. Inline `code` spans use backtick markup. */
export type AgentBlock =
  | { type: 'p'; text: string }
  | { type: 'ul'; items: string[] }
  | { type: 'diff'; diff: DiffCard }

export type ToolDetail =
  | { type: 'plain'; args: string; res: string }
  | { type: 'kv'; heading: string; rows: [string, string][] }
  | {
      type: 'checks'
      rows: { n: string; name: string; note?: string; verdict: 'PASS' | 'FAIL'; dur: string }[]
      summary: string
    }
  | {
      type: 'log'
      heading: string
      note: string
      pre: { text: string; tone?: 'error' | 'em' }[]
      spill?: { label: string; text: string }
    }
  | { type: 'summon'; rows: [string, string][] }

export interface AskOption {
  label: string
  sum: string
  desc: string
}

export type LedgerItem =
  | { kind: 'divider'; uid: string; label: string }
  | { kind: 'system'; uid: string; text: string }
  | {
      kind: 'session-open'
      uid: string
      sessionId: string
      text: string
      tone: 'plain' | 'summon'
    }
  | { kind: 'user'; uid: string; t: string; who: string; text: string; chips?: QueueChip[] }
  | { kind: 'agent'; uid: string; label: string; blocks: AgentBlock[] }
  | { kind: 'code'; uid: string; lang: string; lines: string[] }
  /** `thinking` is false once the part closed — the card folds itself then,
   *  the way `stream.streaming` marks a reply as settled */
  | { kind: 'think'; uid: string; dur: string; text: string; thinking: boolean }
  | {
      kind: 'plan'
      uid: string
      steps: { n: string; text: string; status: 'done' | 'active' | 'idle' }[]
    }
  | {
      kind: 'tool'
      uid: string
      name: string
      icon: 'search' | 'file-diff' | 'summon' | 'send' | 'file'
      dur?: string
      status: 'run' | 'done'
      badge?: { label: string; tone: 'gold' | 'terra' }
      detail: ToolDetail
      defaultOpen?: boolean
    }
  | { kind: 'file'; uid: string; name: string; rev: string; note: string }
  | { kind: 'sub'; uid: string; id: string; route: string; note: string }
  | {
      kind: 'approval'
      uid: string
      title: string
      desc: string
      /** the tool the approval is for — "Edit", "Bash" */
      tool: string
      /** e.g. "Edit · approval resumes the run" */
      meta: string
      state: 'waiting' | 'approved' | 'dismissed'
      resolvedT?: string
      /** live deferred-action ids; present when the card answers a real batch */
      batchId?: string
      actionId?: string
    }
  | {
      kind: 'ask'
      uid: string
      title: string
      body: string
      options: AskOption[]
      /** the tool that asked */
      tool: string
      /** e.g. "ask_human · answer resumes the run" */
      meta: string
      state: 'waiting' | 'answered'
      answer?: string
      via?: string
      resolvedT?: string
      /** live deferred-action ids; present when the card answers a real batch */
      batchId?: string
      actionId?: string
    }
  | {
      kind: 'oauth'
      uid: string
      /** integration label — "GitHub" */
      label: string
      uri: string
      /** device-flow one-time code the user types on the page; absent for an
       *  authorization-code flow, whose callback finishes the connection */
      code?: string
      connectorId: string
    }
  | { kind: 'dots'; uid: string; label: string }
  | { kind: 'stream'; uid: string; text: string; streaming: boolean }
  | { kind: 'end'; uid: string; label: string }
  | { kind: 'notice'; uid: string; text: string }

/** `Omit<LedgerItem, 'uid'>` distributed over the union, so a draft keeps its
 * per-kind shape (plain Omit would collapse to the common keys only). */
export type LedgerItemDraft = LedgerItem extends infer T
  ? T extends LedgerItem
    ? Omit<T, 'uid'>
    : never
  : never

/* ---------------------------------------------------------------------------
 * Review dossier — a plan file pinned to a thread, marked up line by line.
 * ------------------------------------------------------------------------- */

export type DocLineKind =
  | 'meta'
  | 'blank'
  | 'h1'
  | 'h2'
  | 'p'
  | 'li'
  | 'num'
  | 'quote'
  | 'code'
  | 'hr'

export interface DocLine {
  id: string
  k: DocLineKind
  t: string
  mark: '' | 'add' | 'del'
  lang?: string
}

export interface ReviewDoc {
  rev: string
  lines: DocLine[]
}

export type CommentStatus = 'queued' | 'sent' | 'outdated'

export interface ReviewComment {
  id: string
  file: string
  /** DocLine id the comment folds under */
  anchor: string
  range: string
  who: string
  t: string
  status: CommentStatus
  appliedIn?: string
  text: string
}

export interface ArtifactRef {
  name: string
  ses: string
  note: string
}

/* ---------------------------------------------------------------------------
 * Projects — the working tree a thread's session ran in. Read-only: the relay
 * learns a project from where a native session runs and freezes it on the
 * thread, so nothing here is ever sent back.
 * ------------------------------------------------------------------------- */

export interface ProjectRef {
  name: string
  path: string
  /** the latest run's directory, when it sits below the root rather than on it */
  cwd?: string
  /** git state has no relay endpoint yet; unset means unknown, not "no git" */
  git?: boolean
  branch?: string
  stat?: string
  dirty?: boolean
  /** "also filed here: THR-0142 · slack …" */
  also?: string
}

/**
 * Where a thread's work lives on this machine, as VS Code deep links. Built
 * from what the relay reports — never typed by a user — so the console only
 * navigates to them.
 */
export interface VsCodeTarget {
  /** the directory, for the button's tooltip and the strip */
  dir: string
  /** opens (or focuses) that directory as a folder */
  folder: string
  /** reopens the native session in its own extension, when the thread is one */
  session?: string
}

export interface SurfaceInfo {
  id: ChannelId
  label: string
  sub: string
  brand: string
}

export interface ThreadDetail {
  key: string
  /** true when this detail was hydrated from the live trunkline API */
  live?: boolean
  /** owning channel tentacle id (live threads) */
  channel?: string
  /** trunkline directive key; absent = read-only (another channel's thread) */
  sendKey?: string
  msgCount: number
  sessions: SessionInfo[]
  ledger: LedgerItem[]
  project?: ProjectRef
  vscode?: VsCodeTarget
  artifacts?: ArtifactRef[]
  docs?: Record<string, ReviewDoc>
  comments?: ReviewComment[]
}

/* ---------------------------------------------------------------------------
 * Control plane — agents, MCP, users, dashboard, settings.
 * ------------------------------------------------------------------------- */

export type EffortStep = 'minimal' | 'low' | 'medium' | 'high' | 'xhigh'

export interface AgentModelInfo {
  name: string
  efforts: EffortStep[]
}

export interface AgentInfo {
  name: string
  tag: string
  tagTone: 'accent' | 'teal'
  state: string
  stateTone: 'accent' | 'sage'
  miniNote: string
  models: AgentModelInfo[]
}

export interface AgentRouteDef {
  name: string
  code: string
  models: string[]
  desc: string
}

export interface McpServerRow {
  key: string
  url: string
  prefix: string
  status: string
}

export interface IntegrationRow {
  name: string
  flow: string
  line: string
}

export interface ConnectionRow {
  user: string
  connector: string
  state: 'connected' | 'pending' | 'cold'
  note: string
}

export interface UserRow {
  username: string
  kind: 'registered' | 'pseudo' | 'observed'
  profileLine: string
  oauth: string
}

export interface ProviderRow {
  name: string
  status: string
  tone: 'sage' | 'teal' | 'ghost'
  src: string
}

export interface KvRow {
  k: string
  v: string
  tone: 'ink' | 'sage' | 'ghost'
}

export interface StatTile {
  k: string
  v: string
  sub: string
}

export interface FeedRow {
  t: string
  verb: string
  text: string
}

export interface BarDatum {
  label: string
  value: number
}

export interface ControlData {
  agents: AgentInfo[]
  agentRoutes: AgentRouteDef[]
  mcpRows: McpServerRow[]
  integrations: IntegrationRow[]
  connections: ConnectionRow[]
  users: UserRow[]
  providers: ProviderRow[]
  hookRows: KvRow[]
  obsRows: KvRow[]
  stats: StatTile[]
  feed: FeedRow[]
  verbBars: BarDatum[]
}

export interface StatusChip {
  t: string
  tip: string
  dot?: 'teal' | 'gold' | 'red'
}

export interface StatusData {
  left: StatusChip[]
  right: StatusChip[]
}

/** GET /api/configure — the one real config endpoint today. */
export interface ConfigureModel {
  id: string
  name: string
}
