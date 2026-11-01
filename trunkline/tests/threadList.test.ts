import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiHandoff, ApiProject, ApiThread, ThreadEvent } from '../src/lib/api/events.ts'
import type { ThreadDetail } from '../src/lib/api/types.ts'
import { installLiveSocket, LiveSocket } from './liveSocket.ts'

let server: ViteDevServer
let api: typeof import('../src/lib/api/index.ts').api
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let watchThreads: typeof import('../src/state/threads.ts').watchThreads
let useConsole: typeof import('../src/state/console.ts').useConsole
let liveThreadDetail: typeof import('../src/lib/api/live.ts').liveThreadDetail
let stop: (() => void) | undefined
const globals = ['WebSocket', 'location'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
const tick = () => new Promise(resolve => setImmediate(resolve))
const thread: ApiThread = {
  id: 'thread-a', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
  channel_tentacle_id: 'trunkline', channel_thread_id: 'key-a', title: 'Opening',
  project_id: null, status: 'active', created_at: '2026-10-16T01:00:00Z',
  updated_at: '2026-10-16T01:00:00Z', handoffs: [],
}
const project: ApiProject = { id: 'project', name: 'octomate', origin: '', root: '/work/octomate', extra_roots: [], description: null, enabled: true }
const errors: string[] = []
const rows = () => queryClient.getQueryData<ApiThread[]>(['threads'])!

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ api } = await server.ssrLoadModule('/src/lib/api/index.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ watchThreads } = await server.ssrLoadModule('/src/state/threads.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ liveThreadDetail } = await server.ssrLoadModule('/src/lib/api/live.ts'))
})
beforeEach(() => {
  installLiveSocket()
  errors.length = 0
  useConsole.setState({ selThreadId: '', detail: null, live: [], running: false })
})
afterEach(() => {
  stop?.()
  stop = undefined
  queryClient.clear()
  mock.restoreAll()
})
after(async () => {
  await server?.close()
  for (const [key, descriptor] of globals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
})

test('the subscription opens before the snapshot; queued changes survive the read', async () => {
  let resolve!: (rows: ApiThread[]) => void
  const snapshot = mock.method(api, 'listThreads', () => new Promise<ApiThread[]>(done => { resolve = done }))
  stop = watchThreads(error => errors.push(error))
  assert.equal(snapshot.mock.callCount(), 0)
  assert.equal(LiveSocket.last().url, 'ws://console.test/api/trunkline/live/user')
  await tick()
  LiveSocket.last().deliver({ event_kind: 'thread_title_changed', thread_id: thread.id, title: 'Renamed', updated_at: '2026-10-16T02:00:00Z' })
  resolve([thread])
  await tick()
  assert.equal(rows()[0].title, 'Renamed')
  assert.equal(snapshot.mock.callCount(), 1)
  assert.deepEqual(errors, [])
})

test('specific events update known rows and ordering without further reads', async () => {
  const newer = { ...thread, id: 'thread-b', updated_at: '2026-10-16T02:00:00Z' }
  const snapshot = mock.method(api, 'listThreads', async () => [newer, thread])
  const fetch = mock.method(globalThis, 'fetch', async () => { throw new Error('unexpected read') })
  stop = watchThreads(error => errors.push(error))
  await tick()
  const handoff: ApiHandoff = {
    id: 'handoff-a', thread_id: thread.id, source_agent_tentacle_id: null,
    to_agent_tentacle_id: 'codex', to_model: 'model', reason: 'summon', hint: '', brief: '',
    target_conversation_id: null, source_conversation_id: null, created_at: '2026-10-16T02:30:00Z',
  }
  const events: ThreadEvent[] = [
    { event_kind: 'thread_title_changed', thread_id: thread.id, title: null, updated_at: '2026-10-16T02:01:00Z' },
    { event_kind: 'thread_project_bound', thread_id: thread.id, project_id: 'project', updated_at: '2026-10-16T02:02:00Z' },
    { event_kind: 'thread_handoff_recorded', thread_id: thread.id, handoff },
    { event_kind: 'thread_handoff_recorded', thread_id: thread.id, handoff },
    { event_kind: 'thread_message_recorded', thread_id: thread.id, message_id: 'message', updated_at: '2026-10-16T03:00:00Z' },
    { event_kind: 'thread_command_outcome_recorded', thread_id: thread.id, receipt_id: 'receipt', outcome: { status: 'completed', segments: [] }, updated_at: '2026-10-16T04:00:00Z' },
  ]
  LiveSocket.last().deliver(...events)
  await tick()
  assert.deepEqual(rows()[0], {
    ...thread, title: null, project_id: 'project', handoffs: [handoff],
    active_agent_tentacle_id: 'codex', updated_at: '2026-10-16T04:00:00Z',
  })
  assert.equal(rows()[1].id, newer.id)
  assert.equal(snapshot.mock.callCount(), 1)
  assert.equal(fetch.mock.callCount(), 0)
  assert.deepEqual(errors, [])
})

test('an unseen thread is fetched once while subsequent events wait for that row', async () => {
  mock.method(api, 'listThreads', async () => [])
  let resolve!: (response: Response) => void
  const fetch = mock.method(globalThis, 'fetch', () => new Promise<Response>(done => { resolve = done }))
  stop = watchThreads(error => errors.push(error))
  await tick()
  LiveSocket.last().deliver(
    { event_kind: 'thread_message_recorded', thread_id: thread.id, message_id: 'message', updated_at: thread.updated_at },
    { event_kind: 'thread_title_changed', thread_id: thread.id, title: 'New title', updated_at: '2026-10-16T02:00:00Z' },
  )
  await tick()
  assert.equal(fetch.mock.callCount(), 1)
  assert.equal(fetch.mock.calls[0].arguments[0], `/api/trunkline/threads/${thread.id}`)
  resolve(Response.json(thread))
  await tick()
  assert.equal(rows().length, 1)
  assert.equal(rows()[0].title, 'New title')
  assert.equal(fetch.mock.callCount(), 1)
})

for (const viewing of ['open', 'cached', 'switched away', 'uncached']) {
  test(`a project binding refreshes workspace metadata (${viewing}) without replacing history or live output`, async () => {
    const selected = viewing !== 'cached'
    const detail = liveThreadDetail({
      thread: { ...thread, active_agent_tentacle_id: 'codex' }, modelMessages: [], unbound: [], conversations: [], project: null, batches: [], usage: {},
      cursors: { model: 'older', unbound: null }, totals: { model: 10, unbound: 0 },
    })
    detail.ledger.push({ kind: 'user', uid: 'saved', t: '', who: 'Owner', text: 'Already loaded history' })
    const other = { ...detail, key: 'other' }
    const live = [{ kind: 'stream' as const, uid: 'reply', text: 'Still answering', streaming: true }]
    if (viewing !== 'uncached') queryClient.setQueryData(['thread-detail', thread.id], detail)
    useConsole.setState({ selThreadId: selected ? thread.id : 'other', detail: selected ? detail : other, live, running: true })
    mock.method(api, 'listThreads', async () => [thread])
    let resolve!: (response: Response) => void
    const fetch = mock.method(globalThis, 'fetch', () => new Promise<Response>(done => { resolve = done }))
    stop = watchThreads(error => errors.push(error))
    await tick()
    LiveSocket.last().deliver({ event_kind: 'thread_project_bound', thread_id: thread.id, project_id: 'project', updated_at: thread.updated_at })
    await tick()
    assert.equal(fetch.mock.callCount(), 1)
    assert.equal(fetch.mock.calls[0].arguments[0], `/api/trunkline/threads/${thread.id}/project`)
    if (viewing === 'switched away') useConsole.setState({ selThreadId: 'other', detail: other })
    resolve(Response.json(project))
    await tick()
    const cached = queryClient.getQueryData<ThreadDetail>(['thread-detail', thread.id])!
    assert.deepEqual(cached.project, { name: project.name, path: project.root, cwd: undefined })
    assert.equal(cached.vscode?.dir, project.root)
    assert.equal(cached.reads?.project?.id, project.id)
    assert.equal(cached.reads?.thread.project_id, project.id)
    assert.equal(cached.ledger, detail.ledger)
    assert.equal(cached.reads?.cursors.model, 'older')
    assert.deepEqual(useConsole.getState().detail, ['open', 'uncached'].includes(viewing) ? cached : other)
    assert.equal(useConsole.getState().live, live)
    assert.equal(useConsole.getState().running, true)
    assert.deepEqual(errors, [])
  })
}

test('a binding received during the initial detail read refreshes the workspace after that snapshot', async () => {
  let resolve!: (detail: ThreadDetail) => void
  const pending = queryClient.fetchQuery<ThreadDetail>({
    queryKey: ['thread-detail', thread.id],
    queryFn: () => new Promise(done => { resolve = done }),
  })
  mock.method(api, 'listThreads', async () => [thread])
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json(project))
  stop = watchThreads(error => errors.push(error))
  await tick()
  LiveSocket.last().deliver({ event_kind: 'thread_project_bound', thread_id: thread.id, project_id: project.id, updated_at: thread.updated_at })
  await tick()
  assert.equal(fetch.mock.callCount(), 0)
  resolve(liveThreadDetail({
    thread, modelMessages: [], unbound: [], conversations: [], project: null, batches: [], usage: {},
    cursors: { model: null, unbound: null }, totals: { model: 0, unbound: 0 },
  }))
  useConsole.setState({ selThreadId: thread.id, detail: await pending })
  await tick()
  assert.equal(fetch.mock.callCount(), 1)
  assert.equal(useConsole.getState().detail?.project?.name, project.name)
  assert.equal(queryClient.getQueryData<ThreadDetail>(['thread-detail', thread.id])?.project?.name, project.name)
  assert.deepEqual(errors, [])
})

