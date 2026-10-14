import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, createServerModuleRunner, type ViteDevServer } from 'vite'
import type { ApiThread, ApiThreadMessage, WireDeferredQuestion, WireEvent } from '../src/lib/api/events.ts'
import type { OlderPage } from '../src/lib/api/live.ts'
import type { LedgerItem, ThreadDetail } from '../src/lib/api/types.ts'
import { LiveSocket, installLiveSocket } from './liveSocket.ts'

let server: ViteDevServer
let api: typeof import('../src/lib/api/index.ts').api
let batchFeelers: typeof import('../src/lib/api/fold.ts').batchFeelers
let liveThreadDetail: typeof import('../src/lib/api/live.ts').liveThreadDetail
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let useAuth: typeof import('../src/state/auth.ts').useAuth
let callbackResult: void | Promise<void>
const originalGlobals = ['document', 'window', 'requestAnimationFrame', 'WebSocket', 'location'].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
const detail: ThreadDetail = {
  key: 'thread-a', msgCount: 0, sessions: [], ledger: [], ctxK: 0,
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null },
}
const result: WireEvent = { event_kind: 'run_result', output: 'Hello back', usage: { requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 } }
const tick = () => new Promise((resolve) => setTimeout(resolve, 0))

/** The live socket opened on `query`, once the console has opened it. */
async function socketOn(query: string): Promise<LiveSocket> {
  for (let at = 0; at < 200; at++) {
    const socket = LiveSocket.opened.find((one) => one.url.endsWith(`?${query}`) && !one.closed)
    if (socket) return socket
    await tick()
  }
  throw new Error(`no live socket opened on ${query}`)
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ api } = await server.ssrLoadModule('/src/lib/api/index.ts'))
  ;({ batchFeelers } = await server.ssrLoadModule('/src/lib/api/fold.ts'))
  ;({ liveThreadDetail } = await server.ssrLoadModule('/src/lib/api/live.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ useAuth } = await server.ssrLoadModule('/src/state/auth.ts'))
})
beforeEach(() => {
  const panel = { style: { removeProperty() {}, viewTransitionName: '' } }
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    getElementById: (id: string) => id === 'trk-chathead' ? { closest: () => panel } : null,
    startViewTransition(update: () => void | Promise<void>) {
      callbackResult = update()
      const updateCallbackDone = Promise.resolve(callbackResult)
      let skip!: (reason: DOMException) => void
      const ready = new Promise<void>((resolve, reject) => { skip = reject; queueMicrotask(resolve) })
      return { ready, updateCallbackDone, finished: updateCallbackDone, skipTransition() { skip(new DOMException('Transition was skipped', 'AbortError')) } }
    },
  } })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { matchMedia: () => ({ matches: false }) } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  installLiveSocket()
  useConsole.setState({ selThreadId: 'other', detail: null, mgmtSec: '' })
})
afterEach(() => {
  mock.restoreAll()
  queryClient.clear()
  for (const [key, descriptor] of originalGlobals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
})
after(async () => { await server?.close() })

test('revisiting a thread displays cached detail before its refresh finishes', async () => {
  let resolve!: (value: ThreadDetail) => void
  const response = new Promise<ThreadDetail>((done) => { resolve = done })
  mock.method(api, 'getThreadDetail', () => response)
  queryClient.setQueryData(['thread-detail', 'thread-a'], detail)
  const switching = useConsole.getState().actions.selectThread('codex', 'thread-a')
  assert.equal(callbackResult, undefined, 'the animation callback must not await the network')
  assert.deepEqual(useConsole.getState().detail, detail)
  resolve({ ...detail, msgCount: 2 })
  await switching
  assert.equal(useConsole.getState().detail?.msgCount, 2)
  assert.equal(queryClient.getQueryData<ThreadDetail>(['thread-detail', 'thread-a'])?.msgCount, 2)
})

test('a first visit starts its transition without waiting for data', async () => {
  let resolve!: (value: ThreadDetail) => void
  const response = new Promise<ThreadDetail>((done) => { resolve = done })
  mock.method(api, 'getThreadDetail', () => response)
  const switching = useConsole.getState().actions.selectThread('codex', 'thread-a')
  assert.equal(callbackResult, undefined)
  assert.equal(useConsole.getState().selThreadId, 'thread-a')
  assert.equal(useConsole.getState().detail, null)
  resolve(detail)
  await switching
  assert.deepEqual(useConsole.getState().detail, detail)
})

test('skipping a pending transition handles its rejection and keeps the newly selected thread', async () => {
  let resolve!: (value: ThreadDetail) => void
  const first = new Promise<ThreadDetail>((done) => { resolve = done })
  mock.method(api, 'getThreadDetail', (id: string) => id === 'thread-a' ? first : Promise.resolve({ ...detail, key: id }))
  const switching = useConsole.getState().actions.selectThread('codex', 'thread-a')
  await useConsole.getState().actions.selectThread('codex', 'thread-b')
  resolve(detail)
  await switching
  assert.equal(useConsole.getState().selThreadId, 'thread-b')
  assert.equal(useConsole.getState().detail?.key, 'thread-b')
})

test('a new thread adopts its saved identity without a transition or replacing its live messages', async () => {
  let sendKey = ''
  const saved: ApiThread = {
    id: 'saved-thread', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
    channel_tentacle_id: 'trunkline', channel_thread_id: null, title: 'hello',
    project_id: null, status: 'active', created_at: '', updated_at: '', handoffs: [],
  }
  const fetch = mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method !== 'POST') return Response.json([{ ...saved, channel_thread_id: sendKey }])
    sendKey ||= useConsole.getState().selThreadId
    const events: WireEvent[] = [
      { event_kind: 'reflex_awake' },
      { event_kind: 'run_started', address: {
        channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: sendKey, shared: false,
      } },
      { event_kind: 'run_result', output: 'Hello back', usage: { requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 } },
      { event_kind: 'reflex_asleep' },
    ]
    queueMicrotask(() => LiveSocket.last().deliver(...events))
    return new Response(null, { status: 202 })
  })
  mock.method(api, 'getThreadDetail', async () => ({
    ...detail, key: 'saved-thread', live: true, sendKey,
    sessions: [{ n: '1', id: 'session', conversationId: 'conversation', route: 'codex', agent: 'codex', model: null, effort: 'high', mode: null, kind: 'entry', t: '', reason: '', status: 'active', tone: 'accent' }],
    ledger: [{ kind: 'user', uid: 'persisted-prompt', text: 'hello', t: '', who: 'operator' }],
  } satisfies ThreadDetail))
  const transition = mock.method(document, 'startViewTransition')
  useConsole.setState({ ntOn: true, ntStarted: false, ntRouteId: 'codex:', ntProject: null, ntPermissionMode: null, ntEffort: 'high', selThreadId: 'THR-NEW', live: [], running: false })
  const actions = useConsole.getState().actions
  actions.sendDirective('hello')
  const prompt = useConsole.getState().live.find((item) => item.kind === 'user')
  for (let at = 0; at < 200 && useConsole.getState().running; at++) await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(useConsole.getState().running, false)
  assert.equal(transition.mock.callCount(), 0)
  assert.equal(useConsole.getState().ntOn, false)
  assert.equal(useConsole.getState().selThreadId, saved.id)
  assert.equal(useConsole.getState().detail?.sessions[0].conversationId, 'conversation')
  assert.equal(useConsole.getState().detail?.sessions[0].effort, 'high')
  assert.deepEqual(useConsole.getState().detail?.ledger, [])
  assert.equal(useConsole.getState().live.find((item) => item.kind === 'user'), prompt)
  assert.equal(useConsole.getState().live.filter((item) => item.kind === 'stream').length, 1)

  actions.sendDirective('continue')
  for (let at = 0; at < 200 && useConsole.getState().running; at++) await new Promise((resolve) => setTimeout(resolve, 0))
  const sent = fetch.mock.calls.filter((call) => call.arguments[1]?.method === 'POST')
  assert.equal(sent.length, 2)
  assert.equal(String(sent[0].arguments[0]), String(sent[1].arguments[0]))
  // Both turns were watched on the key the thread was composed under, before and after it had a row;
  // once it had one, the open thread was watched by it between turns and after them.
  const byKey = `ws://console.test/api/trunkline/live?thread_key=${sendKey}`
  const byId = 'ws://console.test/api/trunkline/live?thread_id=saved-thread'
  assert.deepEqual(LiveSocket.opened.map((socket) => socket.url), [byKey, byId, byKey, byId])
  assert.equal(transition.mock.callCount(), 0)
  assert.deepEqual(useConsole.getState().live.filter((item) => item.kind === 'user').map((item) => item.text), ['hello', 'continue'])
  assert.equal(useConsole.getState().live.filter((item) => item.kind === 'stream').length, 2)
  assert.equal(useConsole.getState().running, false)
})

