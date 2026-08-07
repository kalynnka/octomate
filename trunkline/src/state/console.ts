/**
 * Console state — a faithful port of the design comp's logic class. One store
 * drives selection, panel layout, the live ledger, feelers, the review
 * dossier, and the turn flows. Trunkline threads run live over /api/trunkline
 * (`runLive` + `lib/api/fold.ts`); threads on channels without an API yet
 * keep the comp's review-dossier demo turn (`sendReview`).
 */
import { create } from 'zustand'
import type {
  DocLine,
  LedgerItem,
  ProjectInfo,
  QueueChip,
  ReviewComment,
  ReviewDoc,
  ThreadDetail,
} from '@/lib/api/types'
import { api, resolveBatch, streamDirective } from '@/lib/api'
import type { BatchResponseBody, WireEvent } from '@/lib/api/events'
import { shortModel } from '@/lib/api/live'
import { queryClient } from '@/lib/queryClient'
import { TurnFold } from '@/lib/api/fold'

export type ControlSection = '' | 'agents' | 'mcp' | 'users' | 'dash' | 'settings'
export type ThemeMode = 'light' | 'dark' | 'auto'
export type RailKey = 'sb' | 'mgmt' | 'trace' | 'pv'

export interface NewThreadProject {
  name: string
  path: string
  git: boolean
  note?: string
  branches: { n: string; stat: string; dirty: boolean }[]
}

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
  selectThread(chId: string, thId: string): Promise<void>
  loadOlder(): void
  onChatScroll(scrollTop: number): void
  setSysDark(dark: boolean): void
  isDark(): boolean
  toggleTheme(): void
  toggleSidebar(): void
  toggleChannelFold(id: string): void
  toggleChannelPin(id: string): void
  focusChannel(id: string, all: string[]): void
  toggleControl(): void
  setControlSection(sec: ControlSection): void
  goChat(): void
  toggleTrace(): void
  /** fold/unfold panels when the window crosses width breakpoints */
  applyViewport(width: number): void
  setRailWidth(key: RailKey, w: number): void
  setRailDrag(key: RailKey | null): void
  toggleCardOpen(uid: string, def?: boolean): void
  toggleTimelineFold(id: string): void
  toggleTeleMenu(): void
  teleport(id: string, label: string, fromLabel: string): void
  vsOpen(): void
  resolveApproval(uid: string, verdict: 'approved' | 'dismissed'): void
  answerAsk(uid: string, answer: string, via: string): void
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
  closeNtMenu(): void
  pickNtProject(p: NewThreadProject | null): void
  togglePure(): void
  registerProject(path: string): void
  pickBranch(n: string): void
  forkBranch(n: string): void
  sendNewThread(text: string): void
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
  ctxBump: number
  notices: string[]

  // theme
  theme: ThemeMode
  sysDark: boolean

  // panels
  sbFold: boolean
  chFold: Record<string, boolean>
  chPins: string[]
  mgmtOpen: boolean
  mgmtSec: ControlSection
  traceOn: boolean | null
  widths: Partial<Record<RailKey, number>>
  railDrag: RailKey | null

  // surfaces / teleport
  surface: string
  teleOpen: boolean
  teleporting: boolean
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
  ntMenu: 'proj' | 'br' | 'sel' | null
  ntMenuPos: { top: number; right: number }
  ntProjSel: NewThreadProject | null
  ntBranch: string | null
  ntPure: boolean

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

