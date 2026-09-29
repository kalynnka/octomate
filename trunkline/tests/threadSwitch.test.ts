import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
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
