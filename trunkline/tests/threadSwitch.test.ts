import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiThread, WireEvent } from '../src/lib/api/events.ts'
import type { ThreadDetail } from '../src/lib/api/types.ts'

let server: ViteDevServer
let api: typeof import('../src/lib/api/index.ts').api
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let callbackResult: void | Promise<void>
const originalGlobals = ['document', 'window', 'requestAnimationFrame'].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
const detail: ThreadDetail = {
  key: 'thread-a', msgCount: 0, sessions: [], ledger: [], ctxK: 0,
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null },
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ api } = await server.ssrLoadModule('/src/lib/api/index.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
})
beforeEach(() => {
  const panel = { style: { removeProperty() {}, viewTransitionName: '' } }
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    getElementById: (id: string) => id === 'trk-chathead' ? { closest: () => panel } : null,
    startViewTransition(update: () => void | Promise<void>) {
      callbackResult = update()
      const updateCallbackDone = Promise.resolve(callbackResult)
      return { updateCallbackDone, finished: updateCallbackDone, skipTransition() {} }
    },
  } })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { matchMedia: () => ({ matches: false }) } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
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

test('a delayed response cannot replace the newly selected thread', async () => {
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
      { event_kind: 'custom', name: 'run_started', address: {
        channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: sendKey, shared: false,
      } },
      { event_kind: 'run_result', output: 'Hello back', usage: { requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 } },
    ]
    return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } })
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
  assert.equal(transition.mock.callCount(), 0)
  assert.deepEqual(useConsole.getState().live.filter((item) => item.kind === 'user').map((item) => item.text), ['hello', 'continue'])
  assert.equal(useConsole.getState().live.filter((item) => item.kind === 'stream').length, 2)
  assert.equal(useConsole.getState().running, false)
})