export const useConsole = create<ConsoleState>()((set, get) => {
  const prefs = loadChannelPrefs()

  const saveChannelPrefs = () => {
    const { chFold, chPins } = get()
    try {
      localStorage.setItem('trk-channels', JSON.stringify({ fold: chFold, pins: chPins }))
    } catch {
      // storage may be unavailable; prefs simply don't persist
    }
  }

  const push = (item: Omit<LedgerItem, 'uid'> & { uid?: string }) => {
    const withUid = { ...item, uid: item.uid ?? nextUid() } as LedgerItem
    set((s) => ({ live: [...s.live, withUid] }))
    scrollChatBottom(true)
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

  const refreshThreads = () => {
    void queryClient.invalidateQueries({ queryKey: ['threads'] })
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
          ntModel: shortModel(first.model),
          ntRouteId: first.id,
        })
      })
  }

  /**
   * Drive one live run: fold the SSE events into the live overlay, drop the
   * dispatch dots on the first real event, and settle `running` when the
   * stream ends. Once the user navigates away the fold goes permanently dead
   * (its cursors point at a wiped overlay) — the run keeps going server-side,
   * lands in the thread's ledger, and a re-select after it ends rehydrates.
   */
  const runLive = async (
    selId: string,
    request: (onEvent: (event: WireEvent) => void) => Promise<void>,
    quietClose = 'stream closed without a result',
  ) => {
    let dead = false
    const alive = () => {
      if (!dead && get().selThreadId !== selId) dead = true
      return !dead
    }
    const dotsUid = push({ kind: 'dots', label: 'relay · dispatching' } as LedgerItem)
    const clearDots = () => set((s) => ({ live: s.live.filter((it) => it.uid !== dotsUid) }))
    let received = 0
    const fold = new TurnFold({
      push: (item) => {
        clearDots()
        if (!alive()) return 'stale'
        return push(item as LedgerItem)
      },
      patch: (uid, patchObj) => {
        if (!alive()) return
        patchLive((x) => (x.uid === uid ? ({ ...x, ...patchObj } as LedgerItem) : x))
      },
      done: () => {
        if (alive()) set({ running: false })
      },
    })
    try {
      await request((event) => {
        received++
        fold.feed(event)
      })
      fold.abort(
        received === 0
          ? quietClose
          : 'stream closed without a result — the run continues on the relay',
      )
    } catch (err) {
      fold.abort(`relay error — ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      clearDots()
      if (get().selThreadId === selId) {
        if (dead) {
          // The user left and came back mid-run; the run has ended now, so
          // reload the ledger from the relay instead of showing a torn turn.
          void actions.selectThread(get().selChannel, selId)
        } else {
          set({ running: false })
        }
      }
      refreshThreads()
    }
  }

  const sendLiveDirective = (sendKey: string, selId: string, text: string) => {
    // No route on the wire: an owned thread's route is fixed (a mid-thread
    // model switch busts the KV cache); the pick only rides a thread's first
    // directive, in sendNewThread.
    set({ running: true, composer: '' })
    push({ kind: 'user', t: nowClock(), who: 'kalynnka', text } as LedgerItem)
    void runLive(selId, (onEvent) => streamDirective(sendKey, { text }, onEvent))
  }

  /** Resolve a deferred batch and stream the resumed run into the ledger. */
  const resolveLive = (batchId: string, body: BatchResponseBody) => {
    set({ running: true })
    void runLive(
      get().selThreadId,
      (onEvent) => resolveBatch(batchId, body, onEvent),
      'answer recorded — the resumed run reports on its home channel',
    )
  }

  const actions: ConsoleActions = {
    /* ---------------------------------------------------- selection ------ */
    async selectThread(chId: string, thId: string) {
      clearTurnTimers()
      set((s) => ({
        selChannel: chId,
        selThreadId: thId,
        detail: null,
        live: [],
        running: false,
        ledgerN: LEDGER_PAGE,
        ctxBump: 0,
        notices: [],
        ntOn: false,
        ntMenu: null,
        ntStarted: false,
        ntRouteId: null,
        surface: ['trunkline', 'slack', 'lark', 'napcat'].includes(chId) ? chId : s.surface,
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
      const detail = await api.getThreadDetail(thId)
      if (get().selThreadId !== thId) return
      set({ detail })
      scrollChatBottom()
      replayView()
    },

    loadOlder() {
      const s = get()
      if (!s.detail || s.ledgerN >= s.detail.ledger.length) return
      // Reveal the next page above, keeping the viewport anchored on the
      // rows the reader was looking at.
      const el = document.getElementById('trk-chatlog')
      const ph = el?.scrollHeight ?? 0
      const pt = el?.scrollTop ?? 0
      set((x) => ({ ledgerN: x.ledgerN + LEDGER_PAGE }))
      requestAnimationFrame(() => {
        const e2 = document.getElementById('trk-chatlog')
        if (e2) e2.scrollTop = e2.scrollHeight - ph + pt
      })
    },

    onChatScroll(scrollTop: number) {
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
      const t: ThemeMode = actions.isDark() ? 'light' : 'dark'
      try {
        localStorage.setItem('trk-theme', t)
      } catch {
        // non-persistent theme is fine
      }
      set({ theme: t })
      applyThemeAttr(t === 'dark')
    },

    /* ---------------------------------------------------- panels --------- */
    toggleSidebar: () => set((s) => ({ sbFold: !s.sbFold })),
    toggleChannelFold(id: string) {
      set((s) => ({ chFold: { ...s.chFold, [id]: !s.chFold[id] } }))
      saveChannelPrefs()
    },
    toggleChannelPin(id: string) {
      set((s) => ({
        chPins: s.chPins.includes(id) ? s.chPins.filter((p) => p !== id) : [...s.chPins, id],
      }))
      saveChannelPrefs()
    },
    focusChannel(id: string, all: string[]) {
      const fold: Record<string, boolean> = {}
      for (const c of all) fold[c] = c !== id
      set({ chFold: fold, sbFold: false })
      saveChannelPrefs()
    },
    toggleControl() {
      set((s) => {
        const opening = !s.mgmtOpen
        const narrow = window.innerWidth < 1200
        return {
          mgmtOpen: opening,
          mgmtSec: opening ? s.mgmtSec : '',
          traceOn: opening && narrow ? false : s.traceOn,
        }
      })
    },
    setControlSection(sec: ControlSection) {
      set((s) => ({ mgmtSec: s.mgmtSec === sec ? '' : sec }))
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
    toggleTeleMenu: () => set((s) => ({ teleOpen: !s.teleOpen })),
    teleport(id: string, label: string, fromLabel: string) {
      const s = get()
      if (id === s.surface) {
        set({ teleOpen: false })
        return
      }
      set({ teleOpen: false, teleporting: true })
      at(900, () => {
        const hh = new Date().toLocaleTimeString('en-GB', { hour12: false }).slice(0, 5)
        set((x) => ({
          teleporting: false,
          surface: id,
          notices: [...x.notices, `teleport → ${label} — pointer card left in ${fromLabel} · ${hh}`],
        }))
        setTimeout(() => {
          scrollChatBottom()
          replayView()
        }, 40)
      })
    },
    vsOpen() {
      if (get().vsLaunch) return
      set({ vsLaunch: true })
      at(1600, () => set({ vsLaunch: false }))
    },

    /* ---------------------------------------------------- feelers -------- */
    resolveApproval(uid: string, verdict: 'approved' | 'dismissed') {
      const t = nowClock().slice(3)
      const card = [...(get().detail?.ledger ?? []), ...get().live].find(
        (it) => it.uid === uid,
      )
      const mark = (it: LedgerItem): LedgerItem =>
        it.uid === uid && it.kind === 'approval' ? { ...it, state: verdict, resolvedT: t } : it
      const isLive = card?.kind === 'approval' && card.batchId && card.actionId
      // A live batch response launches the resumed run; don't mark the card
      // while another run streams (the click is simply ignored).
      if (isLive && get().running) return
      set((s) => ({
        detail: s.detail && { ...s.detail, ledger: s.detail.ledger.map(mark) },
        live: s.live.map(mark),
      }))
      if (isLive && card.kind === 'approval' && card.batchId && card.actionId) {
        resolveLive(card.batchId, {
          approvals: { [card.actionId]: verdict === 'approved' },
        })
      }
    },
    answerAsk(uid: string, answer: string, via: string) {
      const t = nowClock().slice(3)
      const card = [...(get().detail?.ledger ?? []), ...get().live].find(
        (it) => it.uid === uid,
      )
      const mark = (it: LedgerItem): LedgerItem =>
        it.uid === uid && it.kind === 'ask'
          ? { ...it, state: 'answered' as const, answer, via, resolvedT: t }
          : it
      const isLive = card?.kind === 'ask' && card.batchId && card.actionId
      if (isLive && get().running) return
      set((s) => ({
        detail: s.detail && { ...s.detail, ledger: s.detail.ledger.map(mark) },
        live: s.live.map(mark),
      }))
      if (isLive && card.kind === 'ask' && card.batchId && card.actionId) {
        resolveLive(card.batchId, { answers: { [card.actionId]: answer } })
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
        who: 'kalynnka',
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
      if (s.running) return
      if (s.ntOn) {
        actions.sendNewThread(text)
        return
      }
      if (s.detail?.live) {
        const live = text.trim()
        if (!live) return
        if (s.detail.sendKey) {
          sendLiveDirective(s.detail.sendKey, s.selThreadId, live)
        } else {
          push({
            kind: 'notice',
            text: `read-only view — this thread lives on ${s.detail.channel ?? 'another channel'}; reply there`,
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
        who: 'kalynnka',
        text: body || (hasC ? 'Fold these into the next rev.' : 'Pinned for reference.'),
        chips,
      } as LedgerItem)
      at(400, () => push({ kind: 'dots', label: 'SES-0530 · claude · opus[1m] · working' } as LedgerItem))
      if (hasC) {
        at(1500, () => {
          set((x) => ({ live: x.live.filter((it) => it.kind !== 'dots') }))
          push({
            kind: 'think',
            dur: '1.3s',
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
      set({
        ntOn: true,
        ntStarted: false,
        ntTitle: '',
        ntProjSel: null,
        ntBranch: null,
        ntPure: false,
        ntMenu: null,
        live: [],
        running: false,
        selChannel: 'trunkline',
        selThreadId: 'THR-NEW',
        surface: 'trunkline',
        mgmtSec: '',
        pvOpen: false,
        sel: null,
        draft: null,
        queue: [],
        ctxBump: 0,
        detail: null,
        composer: '',
      })
      seedRoutePicker()
      replayView()
    },
    setNtRoute(
      patch: Partial<Pick<ConsoleState, 'ntAgent' | 'ntModel' | 'ntEffort' | 'ntRouteId'>>,
    ) {
      set(patch)
    },
    setNtMenu(menu: ConsoleState['ntMenu'], pos?: { top: number; right: number }) {
      set((s) => ({ ntMenu: s.ntMenu === menu ? null : menu, ...(pos ? { ntMenuPos: pos } : {}) }))
    },
    closeNtMenu: () => set({ ntMenu: null }),
    pickNtProject(p: NewThreadProject | null) {
      set({
        ntProjSel: p ? structuredClone(p) : null,
        ntBranch: p && p.git && p.branches.length ? (p.branches[0]?.n ?? null) : null,
        ntMenu: null,
        ntPure: p === null,
      })
    },
    togglePure() {
      set((s) => ({ ntPure: !s.ntPure, ntProjSel: null, ntBranch: null, ntMenu: null }))
    },
    registerProject(path: string) {
      const clean = path.trim()
      if (!clean) return
      const name = clean.replace(/\/+$/, '').split('/').pop() || 'untitled'
      set({
        ntProjSel: {
          name,
          path: clean,
          git: true,
          note: 'new tree — git init runs when the session boots',
          branches: [{ n: 'main', stat: 'init · no commits', dirty: false }],
        },
        ntBranch: 'main',
        ntMenu: null,
      })
    },
    pickBranch: (n: string) => set({ ntBranch: n, ntMenu: null }),
    forkBranch(n: string) {
      const name = n.trim()
      if (!name) return
      set((s) => {
        if (!s.ntProjSel) return {}
        if (s.ntProjSel.branches.some((b) => b.n === name)) return { ntBranch: name, ntMenu: null }
        const br = { n: name, stat: `new · forks ${s.ntBranch ?? 'main'}`, dirty: false }
        return {
          ntProjSel: { ...s.ntProjSel, branches: [br, ...s.ntProjSel.branches] },
          ntBranch: name,
          ntMenu: null,
        }
      })
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
      push({ kind: 'user', t: nowClock(), who: 'kalynnka', text: body } as LedgerItem)
      // The pick is honored only on the first directive; after that the
      // thread's route is fixed.
      const routeId = started ? undefined : (s.ntRouteId ?? undefined)
      void runLive(threadId, (onEvent) =>
        streamDirective(threadId, { text: body, model: routeId }, onEvent),
      )

    },
  }

  return {
    selChannel: 'trunkline',
    selThreadId: '',
    detail: null,
    live: [],
    running: false,
    ledgerN: LEDGER_PAGE,
    ctxBump: 0,
    notices: [],
    theme: loadTheme(),
    sysDark: false,
    sbFold: false,
    chFold: prefs.fold,
    chPins: prefs.pins,
    mgmtOpen: false,
    mgmtSec: '',
    traceOn: null,
    widths: {},
    railDrag: null,
    surface: 'trunkline',
    teleOpen: false,
    teleporting: false,
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
    ntAgent: 'claude',
    ntModel: 'opus-4.1',
    ntRouteId: null,
    ntEffort: 'high',
    ntMenu: null,
    ntMenuPos: { top: 0, right: 0 },
    ntProjSel: null,
    ntBranch: null,
    ntPure: false,
    actions,
  }
})

export type { ProjectInfo }