const thread: ApiThread = {
  id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner', channel_tentacle_id: 'trunkline',
  channel_thread_id: 'trunkline-a', title: null, project_id: null, status: 'active',
  created_at: '2026-10-09T10:00:00Z', updated_at: '2026-10-09T10:00:00Z', handoffs: [],
  active_agent_tentacle_id: 'claude',
}
const said = (id: string): ApiThreadMessage => ({
  kind: 'message', id, thread_id: 'thread-a', platform_message_id: null, reply_id: '', happened_at: '2026-10-09T10:00:00Z',
  direction: 'inbound', actor_kind: 'human', agent_tentacle_id: null, sender: null, segments: [],
  message_text: id, created_at: '2026-10-09T10:00:00Z',
})
const opened = (unbound: ApiThreadMessage[], cursor: string | null) =>
  liveThreadDetail({
    thread, modelMessages: [], unbound, cursors: { model: null, unbound: cursor }, totals: { model: 0, unbound: 4 },
    conversations: [], project: null, batches: [], usage: {},
  })
const older = (id: string, cursor: string | null): OlderPage => ({
  read: 'unbound', page: { items: [said(id)], total: 4, next_cursor: cursor ?? 'end', has_more: cursor !== null },
})
const settle = async () => {
  for (let tick = 0; tick < 5; tick++) await new Promise((done) => setTimeout(done, 0))
}