test('events already reflected in the snapshot do not roll it back', async () => {
  const current = { ...thread, title: 'Latest', updated_at: '2026-10-16T03:00:00Z' }
  mock.method(api, 'listThreads', async () => [current])
  stop = watchThreads(error => errors.push(error))
  await tick()
  LiveSocket.last().deliver({ event_kind: 'thread_title_changed', thread_id: thread.id, title: 'Earlier', updated_at: '2026-10-16T02:00:00Z' })
  await tick()
  assert.deepEqual(rows(), [current])
})

test('run events do not read or alter the thread list', async () => {
  const snapshot = mock.method(api, 'listThreads', async () => [thread])
  stop = watchThreads(error => errors.push(error))
  await tick()
  LiveSocket.last().deliver(
    { event_kind: 'reflex_awake' },
    { event_kind: 'part_delta', index: 0, delta: { part_delta_kind: 'text', content_delta: 'Hi' } },
    { event_kind: 'reflex_asleep' },
  )
  await tick()
  assert.deepEqual(rows(), [thread])
  assert.equal(snapshot.mock.callCount(), 1)
})

test('a row no longer accessible is not added', async () => {
  mock.method(api, 'listThreads', async () => [])
  mock.method(globalThis, 'fetch', async () => new Response(null, { status: 404 }))
  stop = watchThreads(error => errors.push(error))
  await tick()
  LiveSocket.last().deliver({ event_kind: 'thread_message_recorded', thread_id: thread.id, message_id: 'message', updated_at: thread.updated_at })
  await tick()
  assert.deepEqual(rows(), [])
  assert.deepEqual(errors, [])
})

