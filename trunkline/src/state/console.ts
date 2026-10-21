/**
 * Console state — a faithful port of the design comp's logic class. One store
 * drives selection, panel layout, the live ledger, feelers, the review
 * dossier, and the turn flows. Trunkline threads run live over /api/trunkline
 * (`runLive` + `lib/api/fold.ts`); threads on channels without an API yet
 * keep the comp's review-dossier demo turn (`sendReview`).
 */
import { create } from 'zustand'
import type {
  AskAnswer,
  DocLine,
  EffortStep,
  LedgerItem,
  QueueChip,
  ReviewComment,
  ReviewDoc,
  ThreadDetail,
} from '@/lib/api/types'
import { api, resolveBatch, sendDirective } from '@/lib/api'
import { castGateway, executeCommand, fetchThreads, watchThread } from '@/lib/api/client'
import type {
  ActionBatchAnsweredEvent,
  ApiCommandDescriptor,
  BatchResponseBody,
  ChannelAddress,
  CommandContextBody,
  CommandOutcomeEvent,
  CommandStreamEvent,
  GatewayEvent,
  GatewayRequest,
  RunResultEvent,
  WireEvent,
} from '@/lib/api/events'
import { queryClient } from '@/lib/queryClient'
import { TurnFold } from '@/lib/api/fold'
import { channelMeta, holdingBack, withOlderPage } from '@/lib/api/live'
import { useAuth } from '@/state/auth'
import { COMMAND_CONTROLS } from '@/features/chat/commands'
import { withArtifact } from '@/features/artifacts/ledger'

export type ControlSection = '' | 'agents' | 'mcp' | 'profile' | 'keys' | 'dash' | 'settings'
export type ThemeMode = 'light' | 'dark' | 'auto'
export type InterfaceSize = 'small' | 'standard' | 'large'
export type RailKey = 'sb' | 'mgmt' | 'trace' | 'pv'

interface ChannelPrefs {
  fold: Record<string, boolean>
  pins: string[]
}

const loadChannelPrefs = (): ChannelPrefs => {
  try {
    const raw = JSON.parse(localStorage.getItem('trk-channels') ?? '{}') as Partial<ChannelPrefs>
    return { fold: raw.fold ?? {}, pins: raw.pins ?? [] }
  } catch {
    return { fold: {}, pins: [] }
  }
}

const loadTheme = (): ThemeMode => {
  try {
    const t = localStorage.getItem('trk-theme')
    return t === 'light' || t === 'dark' || t === 'auto' ? t : 'auto'
  } catch {
    return 'auto'
  }
}

const loadInterfaceSize = (): InterfaceSize => {
  try {
    const size = localStorage.getItem('trk-interface-size')
    return size === 'small' || size === 'large' ? size : 'standard'
  } catch {
    return 'standard'
  }
}

export const applyThemeAttr = (dark: boolean) => {
  const el = document.documentElement
  if (dark) el.setAttribute('data-theme', 'dark')
  else el.removeAttribute('data-theme')
}

// Deferred a frame: callers scroll right after a set(), before React commits
// the new rows — measuring scrollHeight now would stop short of the bottom.
const scrollChatBottom = (smooth?: boolean) => {
  requestAnimationFrame(() => {
    const el = document.getElementById('trk-chatlog')
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  })
}

/** Whether the reader is pinned near the ledger's tail (measured pre-commit,
 * so streaming growth follows only while they haven't scrolled away). */
const nearChatBottom = () => {
  const el = document.getElementById('trk-chatlog')
  return el === null || el.scrollHeight - el.scrollTop - el.clientHeight < 120
}

/** Where a synced row lands under its pane's header — the offset ledger jumps
 *  already use, so scrolling to a row and syncing to it agree. */
const RAIL_LEAD = 46

const atTail = (el: HTMLElement) => el.scrollHeight - el.scrollTop - el.clientHeight < 8

// The pane whose scrolling is currently driving the other. Its own scroll
// events are what moving the other pane provokes, and following those back
// would be the two rails chasing each other.
let railDriver: 'chat' | 'timeline' | null = null
let railDriverTimer: ReturnType<typeof setTimeout> | undefined

/**
 * Hold the ledger and the timeline on the same moment. Both index the same
 * cards — a timeline row carries the uid of the chat card it points at — so
 * whichever one the reader scrolls, the other lands on the row it is showing,
 * and a pane at the tail puts the other at the tail: the latest turn is what
 * both open on.
 */
export const syncRails = (source: 'chat' | 'timeline') => {
  if (railDriver !== null && railDriver !== source) return
  const log = document.getElementById('trk-chatlog')
  const rail = document.getElementById('trk-timeline')
  if (log === null || rail === null) return
  const [from, to] = source === 'chat' ? [log, rail] : [rail, log]

  const drive = (top: number) => {
    if (Math.abs(to.scrollTop - top) < 2) return
    railDriver = source
    clearTimeout(railDriverTimer)
    railDriverTimer = setTimeout(() => (railDriver = null), 160)
    to.scrollTop = top
  }

  if (atTail(from)) {
    drive(to.scrollHeight - to.clientHeight)
    return
  }
  // The last row that has scrolled past the lead line is the one being read;
  // rows and cards share the ledger's order, so the first row still below it
  // ends the search. A row whose card has paged out of the ledger is skipped.
  const fold = from.getBoundingClientRect().top + RAIL_LEAD
  let lead: [HTMLElement, HTMLElement] | null = null
  for (const row of rail.querySelectorAll<HTMLElement>('[data-uid]')) {
    const card = document.getElementById(`pm-${row.dataset.uid}`)
    if (card === null) continue
    const pair: [HTMLElement, HTMLElement] = source === 'chat' ? [card, row] : [row, card]
    if (pair[0].getBoundingClientRect().top > fold) break
    lead = pair
  }
  if (lead === null) return
  drive(
    to.scrollTop + lead[1].getBoundingClientRect().top - to.getBoundingClientRect().top - RAIL_LEAD,
  )
}

let flashTimer: ReturnType<typeof setTimeout> | undefined
export const goLedgerTarget = (tgt?: string) => {
  const log = document.getElementById('trk-chatlog')
  if (!log) return
  const el = tgt ? (document.getElementById(tgt) ?? document.getElementById(`pm-${tgt}`)) : null
  if (!el) {
    scrollChatBottom(true)
    return
  }
  log.scrollTo({ top: Math.max(0, el.offsetTop - 46), behavior: 'smooth' })
  el.classList.remove('trk-flash')
  void el.offsetWidth
  el.classList.add('trk-flash')
  clearTimeout(flashTimer)
  flashTimer = setTimeout(() => el.classList.remove('trk-flash'), 1500)
}

/** Replay entry animations on the chat surfaces after a view switch. */
export const replayView = () => {
  requestAnimationFrame(() => {
    for (const id of ['trk-chathead', 'trk-projstrip', 'trk-chatlog', 'trk-page']) {
      const el = document.getElementById(id)
      if (el) {
        el.style.animation = 'none'
        void el.offsetWidth
        el.style.animation = ''
      }
    }
  })
}

export interface ConsoleActions {
  selectThread(chId: string, thId: string, direction?: 'up' | 'down'): Promise<void>
  loadOlder(): void
  onChatScroll(scrollTop: number): void
  setSysDark(dark: boolean): void
  isDark(): boolean
  toggleTheme(): void
  setTheme(theme: ThemeMode): void
  setInterfaceSize(size: InterfaceSize): void
  toggleSidebar(): void
  toggleChannelPin(id: string): void
  focusChannel(id: string): void
  toggleChannel(id: string): void
  toggleControl(): void
  setControlSection(sec: ControlSection): void
  /** show a control page, whatever was open: the address names it */
  openControl(sec: Exclude<ControlSection, ''>): void
  goChat(): void
  toggleTrace(): void
  /** fold/unfold panels when the window crosses width breakpoints */
  applyViewport(width: number): void
  /** dismiss the touch layout's overlays; an open control page keeps its rail */
  closeOverlays(): void
  setRailWidth(key: RailKey, w: number): void
  setRailDrag(key: RailKey | null): void
  toggleCardOpen(uid: string, def?: boolean): void
  toggleTimelineFold(id: string): void
  reportThreadError(threadId: string, message: string): void
  gateway(threadId: string, request: GatewayRequest): Promise<void>
  setGatewayMode(mode: ConsoleState['gatewayMode']): void
  vsOpen(): void
  resolveApproval(uid: string, verdict: 'approved' | 'dismissed'): void
  answerAsk(uid: string, answers: AskAnswer[], via: string): void
  reviewTabs(): string[]
  togglePv(): void
  openFile(name: string): void
  closeTab(name: string): void
  setView(view: 'diff' | 'clean'): void
  lineDown(i: number): void
  lineOver(i: number): void
  endSelection(): void
  plusLine(i: number): void
  clearSelection(): void
  docNums(doc: ReviewDoc): string[]
  docRange(doc: ReviewDoc, a: number, b: number): string
  quoteSelection(keep: boolean): void
  nextCommentId(): string
  setDraftText(text: string): void
  cancelDraft(): void
  queueComment(): void
  removeQueued(qid: string): void
  toggleComment(id: string): void
  applyRev(ids: string[]): void
  setComposer(text: string): void
  sendDirective(text: string): void
  sendReview(text: string): void
  startNewThread(): void
  setNtRoute(
    patch: Partial<Pick<ConsoleState, 'ntAgent' | 'ntModel' | 'ntEffort' | 'ntRouteId'>>,
  ): void
  setNtMenu(menu: ConsoleState['ntMenu'], pos?: { top: number; right: number }): void
  /** file the thread being created under a project, or under none */
  setNtProject(name: string | null): void
  /** step the working agent's approval posture one along its own vocabulary */
  cyclePermissionMode(): Promise<void>
  /** choose a mode for the draft or persist it on the current conversation */
  setPermissionMode(mode: string): Promise<void>
  /** set the level the current conversation's runs ask for; null = the runtime's default */
  setEffort(effort: EffortStep | null): Promise<void>
  /** Open a shared client control, or stream a runtime command into the ledger. */
  runCommand(context: CommandContextBody, command: ApiCommandDescriptor, args: string): Promise<void>
  closeNtMenu(): void
  sendNewThread(text: string): void
  /** drop what the last operator left open — the next one boots into their own */
  signedOut(): void
}