test('a thread opens on three pages and leaves the rest to scrolling', async () => {
  const pages: Record<string, OlderPage> = { c4: older('m3', 'c3'), c3: older('m2', 'c2'), c2: older('m1', null) }
  const asked: string[] = []
  mock.method(api, 'getThreadDetail', () => Promise.resolve(opened([said('m4')], 'c4')))
  mock.method(api, 'olderPage', (_: string, read: string, cursor: string) => {
    asked.push(`${read}:${cursor}`)
    return Promise.resolve(pages[cursor])
  })
  await useConsole.getState().actions.selectThread('trunkline', 'thread-a')
  await settle()
  assert.deepEqual(asked, ['unbound:c4', 'unbound:c3'])
  const held = useConsole.getState().detail
  assert.deepEqual(held?.ledger.map((item) => item.kind === 'user' && item.text), ['m2', 'm3', 'm4'])
  assert.equal(held?.earlier, true)
  // A revisit opens on what was read, not on the first page alone.
  assert.equal(queryClient.getQueryData<ThreadDetail>(['thread-detail', 'thread-a'])?.ledger.length, 3)
})

test('a page landing after the reader moved on is dropped', async () => {
  let land!: (page: OlderPage) => void
  mock.method(api, 'getThreadDetail', (id: string) =>
    Promise.resolve(id === 'thread-a' ? opened([said('m4')], 'c4') : { ...detail, key: id }))
  mock.method(api, 'olderPage', () => new Promise((done) => { land = done }))
  await useConsole.getState().actions.selectThread('trunkline', 'thread-a')
  await useConsole.getState().actions.selectThread('codex', 'thread-b')
  land(older('m3', null))
  await settle()
  assert.equal(useConsole.getState().detail?.key, 'thread-b')
  assert.equal(queryClient.getQueryData<ThreadDetail>(['thread-detail', 'thread-a'])?.ledger.length, 1)
})

test('a run left streaming writes on while the reader is away, and is there on returning', async () => {
  const live = (id: string): ThreadDetail => ({ ...detail, key: id, live: true, sendKey: `${id}-key` })
  mock.method(api, 'getThreadDetail', async (id: string) => live(id))
  mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, init?: RequestInit) =>
    init?.method === 'POST' ? new Response(null, { status: 202 }) : Response.json([]))
  for (const id of ['thread-a', 'thread-b']) queryClient.setQueryData(['thread-detail', id], live(id))
  const actions = useConsole.getState().actions
  await actions.selectThread('trunkline', 'thread-a')
  actions.sendDirective('carry on')
  const run = await socketOn('thread_key=thread-a-key')

  await actions.selectThread('trunkline', 'thread-b')
  run.deliver({ event_kind: 'reflex_awake' }, result)
  assert.equal(useConsole.getState().live.length, 0)
  await actions.selectThread('trunkline', 'thread-a')

  assert.ok(useConsole.getState().live.some((item) => item.kind === 'stream' && item.text === 'Hello back'))
  assert.equal(useConsole.getState().running, true)
  run.deliver({ event_kind: 'reflex_asleep' })
  for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()
  assert.equal(useConsole.getState().running, false)
})