for (const reading of ['snapshot', 'unseen row']) {
  test(`closing the watcher during a ${reading} read cannot refill the next account's cache`, async () => {
    let resolve!: (rows: ApiThread[]) => void
    const pending = new Promise<ApiThread[]>(done => { resolve = done })
    mock.method(api, 'listThreads', () => reading === 'snapshot' ? pending : Promise.resolve([]))
    mock.method(globalThis, 'fetch', async () => Response.json((await pending)[0]))
    stop = watchThreads(error => errors.push(error))
    await tick()
    if (reading === 'unseen row') {
      LiveSocket.last().deliver({ event_kind: 'thread_message_recorded', thread_id: thread.id, message_id: 'message', updated_at: thread.updated_at })
      await tick()
    }
    stop()
    queryClient.clear()
    queryClient.setQueryData(['threads'], [])
    resolve([thread])
    await tick()
    assert.equal(LiveSocket.last().closed, true)
    assert.deepEqual(rows(), [])
    assert.deepEqual(errors, [])
  })
}

test('disconnects are reported and cleanup does not report a second error', async () => {
  mock.method(api, 'listThreads', async () => [thread])
  stop = watchThreads(error => errors.push(error))
  await tick()
  LiveSocket.last().close()
  stop()
  assert.deepEqual(errors, ['Thread-list updates disconnected.'])
})
