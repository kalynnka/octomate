import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiThread } from '../src/lib/api/events.ts'
import type { ThreadDetail } from '../src/lib/api/types.ts'

let server: ViteDevServer
let api: typeof import('../src/lib/api/index.ts').api
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let followAddress: typeof import('../src/state/url.ts').followAddress

const detail: ThreadDetail = {
  key: 'thread', msgCount: 0, sessions: [], ledger: [], ctxK: 0,
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null },
}
const row = (id: string, channel: string): ApiThread => ({
  id, kind: 'thread', chat_type: 'thread', chat_id: 'owner', channel_tentacle_id: channel,
  channel_thread_id: id, title: id, project_id: null, status: 'active',
  created_at: '2026-10-10T00:00:00Z', updated_at: '2026-10-10T00:00:00Z', handoffs: [],
})
const rows = [row('newest', 'trunkline'), row('on-slack', 'slack')]

// What the browser was asked to do with its address, in order.
let moves: string[]
let popstate: (() => void) | undefined
const originals = ['document', 'window', 'requestAnimationFrame'].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
const settle = async () => {
  for (let tick = 0; tick < 10; tick++) await new Promise((done) => setTimeout(done, 0))
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ api } = await server.ssrLoadModule('/src/lib/api/index.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ followAddress } = await server.ssrLoadModule('/src/state/url.ts'))
})
beforeEach(() => {
  moves = []
  const location = { pathname: '/' }
  const move = (kind: string) => (_: unknown, __: string, path: string) => {
    moves.push(`${kind} ${path}`)
    location.pathname = path
  }
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    getElementById: () => null,
  } })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    location,
    history: { pushState: move('push'), replaceState: move('replace') },
    matchMedia: () => ({ matches: true }),
    innerWidth: 1600,
    addEventListener: (_: string, listener: () => void) => { popstate = listener },
    removeEventListener: () => { popstate = undefined },
  } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  mock.method(api, 'getThreadDetail', (id: string) => Promise.resolve({ ...detail, key: id }))
  mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path.endsWith('/api/trunkline/threads')) return Response.json(rows)
    if (path.endsWith('/api/trunkline/routes')) return Response.json([])
    const thread = rows.find((each) => path.endsWith(`/threads/${each.id}`))
    return thread ? Response.json(thread) : new Response('{}', { status: 404 })
  })
  useConsole.getState().actions.signedOut()
})
afterEach(() => {
  mock.restoreAll()
  queryClient.clear()
  for (const [key, descriptor] of originals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
})
after(async () => { await server?.close() })

test('a reload opens what the address names, on its own channel', async () => {
  window.location.pathname = '/threads/on-slack'
  const stop = followAddress()
  await settle()
  assert.deepEqual([useConsole.getState().selChannel, useConsole.getState().selThreadId], ['slack', 'on-slack'])
  assert.deepEqual(moves, [])
  stop()
})

test('the root, or a thread this account cannot open, lands on the newest thread', async () => {
  for (const start of ['/', '/threads/someone-elses']) {
    moves = []
    window.location.pathname = start
    const stop = followAddress()
    await settle()
    assert.deepEqual(moves, ['replace /threads/newest'], start)
    stop()
    useConsole.getState().actions.signedOut()
  }
})

test('each place opened is a step in history, and Back reopens the last one', async () => {
  window.location.pathname = '/threads/newest'
  const stop = followAddress()
  await settle()
  const actions = useConsole.getState().actions
  await actions.selectThread('slack', 'on-slack')
  actions.openControl('agents')
  actions.startNewThread()
  // Saving the new thread puts it where the composer was, rather than after it.
  useConsole.setState({ ntOn: false, selThreadId: 'saved' })
  assert.deepEqual(moves, [
    'push /threads/on-slack',
    'push /control/agents',
    'push /threads/new',
    'replace /threads/saved',
  ])

  window.location.pathname = '/control/agents'
  popstate?.()
  await settle()
  assert.equal(useConsole.getState().mgmtSec, 'agents')
  assert.equal(moves.length, 4, 'reading the address back does not write it again')

  // A lapsed session leaves the address for the sign-in that follows.
  actions.signedOut()
  assert.equal(window.location.pathname, '/control/agents')
  stop()
})