test('the open thread shows a run kicked elsewhere, and reads its ledger again once it settles', async () => {
  const settled: ThreadDetail = { ...detail, key: 'watched', ledger: [{ kind: 'user', uid: 'from-slack', text: 'asked on slack', t: '', who: 'someone' } as LedgerItem] }
  const opening: ThreadDetail = { ...detail, key: 'watched' }
  const getDetail = mock.method(api, 'getThreadDetail', async () => opening)
  queryClient.setQueryData(['thread-detail', 'watched'], opening)
  await useConsole.getState().actions.selectThread('slack', 'watched')
  const watching = await socketOn('thread_id=watched')

  watching.deliver({ event_kind: 'reflex_awake' }, result)
  assert.equal(useConsole.getState().running, true)
  assert.ok(useConsole.getState().live.some((item) => item.kind === 'stream' && item.text === 'Hello back'))
  getDetail.mock.mockImplementation(async () => settled)
  watching.deliver({ event_kind: 'reflex_asleep' })
  for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()

  assert.equal(useConsole.getState().running, false)
  assert.deepEqual(useConsole.getState().live, [])
  assert.deepEqual(useConsole.getState().detail?.ledger, settled.ledger)
})

test('a card answered anywhere else stops waiting here', async () => {
  const question: WireDeferredQuestion = {
    id: 'first', kind: 'question', status: 'pending', tool_name: 'AskUserQuestion', tool_call_id: 'q', position: 0,
    args: { question: 'Ship it?', choices: ['Yes', 'No'] },
  }
  const [card] = batchFeelers('batch', [question], [])
  const asking: ThreadDetail = { ...detail, key: 'asking', ledger: [{ ...card, uid: 'card' } as LedgerItem] }
  mock.method(api, 'getThreadDetail', async () => asking)
  queryClient.setQueryData(['thread-detail', 'asking'], asking)
  await useConsole.getState().actions.selectThread('slack', 'asking')
  const watching = await socketOn('thread_id=asking')

  watching.deliver({ event_kind: 'action_batch_answered', batch_id: 'batch', approvals: {}, answers: { first: 'Yes' } })

  const [answered] = useConsole.getState().detail?.ledger ?? []
  assert.equal(answered.kind === 'ask' && answered.state, 'answered')
  assert.deepEqual(answered.kind === 'ask' && answered.questions.map((q) => q.answer), ['Yes'])
})

test('a run streaming on the open thread comes over its own socket alone, and the watch returns after', async () => {
  const live: ThreadDetail = { ...detail, key: 'solo', live: true, sendKey: 'solo-key' }
  mock.method(api, 'getThreadDetail', async () => live)
  mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, init?: RequestInit) =>
    init?.method === 'POST' ? new Response(null, { status: 202 }) : Response.json([]))
  queryClient.setQueryData(['thread-detail', 'solo'], live)
  const actions = useConsole.getState().actions
  await actions.selectThread('trunkline', 'solo')
  const watching = await socketOn('thread_id=solo')

  actions.sendDirective('carry on')
  const run = await socketOn('thread_key=solo-key')
  assert.ok(watching.closed)
  run.deliver({ event_kind: 'reflex_awake' }, result, { event_kind: 'reflex_asleep' })
  for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()

  assert.ok(run.closed)
  assert.notEqual(await socketOn('thread_id=solo'), watching)
})