interface ConsoleState {
  // selection
  selChannel: string
  selThreadId: string
  detail: ThreadDetail | null
  /** live overlay items appended by the running turn */
  live: LedgerItem[]
  running: boolean
  /** how many trailing ledger items are rendered; scroll-top reveals more */
  ledgerN: number
  notices: Extract<LedgerItem, { kind: 'notice' }>[]

  // theme
  theme: ThemeMode
  sysDark: boolean
  interfaceSize: InterfaceSize

  // panels
  sbFold: boolean
  chFold: Record<string, boolean>
  channelFocusHistory: string[]
  chPins: string[]
  mgmtOpen: boolean
  mgmtSec: ControlSection
  traceOn: boolean | null
  widths: Partial<Record<RailKey, number>>
  railDrag: RailKey | null

  gatewayPending: { threadId: string; action: GatewayRequest['action'] } | null
  /** the gateway op the composer is expanded for, and the thread it was opened on */
  gatewayMode: { threadId: string; action: GatewayRequest['action'] } | null
  vsLaunch: boolean

  // review panel
  pvOpen: boolean
  tabs: string[] | null
  activeFile: string
  view: 'diff' | 'clean'
  docs: Record<string, ReviewDoc> | null
  cmts: ReviewComment[] | null
  cmtOpen: Record<string, boolean>
  sel: { a: number; b: number } | null
  selecting: boolean
  selMoved: boolean
  draft: { text: string } | null
  queue: QueueChip[]
  composer: string

  // fold state for chat cards, keyed by uid
  open: Record<string, boolean>
  tlFold: Record<string, boolean>

  // new thread flow
  ntOn: boolean
  ntStarted: boolean
  ntTitle: string
  ntAgent: string
  ntModel: string
  ntEffort: string
  /** exact route id ("agent:model") for the wire; null = relay default */
  ntRouteId: string | null
  /** the project a new thread will be filed under; null is a real answer —
   *  the thread is then a chat, in no project at all */
  ntProject: string | null
  /** the approval posture a new thread's first directive carries; null declares
   *  nothing and leaves the agent's configured default deciding. Held here only
   *  while the thread has no conversation row to hold it */
  ntPermissionMode: string | null
  /** the new-thread menus the relay honors: the composer's route picker, and
   *  the strip's project picker */
  ntMenu: 'sel' | 'proj' | 'perm' | null
  ntMenuPos: { top: number; right: number }

  actions: ConsoleActions
}

/** Card fold defaults for the CI story (matches the comp's `open` state). */
const defaultOpen: Record<string, boolean> = {
  cithink: true,
  ciplan: true,
  cit1: false,
  cit2: true,
  cit3: true,
  'diff-cimsg1-2': true,
}

let timers: ReturnType<typeof setTimeout>[] = []
let streamInterval: ReturnType<typeof setInterval> | undefined
let uidN = 0
const nextUid = () => `v${++uidN}`

/** Ledger items rendered per page — the latest page first, earlier pages on scroll-top. */
const LEDGER_PAGE = 40

/** Relay pages a thread opens with: the latest of each read, and two more behind
 *  them read while the reader looks. Further pages are read as scrolling nears them. */
const PRELOAD_PAGES = 3

/* Width lines the shell folds at: below TRACE_BREAK the timeline gives its
   column to the chat, below SIDEBAR_BREAK the thread tree collapses to the
   letter-rail, below PANEL_BREAK the dossier and control close outright. */
const TRACE_BREAK = 1360
const SIDEBAR_BREAK = 1120
const PANEL_BREAK = 880
let lastViewportWidth: number | null = null

const at = (ms: number, fn: () => void) => {
  timers.push(setTimeout(fn, ms))
}
const clearTurnTimers = () => {
  timers.forEach(clearTimeout)
  timers = []
  clearInterval(streamInterval)
}

const nowClock = () =>
  new Date().toLocaleTimeString('en-GB', { hour12: false })

/** Who a card the console writes is from: the signed-in account, by the name
 *  the relay files its messages under. */
const operator = () => useAuth.getState().user?.name ?? 'operator'