test('a run follows the first reflex its socket sees wake, not the end of one already awake', async () => {
  const live: ThreadDetail = { ...detail, key: 'late', live: true, sendKey: 'late-key' }
  mock.method(api, 'getThreadDetail', async () => live)
  mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, init?: RequestInit) =>
    init?.method === 'POST' ? new Response(null, { status: 202 }) : Response.json([]))
  queryClient.setQueryData(['thread-detail', 'late'], live)
  const actions = useConsole.getState().actions
  await actions.selectThread('trunkline', 'late')

  actions.sendDirective('carry on')
  const run = await socketOn('thread_key=late-key')
  // The end of a reflex that woke before the socket opened, then the run's own.
  run.deliver({ ...result, output: 'Not this one' }, { event_kind: 'reflex_asleep' })
  await tick()
  assert.equal(useConsole.getState().running, true)
  run.deliver({ event_kind: 'reflex_awake' }, result, { event_kind: 'reflex_asleep' })
  for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()

  const replies = useConsole.getState().live.filter((item) => item.kind === 'stream')
  assert.deepEqual(replies.map((item) => item.kind === 'stream' && item.text), ['Hello back'])
})

test('opening the same thread twice during connection renders each event once', async () => {
  const id = 'overlapping-watch'
  mock.method(api, 'getThreadDetail', async () => ({ ...detail, key: id }))
  const actions = useConsole.getState().actions
  await Promise.all([
    actions.selectThread('codex-native', id),
    actions.selectThread('codex-native', id),
  ])
  const sockets = LiveSocket.opened.filter(socket => !socket.closed)
  const events: WireEvent[] = [
    { event_kind: 'run_started', address: {
      channel_tentacle_id: 'codex-native', chat_type: 'thread', chat_id: id, user_id: 'owner', channel_thread_id: null, shared: false,
    } },
    { event_kind: 'part_start', index: 0, part: { part_kind: 'text', content: 'Checking the watcher.' } },
    { event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_call_id: 'call-1', tool_name: 'exec', args: '{}' } },
    { event_kind: 'function_tool_result', part: { part_kind: 'tool-return', tool_call_id: 'call-1', tool_name: 'exec', content: 'Done' } },
  ]
  for (const event of events) {
    for (const socket of sockets) socket.deliver(event)
  }
  const live = useConsole.getState().live
  assert.deepEqual(live.filter(item => item.kind === 'stream').map(item => item.text), ['Checking the watcher.'])
  assert.equal(live.filter(item => item.kind === 'tool').length, 1)
  assert.equal(sockets.length, 1)
})

test('a failed connection releases the watch so the thread can be opened again', async () => {
  const id = 'failed-watch'
  mock.method(api, 'getThreadDetail', async () => ({ ...detail, key: id }))
  const actions = useConsole.getState().actions
  const opening = actions.selectThread('codex-native', id)
  LiveSocket.last().close()
  await opening
  await actions.selectThread('codex-native', id)
  assert.equal(LiveSocket.opened.length, 2)
  assert.deepEqual(LiveSocket.opened.map(socket => socket.closed), [true, false])
})

test('a superseded connection cannot resume watching when the reader returns', async () => {
  mock.method(api, 'getThreadDetail', async (id: string) => ({ ...detail, key: id }))
  const actions = useConsole.getState().actions
  await Promise.all([
    actions.selectThread('codex-native', 'returning-watch'),
    actions.selectThread('codex-native', 'between-watches'),
    actions.selectThread('codex-native', 'returning-watch'),
  ])
  const sockets = LiveSocket.opened
  assert.equal(sockets.length, 3)
  assert.deepEqual(sockets.map(socket => socket.closed), [true, true, false])
  const started: WireEvent = { event_kind: 'run_started', address: {
    channel_tentacle_id: 'codex-native', chat_type: 'thread', chat_id: 'returning-watch', user_id: 'owner', channel_thread_id: null, shared: false,
  } }
  sockets[2].deliver(started, result)
  sockets[0].deliver(started, { ...result, output: 'Stale reply' })
  assert.deepEqual(useConsole.getState().live.filter(item => item.kind === 'stream').map(item => item.text), ['Hello back'])
})

for (const connected of [false, true]) {
  test(`hot disposal releases a ${connected ? 'connected' : 'connecting'} thread watch`, async () => {
    const runner = createServerModuleRunner(server.environments.ssr)
    const { useConsole: store } = await runner.import<typeof import('../src/state/console.ts')>('/src/state/console.ts')
    const { api: source } = await runner.import<typeof import('../src/lib/api/index.ts')>('/src/lib/api/index.ts')
    const { queryClient: cache } = await runner.import<typeof import('../src/lib/queryClient.ts')>('/src/lib/queryClient.ts')
    try {
      mock.method(source, 'getThreadDetail', async () => detail)
      const selecting = store.getState().actions.selectThread('codex-native', 'hot-thread')
      const socket = LiveSocket.last()
      if (connected) await selecting
      assert.ok(runner.hmrClient)
      await runner.hmrClient.prunePaths(['/src/state/console.ts'])
      await selecting
      await tick()
      assert.equal(socket.closed, true)
      socket.deliver({ event_kind: 'run_started', address: {
        channel_tentacle_id: 'codex-native', chat_type: 'thread', chat_id: 'hot-thread', user_id: 'owner', channel_thread_id: null, shared: false,
      } }, result)
      assert.deepEqual(store.getState().live, [])
      assert.equal(store.getState().running, false)
    } finally {
      cache.clear()
      await runner.close()
    }
  })
}

for (const reflex of [false, true]) {
  test(`an open thread settles a ${reflex ? 'reflex only on reflex_asleep' : 'tailed turn on run_ended'}`, async () => {
    const id = `native-${reflex}`
    const opening = { ...detail, key: id }
    const settled = { ...opening, msgCount: 2 }
    const getDetail = mock.method(api, 'getThreadDetail', async () => opening)
    queryClient.setQueryData(['thread-detail', id], opening)
    await useConsole.getState().actions.selectThread('claude', id)
    const watching = await socketOn(`thread_id=${id}`)
    const calls = getDetail.mock.callCount()
    const started: WireEvent = { event_kind: 'run_started', address: {
      channel_tentacle_id: 'claude', chat_type: 'thread', chat_id: id, user_id: 'owner', channel_thread_id: null, shared: false,
    } }
    if (reflex) watching.deliver({ event_kind: 'reflex_awake' })
    watching.deliver(started, result)
    assert.equal(useConsole.getState().running, true)
    assert.ok(useConsole.getState().live.some((item) => item.kind === 'stream' && item.text === 'Hello back'))
    const visible = useConsole.getState().live
    getDetail.mock.mockImplementation(async () => settled)
    watching.deliver({ event_kind: 'run_ended', thread_message_id: 'reply' })
    if (reflex) {
      await tick()
      assert.equal(getDetail.mock.callCount(), calls)
      assert.equal(useConsole.getState().running, true)
      watching.deliver(started, { ...result, output: 'Second run' }, { event_kind: 'run_ended', thread_message_id: 'second' })
      assert.equal(getDetail.mock.callCount(), calls)
      watching.deliver({ event_kind: 'reflex_asleep' })
    }
    for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()
    assert.equal(getDetail.mock.callCount(), calls + (reflex ? 1 : 0))
    assert.equal(useConsole.getState().running, false)
    if (reflex) assert.deepEqual(useConsole.getState().live, [])
    else assert.equal(useConsole.getState().live[0], visible[0])
    assert.equal(useConsole.getState().detail?.msgCount, reflex ? 2 : 0)
  })
}

for (const saved of [false, true]) {
  test(`a re-streamed tailed turn replaces its fold with the prompt ${saved ? 'already saved' : 'still live'}`, async () => {
    const sender = { id: 'profile', channel_tentacle_id: 'claude-native', channel_user_id: 'test', name: 'test', user_id: 'native-owner', nickname: null, gender: null, age: null, title: null }
    const ledger: LedgerItem[] = saved ? [{ kind: 'user', uid: 'native-prompt', text: 'The transcript prompt', who: sender.name, userId: sender.user_id, t: '10:00' }] : []
    const opening = { ...detail, key: `replayed-native-${saved}`, ledger }
    mock.method(api, 'getThreadDetail', async () => opening)
    queryClient.setQueryData(['thread-detail', opening.key], opening)
    await useConsole.getState().actions.selectThread('claude', opening.key)
    const watching = await socketOn(`thread_id=${opening.key}`)
    const started: WireEvent = { event_kind: 'run_started', sender, address: {
      channel_tentacle_id: 'claude', chat_type: 'thread', chat_id: opening.key, user_id: 'owner', channel_thread_id: null, shared: false,
    } }
    const prompt: WireEvent = { event_kind: 'enqueued_messages', enqueue_id: 'native-prompt',
      messages: [{ kind: 'request', parts: [{ part_kind: 'user-prompt', content: 'The transcript prompt' }], timestamp: '2026-10-14T10:00:00Z' }],
    }
    watching.deliver(started, prompt, { ...result, output: 'Partial' }, started, prompt, result)
    const state = useConsole.getState()
    const prompts = [...(state.detail?.ledger ?? []), ...state.live].filter(item => item.kind === 'user')
    assert.equal(prompts.length, 1)
    assert.equal(prompts[0]?.uid, 'native-prompt')
    assert.equal(prompts[0]?.kind === 'user' && prompts[0].text, 'The transcript prompt')
    assert.equal(prompts[0]?.kind === 'user' && prompts[0].who, 'test')
    assert.equal(prompts[0]?.kind === 'user' && prompts[0].userId, 'native-owner')
    const replies = useConsole.getState().live.filter((item) => item.kind === 'stream')
    assert.deepEqual(replies.map((item) => item.kind === 'stream' && item.text), ['Hello back'])
    watching.deliver({ event_kind: 'run_ended', thread_message_id: null })
    for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()
    assert.equal(useConsole.getState().running, false)
  })
}