export const useConsole = create<ConsoleState>()((set, get) => {
  const prefs = loadChannelPrefs()
  let threadSelection = 0
  let threadTransition: ViewTransition | undefined

  const saveChannelPrefs = () => {
    const { chFold, chPins } = get()
    try {
      localStorage.setItem('trk-channels', JSON.stringify({ fold: chFold, pins: chPins }))
    } catch {
      // storage may be unavailable; prefs simply don't persist
    }
  }

  const showDetail = (detail: ThreadDetail) => {
    const recorded = new Set(detail.ledger.map(item => item.uid))
    set(s => ({ detail, live: s.live.filter(item => !recorded.has(item.uid)) }))
  }

  const push = (item: Omit<LedgerItem, 'uid'> & { uid?: string }) => {
    const withUid = { ...item, uid: item.uid ?? nextUid() } as LedgerItem
    if (withUid.kind === 'user' && withUid.userId === undefined) withUid.userId = useAuth.getState().user?.id
    if (get().detail?.ledger.some(item => item.uid === withUid.uid)) return withUid.uid
    const follow = nearChatBottom()
    set((s) => ({ live: [...s.live, withUid] }))
    if (follow) scrollChatBottom(true)
    return withUid.uid
  }

  const patchLive = (fn: (item: LedgerItem) => LedgerItem) => {
    const follow = nearChatBottom()
    set((s) => ({ live: s.live.map(fn) }))
    // Streaming grows cards through patches, not pushes; keep the tail in
    // view unless the reader has scrolled up.
    if (follow) scrollChatBottom()
  }

  const stream = (text: string, done?: () => void) => {
    const words = text.split(' ')
    let i = 0
    const uid = push({ kind: 'stream', text: '', streaming: true } as LedgerItem)
    clearInterval(streamInterval)
    streamInterval = setInterval(() => {
      i += 2
      const t = words.slice(0, i).join(' ')
      patchLive((x) => (x.uid === uid && x.kind === 'stream' ? { ...x, text: t } : x))
      if (i >= words.length) {
        clearInterval(streamInterval)
        patchLive((x) => (x.uid === uid && x.kind === 'stream' ? { ...x, streaming: false } : x))
        done?.()
      }
    }, 90)
  }

  const docsOf = (s: ConsoleState) => s.docs ?? s.detail?.docs ?? {}
  const cmtsOf = (s: ConsoleState) => s.cmts ?? s.detail?.comments ?? []

  // Threads with a ledger page in flight; one page at a time for each.
  const paging = new Set<string>()

  /** Fold one more page onto the open thread — of the read holding the other back. */
  const pageBack = async (thId: string) => {
    const reads = get().detail?.reads
    const read = reads === undefined ? null : holdingBack(reads)
    const cursor = read === null ? null : reads?.cursors[read]
    if (paging.has(thId) || get().selThreadId !== thId || read === null || !cursor) return
    paging.add(thId)
    try {
      const older = await api.olderPage(thId, read, cursor)
      const current = get().detail
      // A reader who moved on, or a refresh that replaced the pages, has no use for it.
      if (get().selThreadId !== thId || !current?.reads || current.reads.cursors[read] !== cursor) return
      const detail = withOlderPage(current, current.reads, older)
      showDetail(detail)
      queryClient.setQueryData(['thread-detail', thId], detail)
      // A reader already at the top is waiting on this page.
      const log = document.getElementById('trk-chatlog')
      if (log !== null && log.scrollTop < 40) actions.loadOlder()
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error)
      actions.reportThreadError(thId, `earlier messages did not load — ${reason}`)
    } finally {
      paging.delete(thId)
    }
  }

  /**
   * Snap the new-thread picker to the relay's default route (routes[0], the
   * channel's entry agent). Without this the chip shows the mock default and
   * ntRouteId stays null — a first directive would silently fall to the relay
   * default while the chip claims something else.
   */
  const seedRoutePicker = () => {
    void queryClient
      .fetchQuery({ queryKey: ['routes'], queryFn: api.routes, staleTime: 60_000 })
      .then(({ routes }) => {
        const first = routes[0]
        if (!first) return
        const s = get()
        if (!s.ntOn || s.ntRouteId !== null) return
        set({
          ntAgent: first.agent,
          ntModel: first.model ?? 'Harness default',
          ntRouteId: first.id,
        })
      })
  }

  // Runs streaming into each thread right now. Answering a feeler opens a second
  // one while the run that raised it is still holding the first, so a thread's
  // `running` lifts when the last of them closes rather than the first.
  const streamingOn = new Map<string, number>()
  const streamStart = (thId: string) => {
    streamingOn.set(thId, (streamingOn.get(thId) ?? 0) + 1)
    // The run's own socket carries the thread meanwhile; a watch would carry it twice.
    if (viewing?.thId === thId) {
      viewing.close()
      viewing = undefined
    }
  }
  const streamEnd = (thId: string) => {
    const left = (streamingOn.get(thId) ?? 1) - 1
    if (left > 0) streamingOn.set(thId, left)
    else streamingOn.delete(thId)
  }
  // The live overlay of a thread a run still streams into while another is open,
  // handed back when the reader returns to it.
  const parked = new Map<string, LedgerItem[]>()

  /** Your thread at `address`, if your list holds it, with both ends' details refetched. */
  const landedThread = async (from: string, address: ChannelAddress) => {
    await queryClient.invalidateQueries({ queryKey: ['thread-detail', from] })
    const threads = await fetchThreads()
    const landed = threads.find((thread) =>
      thread.channel_tentacle_id === address.channel_tentacle_id
      && thread.chat_type === address.chat_type
      && thread.chat_id === address.chat_id
      && (thread.channel_thread_id ?? null) === (address.channel_thread_id ?? null),
    )
    if (landed) await queryClient.invalidateQueries({ queryKey: ['thread-detail', landed.id] })
    return landed
  }

  /** The cards a batch's answer settles, marked wherever they were answered — in
   *  this console, another one, or on the batch's own channel. */
  const answeredBy = (event: ActionBatchAnsweredEvent) => (it: LedgerItem): LedgerItem => {
    if ((it.kind !== 'approval' && it.kind !== 'ask') || it.state !== 'waiting' || it.batchId !== event.batch_id) return it
    const t = nowClock().slice(0, 5)
    if (it.kind === 'approval') {
      if (it.actionId === undefined || !(it.actionId in event.approvals)) return it
      return { ...it, state: event.approvals[it.actionId] ? 'approved' : 'dismissed', resolvedT: t }
    }
    const questions = it.questions.map((q) => (q.actionId !== undefined && q.actionId in event.answers ? { ...q, answer: event.answers[q.actionId] } : q))
    return { ...it, state: 'answered', questions, resolvedT: t }
  }
  const markAnswered = (thId: string, event: ActionBatchAnsweredEvent) => {
    const mark = answeredBy(event)
    if (get().selThreadId === thId) {
      set((s) => ({ detail: s.detail && { ...s.detail, ledger: s.detail.ledger.map(mark) }, live: s.live.map(mark) }))
    }
    const kept = parked.get(thId)
    if (kept) parked.set(thId, kept.map(mark))
  }

  // The socket on the open thread, of whichever channel: it carries a run kicked
  // from anywhere else — the thread's own channel, another tab — into the
  // overlay. A run this console asked for streams over its own socket instead,
  // and the watch steps aside until that run ends.
  let viewing: { thId: string; close: () => void } | undefined
  import.meta.hot?.dispose(() => {
    viewing?.close()
    viewing = undefined
    clearTurnTimers()
  })

  /** Read a driven turn or one whose opening was missed, preserving a newer live fold. */
  const reloadSettled = async (thId: string, settled: () => boolean) => {
    const detail = await queryClient.fetchQuery({
      queryKey: ['thread-detail', thId],
      queryFn: () => api.getThreadDetail(thId),
      staleTime: 0,
      retry: false,
    })
    if (get().selThreadId !== thId || streamingOn.has(thId)) return
    if (settled()) set((s) => ({ detail, live: s.live.filter((item) => item.kind === 'artifact' && !detail.ledger.some((saved) => saved.uid === item.uid)), running: false }))
    else showDetail(detail)
  }

  /** Keep observed native turns in place when they settle. Driven turns also
   *  record inbound messages outside the stream, so they still read the ledger,
   *  as does a native turn whose opening was missed. */
  const watchOpen = async (thId: string) => {
    if (viewing?.thId === thId) return
    viewing?.close()
    viewing = undefined
    // A run this console started already carries the thread; its end watches it again.
    if (streamingOn.has(thId)) return
    let socket: Awaited<ReturnType<typeof watchThread>> | undefined
    const watching = { thId, close: () => socket?.close() }
    // Reserve the watch before connecting so overlapping opens share it.
    viewing = watching
    let fold: TurnFold | undefined
    const turnItems = new Set<string>()
    let reflex = false
    const shown = () => viewing === watching && get().selThreadId === thId && !streamingOn.has(thId)
    const onEvent = (event: WireEvent) => {
      if (!shown()) return
      if (event.event_kind === 'artifact_available') {
        set((s) => ({ live: withArtifact(s.live, event, s.detail?.ledger) }))
      } else if (event.event_kind === 'reflex_awake' || (event.event_kind === 'run_started' && !reflex)) {
        // A replay replaces only the unfinished turn, keeping earlier turns mounted.
        if (fold) set(s => ({ live: s.live.filter(item => !turnItems.has(item.uid)) }))
        turnItems.clear()
        reflex = event.event_kind === 'reflex_awake'
        fold = new TurnFold({
          push: (item) => {
            if (!shown()) return 'stale'
            const uid = push(item as LedgerItem)
            turnItems.add(uid)
            return uid
          },
          patch: (uid, patchObj) => {
            if (!shown()) return
            const patch = (item: LedgerItem) => item.uid === uid ? { ...item, ...patchObj } as LedgerItem : item
            if (get().detail?.ledger.some(item => item.uid === uid)) {
              set(s => ({ detail: s.detail && { ...s.detail, ledger: s.detail.ledger.map(patch) } }))
            } else patchLive(patch)
          },
          done: () => {},
        }, event.event_kind === 'run_started' ? channelMeta(event.address.channel_tentacle_id).label : undefined, [...(get().detail?.ledger ?? []), ...get().live])
        set({ running: true })
        if (!reflex) fold.feed(event)
      } else if (event.event_kind === 'reflex_asleep' || (event.event_kind === 'run_ended' && !reflex)) {
        if (fold && !reflex) {
          fold.feed(event)
          set({ running: false })
        } else void reloadSettled(thId, () => fold === undefined)
        fold = undefined
        reflex = false
      } else if (event.event_kind === 'action_batch_answered') {
        markAnswered(thId, event)
      } else {
        fold?.feed(event)
      }
    }
    try {
      socket = await watchThread({ id: thId }, onEvent)
      if (!shown()) {
        socket.close()
        return
      }
      // Closed by the relay: the next open of the thread watches it afresh.
      void socket.closed.then(() => {
        if (viewing === watching) viewing = undefined
      })
    } catch (error) {
      if (viewing !== watching) return
      viewing = undefined
      const reason = error instanceof Error ? error.message : String(error)
      actions.reportThreadError(thId, `live updates did not connect — ${reason}`)
    }
  }

  /**
   * Drive one live run: open the thread's socket, send the request, fold what
   * the socket carries into the live overlay, drop the dispatch dots on the
   * first real event, and settle `running` when the run settles. While the
   * reader is on another thread the fold keeps writing into this thread's
   * parked overlay, so returning mid-run shows the whole run so far. A turn the
   * agent moved elsewhere opens where it landed, as an operation does: at once
   * when a run starts there, else once the run settles — unless the reader is
   * elsewhere, when the run just follows it there.
   */
  const runLive = async (
    from: string,
    // The socket to watch: a Trunkline thread by the key its directives carry,
    // which a thread being composed has before it has a row, any other by its row.
    watch: { id: string } | { key: string },
    request: () => Promise<CommandOutcomeEvent | void>,
    options: {
      // What a turn that sent nothing back says; null says nothing.
      quietClose?: string | null
      // Sees every event first, and keeps the ones it answers true for.
      intercept?: (event: WireEvent) => boolean | void
      // The request may reach a run already awake, as an answer does the run
      // waiting on it, rather than wake one of its own.
      joinsAwake?: boolean
    } = {},
  ) => {
    const { quietClose = 'the run settled without a result', intercept, joinsAwake = false } = options
    // The thread the run streams into, until a run starts in another.
    let selId = from
    streamStart(selId)
    const here = () => get().selThreadId === selId
    // The run's overlay: the open panel's while its thread is shown, else parked.
    const edit = (fn: (live: LedgerItem[]) => LedgerItem[]) => {
      if (here()) set((s) => ({ live: fn(s.live) }))
      else parked.set(selId, fn(parked.get(selId) ?? []))
    }
    const add = (item: LedgerItem) => {
      if (here()) return push(item)
      const withUid = { ...item, uid: item.uid ?? nextUid() } as LedgerItem
      edit((live) => [...live, withUid])
      return withUid.uid
    }
    const moveTo = (thId: string) => {
      parked.delete(selId)
      streamEnd(selId)
      selId = thId
      streamStart(thId)
    }
    const dotsUid = push({ kind: 'dots', label: 'relay · dispatching' } as LedgerItem)
    const clearDots = () => edit((live) => live.filter((it) => it.uid !== dotsUid))
    let received = 0
    let terminal = false
    let landing: ChannelAddress | undefined
    let summoning = get().gatewayPending?.action === 'summon'
    const turnFold = () => new TurnFold({
      push: (item) => {
        clearDots()
        return add(item as LedgerItem)
      },
      patch: (uid, patchObj) => {
        const patched = (x: LedgerItem) => (x.uid === uid ? ({ ...x, ...patchObj } as LedgerItem) : x)
        if (here()) patchLive(patched)
        else edit((live) => live.map(patched))
      },
      // The graph releases its active turn after the result, before stream EOF.
      done: () => {},
    })
    let fold = turnFold()
    // The bar's numbers read the thread's recording, which only exists once a
    // run settles — so the result event itself says what the run spent and the
    // context its last request ran with, and the open panel says the run that
    // just ended rather than the thread as it stood when it was opened. A full
    // re-read (a revisit, a run kicked elsewhere settling) trues them up.
    const recordRunNumbers = (event: RunResultEvent) => {
      const spent = event.usage
      set((s) => {
        if (!s.detail) return s
        const usage = {
          input: s.detail.usage.input + spent.input_tokens,
          output: s.detail.usage.output + spent.output_tokens,
          cacheRead: s.detail.usage.cacheRead + spent.cache_read_tokens,
          cacheWrite: s.detail.usage.cacheWrite + spent.cache_write_tokens,
        }
        const read = usage.input + usage.cacheRead + usage.cacheWrite
        return {
          detail: {
            ...s.detail,
            usage: { ...usage, cacheRate: read > 0 ? usage.cacheRead / read : null },
            ctxK: event.context_tokens == null ? s.detail.ctxK : Math.round(event.context_tokens / 1000),
          },
        }
      })
    }
    const feed = (event: CommandStreamEvent) => {
      received++
      if (event.event_kind === 'artifact_available') {
        clearDots()
        edit((live) => withArtifact(live, event, here() ? get().detail?.ledger : undefined))
        return
      }
      if (event.event_kind === 'run_result' || event.event_kind === 'run_error' || event.event_kind === 'command_outcome') {
        terminal = true
      }
      if (event.event_kind === 'run_result' && here()) recordRunNumbers(event)
      fold.feed(event)
    }
    // Opening the thread a run started in; what streams meanwhile waits for it.
    let following: Promise<void> | undefined
    const held: CommandStreamEvent[] = []
    const openSummon = async () => {
      fold = turnFold()
      terminal = false
      const detail = await api.getThreadDetail(selId)
      if (here()) {
        const opening = detail.ledger.findLast((item) => item.kind === 'session-open')
        const anchor = opening ? push({ ...opening, uid: nextUid() }) : undefined
        set((s) => ({ detail: {
          ...detail,
          ledger: s.detail?.ledger ?? [],
          sessions: detail.sessions.map((session) => session.id === opening?.sessionId ? { ...session, anchor } : session),
        } }))
      }
      following = undefined
      held.splice(0).forEach(feed)
    }
    const follow = async (address: ChannelAddress) => {
      const landed = await landedThread(selId, address)
      if (landed && !here()) {
        // The reader is elsewhere: the run streams on where it landed, parked.
        moveTo(landed.id)
        fold = turnFold()
      } else if (landed && get().ntOn && address.channel_tentacle_id === 'trunkline' && address.channel_thread_id === selId) {
        const detail = await api.getThreadDetail(landed.id)
        if (here()) {
          moveTo(landed.id)
          // The live overlay already owns this panel's prompt and feedback.
          set({ selThreadId: selId, detail: { ...detail, ledger: [] }, ntOn: false, ntStarted: false, ntMenu: null, ntRouteId: null })
          void watchOpen(selId)
        }
      } else if (landed) {
        await actions.selectThread(landed.channel_tentacle_id, landed.id)
        moveTo(landed.id)
        set({ running: true })
        fold = turnFold()
      }
      following = undefined
      held.splice(0).forEach(feed)
    }
    // The reflex falling asleep, which the socket carries last.
    let settle!: () => void
    const settled = new Promise<void>((resolve) => { settle = resolve })
    // A thread holds one reflex at a time, so the run's is the first the socket
    // sees wake: anything before it ends a reflex that woke before the socket.
    let woke = joinsAwake
    const onEvent = (event: WireEvent) => {
      if (event.event_kind === 'artifact_available' && !following) {
        feed(event)
        return
      }
      if (event.event_kind === 'reflex_awake') {
        woke = true
        return
      }
      if (!woke) return
      if (event.event_kind === 'reflex_asleep') {
        settle()
        return
      }
      if (event.event_kind === 'run_ended') return
      if (event.event_kind === 'action_batch_answered') {
        markAnswered(selId, event)
        return
      }
      if (intercept?.(event)) return
      if (event.event_kind === 'gateway') {
        landing = event.destination
        summoning = event.action === 'summon'
        return
      }
      if (following) {
        held.push(event)
        return
      }
      if (event.event_kind === 'run_started') {
        const { channel_tentacle_id, channel_thread_id } = event.address
        if (summoning) {
          summoning = false
          following = openSummon()
        } else if (channel_tentacle_id !== 'trunkline' || channel_thread_id !== get().detail?.sendKey) {
          following = follow(event.address)
        }
        return
      }
      feed(event)
    }
    let socket: Awaited<ReturnType<typeof watchThread>> | undefined
    try {
      socket = await watchThread(watch, onEvent)
      const outcome = await request()
      if (outcome) feed(outcome)
      // The relay closing the socket first means the run outlived what carried it here.
      const carried = await Promise.race([settled.then(() => true), socket.closed.then(() => false)])
      await following
      const closing = !carried
        ? 'the live socket closed — the run continues on the relay'
        : received === 0 ? quietClose : 'the run settled without a result'
      if (closing !== null) fold.abort(closing, carried && received === 0 ? 'info' : 'warning')
      if (landing && here()) {
        const landed = await landedThread(selId, landing)
        if (landed && landed.id !== selId && here()) await actions.selectThread(landed.channel_tentacle_id, landed.id)
      }
      return true
    } catch (err) {
      const message = `relay error — ${err instanceof Error ? err.message : String(err)}`
      if (terminal) actions.reportThreadError(selId, message)
      else fold.abort(message, 'error')
      return false
    } finally {
      socket?.close()
      clearDots()
      streamEnd(selId)
      // The thread's last run settles it; one parked meanwhile is in the ledger now.
      if (!streamingOn.has(selId)) {
        if (here()) {
          set((s) => ({ running: false, live: s.live.map((item) => item.kind === 'stream' && item.streaming ? { ...item, streaming: false } : item) }))
          // The run's socket is gone: a thread with a row is watched again.
          if (!get().ntOn) void watchOpen(selId)
        } else parked.delete(selId)
      }
      void queryClient.invalidateQueries({ queryKey: ['thread-operations', selId] })
    }
  }

  const sendLiveDirective = (sendKey: string, selId: string, text: string) => {
    // No route on the wire: an owned thread's route is fixed (a mid-thread
    // model switch busts the KV cache); the pick only rides a thread's first
    // directive, in sendNewThread.
    set({ running: true, composer: '' })
    push({ kind: 'user', t: nowClock(), who: operator(), text } as LedgerItem)
    scrollChatBottom()
    void runLive(selId, { key: sendKey }, () => sendDirective(sendKey, { text }))
  }

  /** Resolve a deferred batch and fold the resumed run into the ledger. An
   *  answer to a run still going carries nothing back; its card says it was sent. */
  const resolveLive = (batchId: string, body: BatchResponseBody) => {
    const s = get()
    // A run still streaming into the thread carries on once answered, over the
    // socket already folding it; only a run the answer resumes streams anew.
    if (s.running) {
      void resolveBatch(batchId, body).catch((error: unknown) => {
        const reason = error instanceof Error ? error.message : String(error)
        actions.reportThreadError(s.selThreadId, `the answer did not go through — ${reason}`)
      })
      return
    }
    set({ running: true })
    // A thread being composed is watched by the key its directives carry.
    const watch = s.ntOn ? { key: s.selThreadId } : { id: s.selThreadId }
    void runLive(s.selThreadId, watch, () => resolveBatch(batchId, body), { quietClose: null, joinsAwake: true })
  }

  const actions: ConsoleActions = {
    /* ---------------------------------------------------- selection ------ */
    async selectThread(chId: string, thId: string, direction = 'down') {
      const selection = ++threadSelection
      threadTransition?.skipTransition()
      const panel = document.getElementById('trk-chathead')?.closest('main')
      panel?.style.removeProperty('view-transition-name')
      const animate = panel && get().selThreadId !== thId && !get().mgmtSec
        && typeof document.startViewTransition === 'function' && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      // A run still streaming into the thread left its overlay parked; it comes
      // back over the ledger it was built on, which a refetch would duplicate.
      const resuming = parked.has(thId) && queryClient.getQueryData(['thread-detail', thId]) !== undefined
      const update = () => {
        if (selection !== threadSelection) return
        clearTurnTimers()
        const leaving = get().selThreadId
        if (leaving !== thId && streamingOn.has(leaving)) parked.set(leaving, get().live)
        const live = parked.get(thId) ?? []
        parked.delete(thId)
        set((s) => ({
          selChannel: chId,
          selThreadId: thId,
          detail: queryClient.getQueryData<ThreadDetail>(['thread-detail', thId]) ?? null,
          live,
          running: streamingOn.has(thId),
          ledgerN: LEDGER_PAGE,
          notices: [],
          gatewayMode: null,
          ntOn: false,
          ntMenu: null,
          ntStarted: false,
          ntRouteId: null,
          mgmtSec: '',
          pvOpen: false,
          sbFold: s.pvOpen ? false : s.sbFold,
          tabs: null,
          activeFile: 'REGISTRY_CUTOVER.md',
          view: 'diff',
          docs: null,
          cmts: null,
          cmtOpen: {},
          sel: null,
          draft: null,
          queue: [],
          open: { ...defaultOpen },
          tlFold: {},
          composer: '',
        }))
        scrollChatBottom()
        if (!animate) replayView()
        void watchOpen(thId)
      }
      const refresh = async () => {
        const detail = await queryClient.fetchQuery({
          queryKey: ['thread-detail', thId],
          queryFn: () => api.getThreadDetail(thId),
          staleTime: 0,
          retry: false,
        })
        if (selection !== threadSelection || get().selThreadId !== thId) return
        showDetail(detail)
        scrollChatBottom()
        void (async () => {
          for (let page = 1; page < PRELOAD_PAGES; page++) await pageBack(thId)
        })()
      }
      if (!animate) {
        update()
        if (!resuming) await refresh()
        return
      }
      panel.style.viewTransitionName = direction === 'up' ? 'thread-lift' : 'thread-drop'
      const transition = document.startViewTransition(update)
      threadTransition = transition
      try {
        await Promise.all([
          transition.ready.catch((error: unknown) => {
            if (!(error instanceof DOMException && error.name === 'AbortError')) throw error
          }),
          transition.finished,
          transition.updateCallbackDone.then(() => (resuming ? undefined : refresh())),
        ])
      } finally {
        if (threadTransition === transition) {
          threadTransition = undefined
          panel.style.removeProperty('view-transition-name')
        }
      }
    },

    loadOlder() {
      const s = get()
      if (!s.detail) return
      // The next relay page is read while a page of cards is still in hand, so a
      // reader scrolling back seldom waits on one.
      if (s.ledgerN + LEDGER_PAGE >= s.detail.ledger.length) void pageBack(s.selThreadId)
      if (s.ledgerN >= s.detail.ledger.length) return
      // Reveal the next page above, keeping both rails anchored on the rows the
      // reader was looking at — the timeline indexes the same window, so a page
      // grows it from the top too and it drifts by the same amount.
      const held = ['trk-chatlog', 'trk-timeline'].map((id) => {
        const el = document.getElementById(id)
        return { id, height: el?.scrollHeight ?? 0, top: el?.scrollTop ?? 0 }
      })
      set((x) => ({ ledgerN: x.ledgerN + LEDGER_PAGE }))
      requestAnimationFrame(() => {
        for (const { id, height, top } of held) {
          const el = document.getElementById(id)
          if (el) el.scrollTop = el.scrollHeight - height + top
        }
      })
    },

    onChatScroll(scrollTop: number) {
      // Every scroll of the ledger, the reader's and ours alike — which is what
      // puts the timeline at the tail when a thread opens there.
      syncRails('chat')
      const s = get()
      if (s.ntOn) return
      if (scrollTop < 40) actions.loadOlder()
    },

    /* ---------------------------------------------------- theme ---------- */
    setSysDark(dark: boolean) {
      set({ sysDark: dark })
      const s = get()
      applyThemeAttr(s.theme === 'dark' || (s.theme === 'auto' && dark))
    },
    isDark() {
      const s = get()
      return s.theme === 'dark' || (s.theme === 'auto' && s.sysDark)
    },
    toggleTheme() {
      actions.setTheme(actions.isDark() ? 'light' : 'dark')
    },
    setTheme(theme: ThemeMode) {
      try {
        localStorage.setItem('trk-theme', theme)
      } catch {
        // non-persistent theme is fine
      }
      set({ theme })
      applyThemeAttr(theme === 'dark' || (theme === 'auto' && get().sysDark))
    },
    setInterfaceSize(size: InterfaceSize) {
      try {
        localStorage.setItem('trk-interface-size', size)
      } catch {
        // Match theme preferences when browser storage is unavailable.
      }
      set({ interfaceSize: size })
    },

    /* ---------------------------------------------------- panels --------- */
    toggleSidebar: () => set((s) => ({ sbFold: !s.sbFold })),
    toggleChannelPin(id: string) {
      set((s) => ({
        chPins: s.chPins.includes(id) ? s.chPins.filter((p) => p !== id) : [...s.chPins, id],
      }))
      saveChannelPrefs()
    },
    focusChannel(id: string) {
      set((s) => ({
        chFold: { ...s.chFold, [id]: false },
        channelFocusHistory: [...s.channelFocusHistory.filter((channel) => channel !== id), id],
        sbFold: false,
      }))
      saveChannelPrefs()
    },
    toggleChannel(id: string) {
      set((s) => ({
        chFold: { ...s.chFold, [id]: !s.chFold[id] },
        channelFocusHistory: [
          ...s.channelFocusHistory.filter((channel) => channel !== id),
          ...(s.chFold[id] ? [id] : []),
        ],
      }))
      saveChannelPrefs()
    },
    toggleControl() {
      set((s) => {
        const opening = !s.mgmtOpen
        const narrow = window.innerWidth < 1200
        return {
          mgmtOpen: opening,
          mgmtSec: opening ? s.mgmtSec || 'dash' : '',
          traceOn: opening && narrow ? false : s.traceOn,
        }
      })
    },
    setControlSection(sec: ControlSection) {
      set((s) => ({ mgmtSec: s.mgmtSec === sec ? '' : sec }))
      replayView()
    },
    openControl(sec: Exclude<ControlSection, ''>) {
      set({ mgmtOpen: true, mgmtSec: sec })
      replayView()
    },
    goChat() {
      set({ mgmtSec: '', mgmtOpen: false })
      setTimeout(() => {
        scrollChatBottom()
        replayView()
      }, 40)
    },
    applyViewport(width: number) {
      // Crossing-based, not absolute: each downward crossing folds, each
      // upward crossing restores the default — so a user's manual toggle
      // survives resizes until the window actually crosses a line again.
      const before = lastViewportWidth
      lastViewportWidth = width
      if (before === null) {
        // First measurement: fold whatever the initial window cannot fit.
        if (width < TRACE_BREAK) set({ traceOn: false })
        if (width < SIDEBAR_BREAK) set({ sbFold: true })
        if (width < PANEL_BREAK) set({ pvOpen: false, mgmtOpen: false, mgmtSec: '' })
        return
      }
      const crossedDown = (line: number) => before >= line && width < line
      const crossedUp = (line: number) => before < line && width >= line
      if (crossedDown(TRACE_BREAK)) set({ traceOn: false })
      if (crossedUp(TRACE_BREAK)) set({ traceOn: null })
      if (crossedDown(SIDEBAR_BREAK)) set({ sbFold: true })
      if (crossedUp(SIDEBAR_BREAK)) set({ sbFold: false })
      // Dossier and control are explicit opens; a wider window does not
      // reopen them on the user's behalf.
      if (crossedDown(PANEL_BREAK)) set({ pvOpen: false, mgmtOpen: false, mgmtSec: '' })
    },
    closeOverlays() {
      set((s) => ({
        sbFold: true,
        traceOn: false,
        mgmtOpen: s.mgmtSec ? s.mgmtOpen : false,
      }))
    },
    toggleTrace() {
      set((s) => {
        const opening = !(s.traceOn ?? true)
        const narrow = window.innerWidth < 1200
        return { traceOn: opening, mgmtOpen: opening && narrow ? false : s.mgmtOpen }
      })
    },
    setRailWidth(key: RailKey, w: number) {
      set((s) => ({ widths: { ...s.widths, [key]: w } }))
    },
    setRailDrag: (key: RailKey | null) => set({ railDrag: key }),
    toggleCardOpen(uid: string, def = false) {
      set((s) => ({ open: { ...s.open, [uid]: !(s.open[uid] ?? def) } }))
    },
    toggleTimelineFold(id: string) {
      set((s) => ({ tlFold: { ...s.tlFold, [id]: !s.tlFold[id] } }))
    },

    /* ---------------------------------------------------- surfaces ------- */
    reportThreadError(threadId, message) {
      const s = get()
      if (s.selThreadId !== threadId || s.ntOn) return
      set({ notices: [...s.notices, { kind: 'notice', uid: nextUid(), text: message, tone: 'error' }] })
      scrollChatBottom(true)
    },
    setGatewayMode: (mode) => set({ gatewayMode: mode }),
    async gateway(threadId, request) {
      if (get().gatewayPending || get().running || get().selThreadId !== threadId) return
      set({ gatewayPending: { threadId, action: request.action }, gatewayMode: null, running: true })
      try {
        // The operation's own move confirms it; the last move says where it ended.
        const completion: { confirmed?: boolean; landing?: GatewayEvent; error?: string; started?: boolean } = {}
        const completed = await runLive(threadId, { id: threadId }, () => castGateway(threadId, request), {
          intercept: (event) => {
            if (event.event_kind === 'gateway') {
              completion.confirmed ||= event.action === request.action
              completion.landing = event
              return true
            }
            if (event.event_kind === 'run_started') completion.started = true
            // Once a run starts where the move landed, a failure is that run's, shown there.
            else if (event.event_kind === 'run_error' && !completion.started) {
              completion.error = event.message
              return true
            }
          },
        })
        if (completion.error) throw new Error(completion.error)
        if (!completed) return
        if (!completion.confirmed || !completion.landing) {
          throw new Error('The run settled without confirming a destination. Check the thread before retrying.')
        }
        const landed = await landedThread(threadId, completion.landing.destination)
        if (!landed) {
          throw new Error(`${request.action} completed, but its destination is not visible in your thread list.`)
        }
        if (get().selThreadId === threadId && landed.id !== threadId) await actions.selectThread(landed.channel_tentacle_id, landed.id)
      } catch (error) {
        actions.reportThreadError(threadId, error instanceof Error ? error.message : String(error))
      } finally {
        set({ gatewayPending: null })
        void queryClient.invalidateQueries({ queryKey: ['thread-operations', threadId] })
      }
    },
    /**
     * Open this thread's directory in VS Code, then reopen the native session
     * in its own extension.
     *
     * Two links, in that order and spaced apart: the extension one is handled by
     * whichever window is frontmost, so the folder has to have arrived first.
     * `location.href` rather than a click on an anchor — the browser hands a
     * custom scheme to the OS either way, and this needs no element.
     */
    vsOpen() {
      const target = get().detail?.vscode
      if (!target || get().vsLaunch) return
      set({ vsLaunch: true })
      window.location.href = target.folder
      if (target.session) {
        const session = target.session
        // Long enough for a window that has to be opened cold, not only focused.
        at(1200, () => {
          window.location.href = session
        })
      }
      at(2200, () => set({ vsLaunch: false }))
    },

    /* ---------------------------------------------------- feelers -------- */
    resolveApproval(uid: string, verdict: 'approved' | 'dismissed') {
      if (get().detail?.kind === 'native_thread') return
      const card = [...(get().detail?.ledger ?? []), ...get().live].find(
        (it) => it.uid === uid,
      )
      // The card's own state is what says whether it can still be answered. A
      // run streaming is not: an agent that suspends inside a tool call holds
      // its stream open until the answer arrives, so waiting for the stream to
      // end is waiting for the thing the answer is what unblocks.
      if (card?.kind !== 'approval' || card.state !== 'waiting') return
      const t = nowClock().slice(0, 5)
      const mark = (it: LedgerItem): LedgerItem =>
        it.uid === uid && it.kind === 'approval' ? { ...it, state: verdict, resolvedT: t } : it
      set((s) => ({
        detail: s.detail && { ...s.detail, ledger: s.detail.ledger.map(mark) },
        live: s.live.map(mark),
      }))
      if (card.batchId && card.actionId) {
        resolveLive(card.batchId, {
          approvals: { [card.actionId]: verdict === 'approved' },
        })
      }
    },
    answerAsk(uid: string, answers: AskAnswer[], via: string) {
      if (get().detail?.kind === 'native_thread') return
      const card = [...(get().detail?.ledger ?? []), ...get().live].find(
        (it) => it.uid === uid,
      )
      if (card?.kind !== 'ask' || card.state !== 'waiting' || card.nativeCallId) return
      const t = nowClock().slice(0, 5)
      const questions = card.questions.map((q, index) => ({ ...q, answer: answers[index] }))
      const mark = (it: LedgerItem): LedgerItem =>
        it.uid === uid && it.kind === 'ask'
          ? { ...it, state: 'answered' as const, questions, via, resolvedT: t }
          : it
      set((s) => ({
        detail: s.detail && { ...s.detail, ledger: s.detail.ledger.map(mark) },
        live: s.live.map(mark),
      }))
      // The whole batch in one response, so the run resumes once.
      if (card.batchId) {
        resolveLive(card.batchId, {
          answers: Object.fromEntries(
            questions.flatMap((q) => (q.actionId ? [[q.actionId, q.answer ?? '']] : [])),
          ),
        })
      }
    },

    /* ---------------------------------------------------- review --------- */
    reviewTabs(): string[] {
      const s = get()
      return s.tabs ?? (s.detail?.artifacts?.map((a) => a.name) ?? [])
    },
    togglePv() {
      set((s) => {
        const opening = !s.pvOpen
        return { pvOpen: opening, sbFold: opening }
      })
    },
    openFile(name: string) {
      const tabs = actions.reviewTabs().slice()
      if (!tabs.includes(name)) tabs.push(name)
      set({ tabs, activeFile: name, pvOpen: true, sbFold: true, sel: null, draft: null })
    },
    closeTab(name: string) {
      const tabs = actions.reviewTabs().filter((t) => t !== name)
      set((s) => ({
        tabs,
        sel: null,
        draft: null,
        activeFile: s.activeFile === name ? (tabs[0] ?? name) : s.activeFile,
        ...(tabs.length ? {} : { pvOpen: false, sbFold: false }),
      }))
    },
    setView: (view: 'diff' | 'clean') => set({ view }),
    lineDown: (i: number) => set({ sel: { a: i, b: i }, selecting: true, selMoved: false, draft: null }),
    lineOver(i: number) {
      const s = get()
      if (!s.selecting || !s.sel) return
      set({ sel: { a: s.sel.a, b: i }, selMoved: s.selMoved || i !== s.sel.a })
    },
    endSelection() {
      const s = get()
      if (!s.selecting) return
      const moved = s.selMoved
      set({ selecting: false, selMoved: false })
      if (moved) actions.quoteSelection(true)
    },
    plusLine(i: number) {
      const s = get()
      const within = s.sel && i >= Math.min(s.sel.a, s.sel.b) && i <= Math.max(s.sel.a, s.sel.b)
      set({
        sel: within && s.sel ? s.sel : { a: i, b: i },
        selecting: false,
        selMoved: false,
        draft: { text: '' },
      })
    },
    clearSelection: () => set({ sel: null, draft: null }),
    docNums(doc: ReviewDoc): string[] {
      let n = 0
      return doc.lines.map((l) => (l.mark === 'del' ? '·' : String(++n).padStart(2, '0')))
    },
    docRange(doc: ReviewDoc, a: number, b: number): string {
      const ns = actions.docNums(doc)
      const lo = Math.min(a, b)
      const hi = Math.max(a, b)
      const va = ns[lo] === '·' ? String(lo + 1).padStart(2, '0') : ns[lo]
      const vb = ns[hi] === '·' ? String(hi + 1).padStart(2, '0') : ns[hi]
      return lo === hi ? `L${va}` : `L${va}–L${vb}`
    },
    quoteSelection(keep: boolean) {
      const s = get()
      if (!s.sel) return
      const doc = docsOf(s)[s.activeFile]
      if (!doc) return
      const lo = Math.min(s.sel.a, s.sel.b)
      const hi = Math.max(s.sel.a, s.sel.b)
      const first = doc.lines
        .slice(lo, hi + 1)
        .map((l) => l.t)
        .find((t) => t.trim()) ?? '—'
      const body = first.slice(0, 88) + (first.length > 88 || hi > lo ? ' …' : '')
      const q: QueueChip = {
        qid: `q${Date.now()}`,
        kind: 'quote',
        label: `❝ ${s.activeFile} · ${actions.docRange(doc, lo, hi)}`,
        body,
      }
      set((x) => ({ queue: [...x.queue, q], sel: keep ? x.sel : null }))
    },
    nextCommentId() {
      return `CMT_${String(cmtsOf(get()).length + 1).padStart(2, '0')}`
    },
    setDraftText: (text: string) => set((s) => (s.draft ? { draft: { ...s.draft, text } } : {})),
    cancelDraft: () => set({ draft: null }),
    queueComment() {
      const s = get()
      if (!s.sel || !s.draft || !s.draft.text.trim()) return
      const doc = docsOf(s)[s.activeFile]
      if (!doc) return
      const hi = Math.max(s.sel.a, s.sel.b)
      const id = actions.nextCommentId()
      const range = actions.docRange(doc, s.sel.a, s.sel.b)
      const line = doc.lines[hi]
      if (!line) return
      const c: ReviewComment = {
        id,
        file: s.activeFile,
        anchor: line.id,
        range,
        who: operator(),
        t: `14:0${cmtsOf(s).length + 5}`,
        status: 'queued',
        text: s.draft.text.trim(),
      }
      set((x) => ({
        cmts: [...cmtsOf(x), c],
        queue: [...x.queue, { qid: id, kind: 'cmt', ref: id, label: `${id} · ${range}`, body: c.text }],
        sel: null,
        draft: null,
      }))
    },
    removeQueued(qid: string) {
      set((s) => ({
        queue: s.queue.filter((q) => q.qid !== qid),
        cmts: cmtsOf(s).filter((c) => !(c.id === qid && c.status === 'queued')),
      }))
    },
    toggleComment(id: string) {
      set((s) => {
        const c = cmtsOf(s).find((x) => x.id === id)
        const def = !!c && c.status !== 'outdated'
        return { cmtOpen: { ...s.cmtOpen, [id]: !(s.cmtOpen[id] ?? def) } }
      })
    },

    /** Fold queued notes into REV_03 — ported verbatim from the comp. */
    applyRev(ids: string[]) {
      set((s) => {
        const docs = structuredClone(docsOf(s))
        const d = docs['REGISTRY_CUTOVER.md']
        if (!d) return {}
        const remap: Record<string, string | null> = {}
        let prev: DocLine | null = null
        const kept: DocLine[] = []
        d.lines.forEach((l) => {
          if (l.mark === 'del') {
            remap[l.id] = prev ? prev.id : null
          } else {
            l.mark = ''
            kept.push(l)
            prev = l
          }
        })
        d.lines = kept
        const edits: { at: string; adds: DocLine[] }[] = [
          {
            at: 'a20',
            adds: [
              {
                id: 'c1',
                k: 'p',
                t: 'Window opens 02:00Z Saturday with a hard stop at 04:00Z — past the stop the flip aborts and reads stay pinned to legacy.',
                mark: 'add',
              },
            ],
          },
          {
            at: 'a23',
            adds: [
              {
                id: 'c2',
                k: 'li',
                t: 'hold writes dual for two full cycles, then retire the shim behind registry_mirror',
                mark: 'add',
              },
              {
                id: 'c3',
                k: 'li',
                t: 'post-flip: relay pins this thread to slack/#cutover until parity seals',
                mark: 'add',
              },
            ],
          },
        ]
        edits.forEach((e) => {
          const i = d.lines.findIndex((l) => l.id === e.at)
          if (i < 0) return
          const target = d.lines[i]
          if (target) target.mark = 'del'
          d.lines.splice(i + 1, 0, ...e.adds)
        })
        d.rev = 'REV_03'
        const first = d.lines[0]
        if (first) first.t = 'rev: REV_03 · owner: claude · opus[1m] · thread THR-0195'
        const fix = (a: string) => (remap[a] !== undefined ? (remap[a] ?? a) : a)
        const cmts = cmtsOf(s).map((c) =>
          ids.includes(c.id)
            ? { ...c, status: 'outdated' as const, appliedIn: 'REV_03', anchor: fix(c.anchor) }
            : { ...c, anchor: fix(c.anchor) },
        )
        return {
          docs,
          cmts,
          view: 'diff' as const,
          sel: null,
          draft: null,
          activeFile: 'REGISTRY_CUTOVER.md',
          pvOpen: true,
        }
      })
      const dc = document.getElementById('mdv-doc')
      const tg = document.getElementById('ln-c1')
      if (dc && tg) dc.scrollTo({ top: Math.max(0, tg.offsetTop - 130), behavior: 'smooth' })
    },

    /* ---------------------------------------------------- sending -------- */
    setComposer: (text: string) => set({ composer: text }),

    sendDirective(text: string) {
      const s = get()
      if (s.running) {
        // A turn still running takes the message as its next prompt: it is
        // recorded behind the run, and the next turn answers it. The local
        // copy stands where it was said — after what has streamed, before the
        // reply still coming — and when the run settles the ledger is read so
        // the recorded row takes that place over.
        const live = text.trim()
        if (live && s.detail?.live && s.detail.sendKey) {
          const thId = s.selThreadId
          set({ composer: '' })
          push({ kind: 'user', t: nowClock(), who: operator(), text: live } as LedgerItem)
          scrollChatBottom()
          void sendDirective(s.detail.sendKey, { text: live }).then(
            () => {
              const unsubscribe = useConsole.subscribe((state) => {
                if (state.running) return
                unsubscribe()
                void reloadSettled(thId, () => true)
              })
            },
            (error: unknown) => {
              const reason = error instanceof Error ? error.message : String(error)
              push({ kind: 'notice', text: `the message did not go through — ${reason}`, tone: 'warning' } as LedgerItem)
            },
          )
        }
        return
      }
      if (s.ntOn) {
        actions.sendNewThread(text)
        return
      }
      if (s.detail?.kind === 'native_thread') return
      if (s.detail?.live) {
        const live = text.trim()
        if (!live) return
        if (s.detail.sendKey) {
          sendLiveDirective(s.detail.sendKey, s.selThreadId, live)
        } else {
          push({
            kind: 'notice',
            text: `read-only view — this thread lives on ${s.detail.channel ?? 'another channel'}; reply there`,
            tone: 'warning',
          } as LedgerItem)
        }
        return
      }
      if (s.pvOpen) {
        actions.sendReview(text)
        return
      }
    },

    sendReview(text: string) {
      const s = get()
      if (s.running) return
      const q = s.queue
      const body = text.trim()
      if (!q.length && !body) return
      const hasC = q.some((x) => x.kind === 'cmt')
      const ids = q.filter((x) => x.kind === 'cmt').map((x) => x.ref ?? '')
      const chips = q.map((x) => ({ ...x }))
      set((x) => ({
        running: true,
        composer: '',
        queue: [],
        sel: null,
        draft: null,
        cmts: cmtsOf(x).map((c) => (ids.includes(c.id) ? { ...c, status: 'sent' as const } : c)),
      }))
      push({
        kind: 'user',
        t: nowClock(),
        who: operator(),
        text: body || (hasC ? 'Fold these into the next rev.' : 'Pinned for reference.'),
        chips,
      } as LedgerItem)
      scrollChatBottom()
      at(400, () => push({ kind: 'dots', label: 'SES-0530 · claude · opus[1m] · working' } as LedgerItem))
      if (hasC) {
        at(1500, () => {
          set((x) => ({ live: x.live.filter((it) => it.kind !== 'dots') }))
          push({
            kind: 'think',
            dur: '1.3s',
            thinking: false,
            text: 'Notes anchor on the cutover window. Strike the loose lines, fold a hard stop and a longer shim hold into §03, stamp the notes outdated at their anchors, cut REV_03.',
          } as LedgerItem)
        })
        at(2300, () =>
          push({
            kind: 'tool',
            name: 'plan.apply_edits',
            icon: 'send',
            status: 'run',
            defaultOpen: true,
            detail: {
              type: 'plain',
              args: `file: "REGISTRY_CUTOVER.md"\nnotes: ${ids.join(', ') || '—'} · keep anchors`,
              res: '',
            },
          } as LedgerItem),
        )
        at(3500, () =>
          patchLive((x) =>
            x.kind === 'tool' && x.status === 'run'
              ? {
                  ...x,
                  status: 'done',
                  detail: {
                    type: 'plain',
                    args: `file: "REGISTRY_CUTOVER.md"\nnotes: ${ids.join(', ') || '—'} · keep anchors`,
                    res: `REV_03 applied · +3 −2\n${ids.join(', ') || 'notes'} → outdated · folded at anchor`,
                  },
                }
              : x,
          ),
        )
        at(3650, () =>
          push({
            kind: 'sub',
            id: 'SES-0534',
            route: 'diff-audit · haiku',
            note: 'strike map verified · sealed',
          } as LedgerItem),
        )
        at(3700, () => actions.applyRev(ids))
        at(4100, () =>
          push({
            kind: 'file',
            name: 'REGISTRY_CUTOVER.md',
            rev: 'REV_03',
            note: '+3 −2 vs REV_02 · notes folded · struck lines show last turn',
          } as LedgerItem),
        )
        at(4600, () =>
          stream(
            'REV_03 folded in — the window now carries a 04:00Z hard stop and the shim holds for two full cycles. Your notes are stamped outdated at their anchors; struck lines are last turn’s diff. Flip to Clean to read it straight, then mark up the next round.',
            () => {
              push({ kind: 'end', label: 'End of turn · REV_03 · +3 −2 · notes folded' } as LedgerItem)
              set({ running: false })
            },
          ),
        )
      } else {
        at(1500, () => set((x) => ({ live: x.live.filter((it) => it.kind !== 'dots') })))
        at(1550, () =>
          stream(
            'Pinned. Those lines are held as context for the next pass — leave a comment on them when you want an edit, and it lands as the next rev.',
            () => {
              push({ kind: 'end', label: 'End of turn · quoted lines pinned · no rev cut' } as LedgerItem)
              set({ running: false })
            },
          ),
        )
      }
    },

    /* ---------------------------------------------------- new thread ----- */
    startNewThread() {
      clearTurnTimers()
      // A thread being composed has no row to watch yet; its directive's run does.
      viewing?.close()
      viewing = undefined
      set({
        ntOn: true,
        ntStarted: false,
        ntTitle: '',
        ntProject: null,
        ntPermissionMode: null,
        ntEffort: 'auto',
        ntMenu: null,
        live: [],
        running: false,
        selChannel: 'trunkline',
        selThreadId: 'THR-NEW',
        mgmtSec: '',
        pvOpen: false,
        sel: null,
        draft: null,
        queue: [],
        detail: null,
        composer: '',
      })
      seedRoutePicker()
      replayView()
    },
    setNtRoute(
      patch: Partial<Pick<ConsoleState, 'ntAgent' | 'ntModel' | 'ntEffort' | 'ntRouteId'>>,
    ) {
      // A posture is one provider's word, so switching agents drops it rather
      // than sending Claude's vocabulary to Codex on the first directive.
      const dropped = patch.ntAgent !== undefined && patch.ntAgent !== get().ntAgent
      const modelChanged = patch.ntModel !== undefined && patch.ntModel !== get().ntModel
      set({ ...(dropped || modelChanged ? { ntEffort: 'auto' } : {}), ...patch, ...(dropped ? { ntPermissionMode: null } : {}) })
    },
    setNtMenu(menu: ConsoleState['ntMenu'], pos?: { top: number; right: number }) {
      set((s) => ({ ntMenu: s.ntMenu === menu ? null : menu, ...(pos ? { ntMenuPos: pos } : {}) }))
    },
    closeNtMenu: () => set({ ntMenu: null }),
    setNtProject: (name: string | null) => set({ ntProject: name }),

    /**
     * Step the working agent's approval posture one along its own vocabulary —
     * the ⇧⇥ shortcut.
     *
     * Two places hold a posture, because a thread being composed has no row to
     * hold one: before the first directive the pick lives here and rides that
     * directive, and afterwards it lives on the conversation and is written
     * there. When nothing is declared, cycling starts from the agent's default.
     *
     * An agent with no vocabulary (a native session's runtime, which is observed
     * rather than driven) cycles to nothing at all.
     */
    async cyclePermissionMode() {
      if (!get().ntOn && get().detail?.kind === 'native_thread') return
      const vocabularies = await queryClient.fetchQuery({
        queryKey: ['permission-modes'],
        queryFn: api.permissionModes,
        staleTime: 60_000,
      })
      const s = get()
      const session = s.detail?.sessions.at(-1)
      const agent = s.ntOn ? s.ntAgent : session?.agent
      if (!agent) return
      const vocabulary = vocabularies[agent]?.modes.map((mode) => mode.value) ?? []
      if (!vocabulary.length) return
      const declared = s.ntOn ? s.ntPermissionMode : (session?.mode ?? null)
      const current = declared ?? vocabularies[agent]?.default
      const next = vocabulary[(vocabulary.indexOf(current ?? '') + 1) % vocabulary.length]
      await actions.setPermissionMode(next)
    },
    async setPermissionMode(mode: string) {
      const s = get()
      if (s.ntOn) {
        set({ ntPermissionMode: mode })
        return
      }
      if (s.detail?.kind === 'native_thread') return
      const session = s.detail?.sessions.at(-1)
      if (!session) return
      await api.setPermissionMode(session.conversationId, mode)
      // The write is the relay's; this only keeps the chip in step with it. A
      // reader who has moved on since is left alone — their detail is another
      // thread's, and the posture they are looking at is not the one that moved.
      if (get().selThreadId !== s.selThreadId) return
      set((x) =>
        x.detail === null
          ? {}
          : {
              detail: {
                ...x.detail,
                sessions: x.detail.sessions.map((each) =>
                  each.conversationId === session.conversationId
                    ? { ...each, mode }
                    : each,
                ),
              },
            },
      )
    },
    async setEffort(effort: EffortStep | null) {
      const s = get()
      if (s.running || s.detail?.kind === 'native_thread') return
      if (s.ntOn) {
        set({ ntEffort: effort ?? 'auto' })
        return
      }
      const session = s.detail?.sessions.at(-1)
      if (!session) return
      const stored = await api.setEffort(session.conversationId, effort).catch((err: unknown) => {
        actions.reportThreadError(s.selThreadId, `effort not set — ${err instanceof Error ? err.message : String(err)}`)
        return null
      })
      if (stored === null || get().selThreadId !== s.selThreadId) return
      set((x) =>
        x.detail === null
          ? {}
          : {
              detail: {
                ...x.detail,
                sessions: x.detail.sessions.map((each) =>
                  each.conversationId === session.conversationId ? { ...each, effort: stored.effort } : each,
                ),
              },
              notices: [
                ...x.notices,
                { kind: 'notice', uid: nextUid(), text: `effort → ${stored.effort ?? 'auto'} · ${session.route}`, tone: 'info' },
              ],
            },
      )
      scrollChatBottom(true)
    },
    async runCommand(context: CommandContextBody, command: ApiCommandDescriptor, args: string) {
      const s = get()
      if (s.running || s.detail?.kind === 'native_thread') return
      const control = COMMAND_CONTROLS.get(command.id)?.action
      if (!control && (command.unavailable_reason || (!context.conversation_id && command.requires_conversation !== false))) return
      if (control) {
        if (args.trim()) return
        const result = await queryClient.fetchQuery({ queryKey: ['routes'], queryFn: api.routes, staleTime: 60_000 }).catch((error: unknown) => {
          actions.reportThreadError(s.selThreadId, `control unavailable — ${error instanceof Error ? error.message : String(error)}`)
          return null
        })
        if (!result || get().selThreadId !== s.selThreadId || get().ntRouteId !== s.ntRouteId || get().running) return
        const { routes } = result
        const route = routes.find((one) => one.agent === context.agent_id && one.model === context.model)
          ?? routes.find((one) => one.agent === context.agent_id)
        if (!route) {
          actions.reportThreadError(s.selThreadId, `No Trunkline route is available for ${context.agent_id}.`)
          return
        }
        const project = s.ntOn ? s.ntProject : s.detail?.project?.name ?? null
        if (!s.ntOn || s.ntStarted || control === 'new_thread') actions.startNewThread()
        set({
          ntMenu: control === 'model' ? 'sel' : control === 'new_thread' ? null : 'proj',
          ntProject: control === 'new_thread' ? null : project,
          ntAgent: route.agent,
          ntModel: route.model ?? 'Harness default',
          ntRouteId: route.id,
        })
        return
      }
      set({ running: true, composer: '' })
      push({ kind: 'user', t: nowClock(), who: operator(), text: `/${command.name}${args && ` ${args}`}` } as LedgerItem)
      const body = {
        ...context,
        command_id: command.id,
        // Opaque to the relay, which only dedupes on it; getRandomValues, unlike
        // randomUUID, is there on a plain-http origin too.
        delivery_id: Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join(''),
        arguments: args,
      }
      // A command on the composer, before its thread has a row, answers on the
      // key of the thread it would start: none, which the relay reads as such.
      const watch = context.address.channel_tentacle_id === 'trunkline'
        ? { key: context.address.channel_thread_id ?? '' }
        : { id: s.selThreadId }
      void runLive(s.selThreadId, watch, () => executeCommand(body), { quietClose: 'command closed without an outcome' })
        .finally(() => { void queryClient.invalidateQueries({ queryKey: ['command-catalog', context.agent_id] }) })
    },
    sendNewThread(text: string) {
      const s = get()
      const body = text.trim()
      if (!body || s.running) return
      const { ntStarted: started } = s
      const title = body.length > 46 ? `${body.slice(0, 46).trimEnd()}…` : body

      // A fresh key registers the thread on the relay with the first
      // directive; follow-ups continue it.
      const threadId =
        started && !s.selThreadId.startsWith('THR-')
          ? s.selThreadId
          : `trunkline-${Math.random().toString(36).slice(2, 10)}`
      set({
        running: true,
        ntMenu: null,
        composer: '',
        ntStarted: true,
        ntTitle: started ? s.ntTitle : title,
        selThreadId: threadId,
      })
      push({ kind: 'user', t: nowClock(), who: operator(), text: body } as LedgerItem)
      scrollChatBottom()
      // The pick is honored only on the first directive; after that the
      // thread's route is fixed.
      const routeId = started ? undefined : (s.ntRouteId ?? undefined)
      // Same rule for the project: a thread's is frozen when the row is written,
      // so only the directive that creates it carries one.
      const project = started ? undefined : (s.ntProject ?? undefined)
      // The posture is not frozen — it rides every directive of this flow, since
      // the console holds it until the thread is reopened and its conversation
      // starts answering for it.
      const posture = s.ntPermissionMode ?? undefined
      const effort = !started && s.ntEffort !== 'auto' ? s.ntEffort : undefined
      void runLive(threadId, { key: threadId }, () =>
        sendDirective(threadId, { text: body, model: routeId, project, permission_mode: posture, effort }),
      )

    },

    /* ---------------------------------------------------- session -------- */
    signedOut() {
      clearTurnTimers()
      set({
        selThreadId: '',
        detail: null,
        live: [],
        running: false,
        notices: [],
        ntOn: false,
        ntStarted: false,
        mgmtOpen: false,
        mgmtSec: '',
        pvOpen: false,
        queue: [],
        composer: '',
      })
    },
  }

  return {
    selChannel: 'trunkline',
    selThreadId: '',
    detail: null,
    live: [],
    running: false,
    ledgerN: LEDGER_PAGE,
    notices: [],
    theme: loadTheme(),
    sysDark: false,
    interfaceSize: loadInterfaceSize(),
    sbFold: false,
    chFold: prefs.fold,
    channelFocusHistory: [],
    chPins: prefs.pins,
    mgmtOpen: false,
    mgmtSec: '',
    traceOn: null,
    widths: {},
    railDrag: null,
    gatewayPending: null,
    gatewayMode: null,
    vsLaunch: false,
    pvOpen: false,
    tabs: null,
    activeFile: 'REGISTRY_CUTOVER.md',
    view: 'diff',
    docs: null,
    cmts: null,
    cmtOpen: {},
    sel: null,
    selecting: false,
    selMoved: false,
    draft: null,
    queue: [],
    composer: '',
    open: { ...defaultOpen },
    tlFold: {},
    ntOn: false,
    ntStarted: false,
    ntTitle: '',
    ntProject: null,
    ntPermissionMode: null,
    ntAgent: 'claude',
    ntModel: 'opus-4.1',
    ntRouteId: null,
    ntEffort: 'auto',
    ntMenu: null,
    ntMenuPos: { top: 0, right: 0 },
    actions,
  }
})