for (const reflex of [false, true]) {
  test(`an enqueued prompt stays between replies inside ${reflex ? 'a reflex' : 'a tailed turn'}`, async () => {
    const opening = { ...detail, key: `native-steering-${reflex}`, ledger: [] }
    mock.method(api, 'getThreadDetail', async () => opening)
    queryClient.setQueryData(['thread-detail', opening.key], opening)
    await useConsole.getState().actions.selectThread('deepseek-native', opening.key)
    const watching = await socketOn(`thread_id=${opening.key}`)
    if (reflex) watching.deliver({ event_kind: 'reflex_awake' })
    watching.deliver({ event_kind: 'run_started', address: {
      channel_tentacle_id: 'deepseek-native', chat_type: 'thread', chat_id: opening.key, user_id: 'owner', channel_thread_id: null, shared: false,
    } }, { event_kind: 'part_start', index: 0, part: { part_kind: 'text', content: 'First reply' } }, {
      event_kind: 'enqueued_messages', enqueue_id: 'steer',
      messages: [{ kind: 'request', parts: [{ part_kind: 'user-prompt', content: 'Also check this' }], timestamp: '2026-10-14T10:00:00Z' }],
    }, { event_kind: 'part_start', index: 1, part: { part_kind: 'text', content: 'Second reply' } })
    assert.deepEqual(useConsole.getState().live.map(item => 'text' in item && item.text), [
      'First reply', 'Also check this', 'Second reply',
    ])
    assert.equal(useConsole.getState().running, true)
    watching.deliver({ event_kind: 'run_ended', thread_message_id: null })
    if (reflex) {
      assert.equal(useConsole.getState().running, true)
      watching.deliver({ event_kind: 'reflex_asleep' })
    }
    for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()
  })
}

test('a missed turn reads its history without clearing the next tailed turn already folding', async () => {
  const opening = { ...detail, key: 'consecutive-native' }
  const getDetail = mock.method(api, 'getThreadDetail', async () => opening)
  queryClient.setQueryData(['thread-detail', opening.key], opening)
  await useConsole.getState().actions.selectThread('claude', opening.key)
  const watching = await socketOn(`thread_id=${opening.key}`)
  let resolve!: (value: ThreadDetail) => void
  const reloading = new Promise<ThreadDetail>((done) => { resolve = done })
  getDetail.mock.mockImplementation(() => reloading)
  const started: WireEvent = { event_kind: 'run_started', address: {
    channel_tentacle_id: 'claude', chat_type: 'thread', chat_id: opening.key, user_id: 'owner', channel_thread_id: null, shared: false,
  } }
  const next: WireEvent = { event_kind: 'enqueued_messages', enqueue_id: 'next-prompt',
    messages: [{ kind: 'request', parts: [{ part_kind: 'user-prompt', content: 'Next prompt' }], timestamp: '2026-10-14T10:00:00Z' }],
  }
  watching.deliver({ event_kind: 'run_ended', thread_message_id: null }, started, next, result)
  const prompt = useConsole.getState().live.find(item => item.kind === 'user')
  assert.equal(prompt?.kind === 'user' && prompt.userId, useAuth.getState().user?.id)
  resolve({ ...opening, msgCount: 2, ledger: [{ kind: 'user', uid: 'next-prompt', text: 'Next prompt', who: 'Owner', t: '10:00' }] })
  await tick()
  assert.equal(useConsole.getState().detail?.msgCount, 2)
  assert.equal(useConsole.getState().running, true)
  assert.equal(useConsole.getState().live.filter(item => item.kind === 'user').length, 0)
  assert.ok(useConsole.getState().live.some((item) => item.kind === 'stream' && item.text === 'Hello back'))
  watching.deliver({ event_kind: 'run_ended', thread_message_id: 'reply' })
  for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()
  assert.equal(useConsole.getState().running, false)
})


test('consecutive watched turns stay mounted and replay replaces only the unfinished turn', async () => {
  const opening = { ...detail, key: 'stable-native' }
  const getDetail = mock.method(api, 'getThreadDetail', async () => opening)
  await useConsole.getState().actions.selectThread('codex-native', opening.key)
  const watching = await socketOn(`thread_id=${opening.key}`)
  const calls = getDetail.mock.callCount()
  const started: WireEvent = { event_kind: 'run_started', address: {
    channel_tentacle_id: 'codex-native', chat_type: 'thread', chat_id: opening.key, user_id: 'owner', channel_thread_id: null, shared: false,
  } }
  const text: WireEvent = { event_kind: 'part_start', index: 0, part: { part_kind: 'text', content: 'First turn' } }
  watching.deliver(started, text)
  const firstId = useConsole.getState().live[0].uid
  watching.deliver({ event_kind: 'run_ended', thread_message_id: 'reply' })
  const first = useConsole.getState().live[0]
  assert.equal(first.uid, firstId)
  assert.equal(first.kind === 'stream' && first.streaming, false)
  watching.deliver(started, { ...text, part: { part_kind: 'text', content: 'Incomplete' } })
  watching.deliver(started, { ...text, part: { part_kind: 'text', content: 'Second turn' } }, { event_kind: 'run_ended', thread_message_id: 'reply-2' })
  assert.equal(useConsole.getState().live[0], first)
  assert.deepEqual(useConsole.getState().live.map(item => item.kind === 'stream' && item.text), ['First turn', 'Second turn'])
  assert.equal(getDetail.mock.callCount(), calls)
  assert.equal(useConsole.getState().running, false)
})

for (const saved of [false, true]) {
  test(`a later watched turn answers a question ${saved ? 'from saved history' : 'kept from an earlier turn'}`, async () => {
    const question: LedgerItem = {
      kind: 'ask', uid: 'saved-question', title: 'Question', questions: [{ body: 'Inspect?', options: [], multiSelect: false }],
      nativeCallId: 'native-q', nativeAgent: 'Codex', tool: 'request_user_input_async', meta: 'Answer in Codex', state: 'waiting',
    }
    const opening = { ...detail, key: `question-next-turn-${saved}`, ledger: saved ? [question] : [] }
    mock.method(api, 'getThreadDetail', async () => opening)
    await useConsole.getState().actions.selectThread('codex-native', opening.key)
    const watching = await socketOn(`thread_id=${opening.key}`)
    const started: WireEvent = { event_kind: 'run_started', address: {
      channel_tentacle_id: 'codex-native', chat_type: 'thread', chat_id: opening.key, user_id: 'owner', channel_thread_id: null, shared: false,
    } }
    if (!saved) watching.deliver(started, {
      event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_name: 'request_user_input_async', tool_call_id: 'native-q', args: { questions: [{ title: 'Inspect?' }] } },
    }, { event_kind: 'run_ended', thread_message_id: 'reply' })
    const text = `<send_user_message_question_reply>${JSON.stringify([{ questionItemId: JSON.stringify(['request_user_input_async', 'native-q', 0]), question: 'Inspect?', answer: 'Yes' }])}</send_user_message_question_reply>`
    watching.deliver(started, { event_kind: 'enqueued_messages', enqueue_id: 'answer', messages: [{ kind: 'request', timestamp: null, parts: [{ part_kind: 'user-prompt', content: text }] }] }, { event_kind: 'run_ended', thread_message_id: 'reply-2' })
    const state = useConsole.getState()
    const card = [...state.detail!.ledger, ...state.live].find(item => item.kind === 'ask')
    assert.equal(card?.kind === 'ask' && card.state, 'answered')
    assert.equal(card?.kind === 'ask' && card.questions[0].answer, 'Yes')
  })
}
