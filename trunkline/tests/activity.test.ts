import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiThread, WireEvent } from '../src/lib/api/events.ts'
import type { LedgerItem } from '../src/lib/api/types.ts'
import { installLiveSocket, LiveSocket } from './liveSocket.ts'

let server: ViteDevServer
let activity: typeof import('../src/state/activity.ts')
let TurnFold: typeof import('../src/lib/api/fold.ts').TurnFold
let useConsole: typeof import('../src/state/console.ts').useConsole
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useAuth: typeof import('../src/state/auth.ts').useAuth
let apiFetch: typeof import('../src/lib/api/auth.ts').apiFetch
const globals = ['WebSocket', 'location'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
const releases: (() => void)[] = []
const tick = () => new Promise(resolve => setImmediate(resolve))
const thinking: WireEvent = { event_kind: 'run_activity_changed', activity: 'thinking' }
const update = (event: WireEvent, key = 'id:first') => ({ key, event, attention: false })
const batch: WireEvent = { event_kind: 'action_batch', batch_id: 'batch', questions: [], approvals: [] }
const address = { channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: 'key', shared: false } as const
const text = (id = 'first') => activity.useActivity.getState()[`id:${id}`]?.text
const observe = (id = 'first', byKey = false) => {
  const release = activity.observeActivity(id, byKey)
  releases.push(release)
  return release
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  const { activityText, observeActivity, resumeActivity, useThreadActivity, useActivity } = await server.ssrLoadModule('/src/state/activity.ts')
  activity = { activityText, observeActivity, resumeActivity, useThreadActivity, useActivity }
  ;({ TurnFold } = await server.ssrLoadModule('/src/lib/api/fold.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useAuth } = await server.ssrLoadModule('/src/state/auth.ts'))
  ;({ apiFetch } = await server.ssrLoadModule('/src/lib/api/auth.ts'))
})
beforeEach(() => {
  installLiveSocket()
  mock.timers.enable({ apis: ['setTimeout'] })
})
afterEach(async () => {
  releases.splice(0).forEach(release => release())
  await tick()
  queryClient.clear()
  useAuth.setState({ status: 'booting', user: null, notice: null })
  mock.timers.reset()
  mock.restoreAll()
})
after(async () => {
  await server?.close()
  for (const [key, descriptor] of globals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
})

const labels: [WireEvent, string | undefined][] = [
  [{ event_kind: 'reflex_awake' }, 'Preparing'],
  [{ event_kind: 'reflex_phase_changed', phase: 'routing' }, 'Routing'],
  [{ event_kind: 'reflex_phase_changed', phase: 'moving' }, 'Moving'],
  [{ event_kind: 'reflex_phase_changed', phase: 'finalizing' }, 'Finalizing'],
  [{ event_kind: 'run_started', address }, 'Working'],
  [thinking, 'Thinking'],
  [{ event_kind: 'run_activity_changed', activity: 'using_tools' }, 'Using tools'],
  [{ event_kind: 'run_activity_changed', activity: 'writing' }, 'Writing the response'],
  [{ event_kind: 'run_activity_changed', activity: 'compacting' }, 'Compacting context'],
  [{ event_kind: 'part_start', index: 0, part: { part_kind: 'thinking', content: 'private thoughts' } }, 'Thinking'],
  [{ event_kind: 'part_delta', index: 0, delta: { part_delta_kind: 'text', content_delta: 'private reply' } }, 'Writing the response'],
  [{ event_kind: 'part_start', index: 0, part: { part_kind: 'tool-call', tool_name: 'shell', tool_call_id: 't', args: 'secret' } }, 'Working'],
  [{ event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_name: 'mcp__tools__read_file', tool_call_id: 't', args: 'secret' } }, 'Using tools'],
  [{ ...batch, blocks_run: true }, 'Waiting for input'],
  [{ ...batch, blocks_run: false }, 'Working · Input requested'],
  [batch, 'Working · Input requested'],
  [{ event_kind: 'run_ended', outcome: 'deferred' }, ''],
  [{ event_kind: 'reflex_asleep', outcome: 'handed_off' }, ''],
  [{ event_kind: 'run_error', message: 'error', trace_id: 'trace' }, ''],
  [{ event_kind: 'subagent_started', invocation_id: 'child', kind: 'commission', name: 'Child' }, undefined],
  [{ event_kind: 'subagent_settled', invocation_id: 'child', status: 'completed', response: 'Done' }, undefined],
]
for (const [event, expected] of labels) {
  test(`label: ${event.event_kind} ${expected ?? 'unchanged'}`, () => {
    assert.equal(activity.activityText(event), expected)
  })
}

test('harness labels normalize controls and whitespace, bound Unicode, and fall back when empty', () => {
  assert.equal(activity.activityText({ ...thinking, display_text: '\u0000\u202e \n ' }), 'Thinking')
  assert.equal(activity.activityText({ ...thinking, display_text: '  Pondering \u0000 \t\n octopuses\u0000  ' }), 'Pondering octopuses')
  assert.equal(activity.activityText({ ...thinking, display_text: '🐙'.repeat(121) }), '🐙'.repeat(120))
})

test('display labels drop ellipsis suffixes but keep punctuation inside a phrase', () => {
  for (const display_text of ['Pondering…', 'Pondering...', 'Pondering……', 'Pondering.....']) {
    assert.equal(activity.activityText({ ...thinking, display_text }), 'Pondering')
  }
  assert.equal(activity.activityText({ ...thinking, display_text: 'Working… · Input requested' }), 'Working · Input requested')
  assert.equal(activity.activityText({ ...thinking, display_text: 'Reading file...name' }), 'Reading file...name')
  assert.equal(activity.activityText({ ...thinking, display_text: '...' }), 'Thinking')
})

test('a repeated label refreshes expiry without rerendering; later activity reappears', async () => {
  observe()
  await tick()
  LiveSocket.last().deliver(update(thinking))
  const first = activity.useActivity.getState()['id:first']
  mock.timers.tick(119_000)
  LiveSocket.last().deliver(update(thinking))
  assert.equal(activity.useActivity.getState()['id:first'], first)
  mock.timers.tick(119_999)
  assert.equal(text(), 'Thinking')
  mock.timers.tick(1)
  assert.equal(text(), undefined)
  LiveSocket.last().deliver(update({ event_kind: 'reflex_phase_changed', phase: 'routing' }))
  assert.equal(text(), 'Routing')
})

for (const terminal of ['run_ended', 'reflex_asleep', 'run_error'] as const) {
  test(`${terminal} clears immediately and its old timer cannot clear a new label`, async () => {
    observe()
    await tick()
    const socket = LiveSocket.last()
    socket.deliver(update(thinking))
    mock.timers.tick(60_000)
    socket.deliver(update(terminal === 'run_error' ? { event_kind: terminal, message: 'failed', trace_id: 'trace' } : { event_kind: terminal }))
    assert.equal(text(), undefined)
    socket.deliver(update({ event_kind: 'run_activity_changed', activity: 'writing' }))
    mock.timers.tick(60_000)
    assert.equal(text(), 'Writing the response')
  })
}

test('sidebar and chat share observation; selecting another thread preserves the row', async () => {
  const row = observe()
  const chat = observe()
  await tick()
  assert.equal(LiveSocket.opened.length, 1)
  const first = LiveSocket.last()
  first.deliver(update(thinking))
  chat()
  observe('second')
  await tick()
  LiveSocket.last().deliver(update({ event_kind: 'reflex_phase_changed', phase: 'moving' }, 'id:second'))
  assert.equal(text(), 'Thinking')
  assert.equal(text('second'), 'Moving')
  assert.equal(first.closed, false)
  row()
  await tick()
  assert.equal(first.closed, false)
  assert.deepEqual(JSON.parse(first.sent.at(-1)!), { thread_ids: ['second'], thread_keys: [] })
  assert.equal(text(), undefined)
  assert.equal(text('second'), 'Moving')
})

test('moving a running row keeps its shared socket and activity through the effect handoff', async () => {
  const release = observe()
  await tick()
  const socket = LiveSocket.last()
  socket.deliver(update(thinking))
  const first = activity.useActivity.getState()['id:first']
  release()
  observe()
  await tick()
  assert.equal(socket.closed, false)
  assert.equal(LiveSocket.opened.length, 1)
  assert.equal(activity.useActivity.getState()['id:first'], first)
})

test('30 threads and a draft share one socket, including subscription changes and cleanup', async () => {
  const rows = Array.from({ length: 30 }, (_, i) => observe(`thread-${i}`))
  const draft = observe('draft', true)
  await tick()
  assert.equal(LiveSocket.opened.length, 1)
  const socket = LiveSocket.last()
  assert.equal(JSON.parse(socket.sent.at(-1)!).thread_ids.length, 30)
  assert.deepEqual(JSON.parse(socket.sent.at(-1)!).thread_keys, ['draft'])
  socket.deliver(update(thinking, 'id:thread-20'), update(thinking, 'key:draft'), update(thinking, 'id:unwatched'))
  assert.equal(text('thread-20'), 'Thinking')
  assert.equal(activity.useActivity.getState()['key:draft']?.text, 'Thinking')
  assert.equal(text('unwatched'), undefined)
  rows[0]()
  await tick()
  assert.equal(socket.closed, false)
  assert.equal(JSON.parse(socket.sent.at(-1)!).thread_ids.length, 29)
  rows.forEach(release => release())
  draft()
  await tick()
  assert.equal(socket.closed, true)
  assert.deepEqual(activity.useActivity.getState(), {})
  mock.timers.tick(240_000)
  await tick()
  assert.equal(LiveSocket.opened.length, 1)
})

test('expiry leaves ordering, timestamps, run ownership and pending controls untouched', async () => {
  const rows = [{ id: 'second', updated_at: '2026-11-04T02:00:00Z' }, { id: 'first', updated_at: '2026-11-04T01:00:00Z' }]
  queryClient.setQueryData(['threads'], rows)
  const pending: LedgerItem = { kind: 'ask', uid: 'ask', title: 'Choose', tool: 'ask', meta: '', questions: [], state: 'waiting', batchId: 'batch' }
  useConsole.setState({ selThreadId: 'second', running: true, live: [pending] })
  const state = useConsole.getState()
  observe()
  await tick()
  LiveSocket.last().deliver({ ...update({ event_kind: 'run_activity_changed', activity: 'working', display_text: 'Waiting for input' }), attention: true })
  assert.equal(activity.useActivity.getState()['id:first']?.attention, true)
  mock.timers.tick(120_000)
  assert.equal(text(), undefined)
  assert.equal(useConsole.getState(), state)
  assert.deepEqual(queryClient.getQueryData<ApiThread[]>(['threads']), rows)
})

test('reconnect resumes subsequent events and ignores stale connection callbacks', async () => {
  observe()
  await tick()
  const old = LiveSocket.last()
  const stale = old.onmessage!
  old.deliver(update(thinking))
  old.close()
  await tick()
  mock.timers.tick(2_000)
  await tick()
  const next = LiveSocket.last()
  assert.notEqual(next, old)
  next.deliver(update({ event_kind: 'reflex_phase_changed', phase: 'moving' }))
  stale({ data: JSON.stringify(update({ event_kind: 'run_ended' })) })
  assert.equal(text(), 'Moving')
})

test('returning to the tab resumes a pending retry without duplicating the socket or losing activity', async () => {
  observe()
  await tick()
  const old = LiveSocket.last()
  old.deliver(update(thinking))
  activity.resumeActivity()
  assert.equal(LiveSocket.opened.length, 1)
  old.close()
  activity.resumeActivity()
  activity.resumeActivity()
  await tick()
  assert.equal(LiveSocket.opened.length, 2)
  assert.equal(text(), 'Thinking')
  assert.deepEqual(JSON.parse(LiveSocket.last().sent[0]), { thread_ids: ['first'], thread_keys: [] })
  mock.timers.tick(2_000)
  await tick()
  assert.equal(LiveSocket.opened.length, 2)
  LiveSocket.last().deliver(update({ event_kind: 'run_ended' }))
  assert.equal(text(), undefined)
})

test('unmount aborts a pending handshake and cancels reconnect and expiry', async () => {
  LiveSocket.autoOpen = false
  const release = observe('new thread', true)
  await tick()
  const pending = LiveSocket.last()
  assert.ok(pending.url.endsWith('/live/activity'))
  assert.deepEqual(pending.sent, [])
  release()
  await tick()
  assert.equal(pending.closed, true)
  assert.deepEqual(activity.useActivity.getState(), {})
  mock.timers.tick(240_000)
  assert.equal(LiveSocket.opened.length, 1)
})

test('a handshake failure reconnects, but cleanup prevents further attempts', async () => {
  LiveSocket.autoOpen = false
  const release = observe()
  await tick()
  LiveSocket.last().onerror?.()
  await tick()
  LiveSocket.autoOpen = true
  mock.timers.tick(2_000)
  await tick()
  const socket = LiveSocket.last()
  socket.deliver(update(thinking))
  socket.close()
  await tick()
  release()
  await tick()
  mock.timers.tick(240_000)
  assert.equal(LiveSocket.opened.length, 2)
  assert.equal(text(), undefined)
})

test('twenty watched threads share one session check and refresh before reconnecting', async () => {
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  let refreshed = false
  const fetch = mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => {
    if (String(input) === '/api/auth/refresh') {
      await pending
      refreshed = true
      return new Response(null, { status: 204 })
    }
    if (String(input) === '/api/trunkline/routes') {
      return refreshed ? Response.json([]) : new Response(null, { status: 401 })
    }
    assert.equal(String(input), '/api/auth/me')
    return refreshed ? Response.json({ id: 'owner' }) : new Response(null, { status: 401 })
  })
  LiveSocket.autoOpen = false
  for (let index = 0; index < 20; index++) observe(`thread-${index}`)
  await tick()
  LiveSocket.last().onerror?.()
  await tick()
  assert.deepEqual(fetch.mock.calls.map(call => call.arguments[0]), ['/api/auth/me', '/api/auth/refresh'])
  assert.equal(LiveSocket.opened.length, 1)
  const foreground = apiFetch('/api/trunkline/routes')
  await tick()
  release()
  assert.equal((await foreground).status, 200)
  await tick()
  const paths = fetch.mock.calls.map(call => call.arguments[0])
  assert.equal(paths.filter(path => path === '/api/auth/refresh').length, 1)
  assert.equal(paths.filter(path => path === '/api/auth/me').length, 2)
  assert.equal(paths.filter(path => path === '/api/trunkline/routes').length, 2)
  mock.timers.tick(2_000)
  await tick()
  assert.equal(LiveSocket.opened.length, 2)
  LiveSocket.last().deliver(update(thinking, 'id:thread-19'))
  assert.equal(text('thread-19'), 'Thinking')
})

test('an expired login stops activity reconnects and returns the account to sign-in', async () => {
  useAuth.setState({ status: 'signed-in', user: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
  const fetch = mock.method(globalThis, 'fetch', async () => new Response(null, { status: 401 }))
  LiveSocket.autoOpen = false
  observe()
  await tick()
  LiveSocket.last().onerror?.()
  await tick()
  assert.equal(useAuth.getState().status, 'signed-out')
  assert.equal(fetch.mock.callCount(), 2)
  mock.timers.tick(240_000)
  await tick()
  assert.equal(LiveSocket.opened.length, 1)
  assert.equal(fetch.mock.callCount(), 2)
})

test('unmount during a shared session check removes that thread from the replacement subscription', async () => {
  let release!: (response: Response) => void
  mock.method(globalThis, 'fetch', () => new Promise<Response>(resolve => { release = resolve }))
  LiveSocket.autoOpen = false
  const stop = observe()
  observe('second')
  await tick()
  LiveSocket.last().onerror?.()
  await tick()
  stop()
  LiveSocket.autoOpen = true
  release(Response.json({ id: 'owner' }))
  await tick()
  mock.timers.tick(2_000)
  await tick()
  assert.equal(LiveSocket.opened.length, 2)
  assert.deepEqual(JSON.parse(LiveSocket.last().sent.at(-1) ?? '{}'), { thread_ids: ['second'], thread_keys: [] })
})

for (const failure of ['offline', 503] as const) {
  test(`session recovery ${failure} keeps the account signed in and retries at the normal interval`, async () => {
    useAuth.setState({ status: 'signed-in', user: { id: 'owner', username: 'owner', name: 'Owner', nickname: null } })
    const fetch = mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => {
      if (failure === 'offline') throw new TypeError('Network unavailable')
      return new Response(null, { status: String(input) === '/api/auth/refresh' ? failure : 401 })
    })
    LiveSocket.autoOpen = false
    observe()
    await tick()
    LiveSocket.last().onerror?.()
    await tick()
    assert.ok(fetch.mock.callCount() > 0)
    assert.equal(useAuth.getState().status, 'signed-in')
    assert.equal(LiveSocket.opened.length, 1)
    mock.timers.tick(1_999)
    await tick()
    assert.equal(LiveSocket.opened.length, 1)
    mock.timers.tick(1)
    await tick()
    assert.equal(LiveSocket.opened.length, 2)
  })
}



test('a denied subscription does not retry the same forbidden request', async () => {
  observe()
  await tick()
  LiveSocket.last().close(1008)
  activity.resumeActivity()
  mock.timers.tick(240_000)
  await tick()
  assert.equal(LiveSocket.opened.length, 1)
})

for (const outcome of ['completed', 'failed', 'cancelled', 'deferred'] as const) {
  test(`the timeline uses the ${outcome} outcome without losing usage or question cards`, () => {
    let items: LedgerItem[] = []
    const fold = new TurnFold({
      push(draft) {
        const item = { ...draft, uid: String(items.length) } as LedgerItem
        items.push(item)
        return item.uid
      },
      patch(uid, patch) { items = items.map(item => item.uid === uid ? { ...item, ...patch } as LedgerItem : item) },
      done() {},
    })
    fold.feed({ event_kind: 'run_result', output: null, usage: { requests: 1, tool_calls: 0, input_tokens: 12, output_tokens: 3, cache_read_tokens: 0, cache_write_tokens: 0 } })
    fold.feed({ ...batch, blocks_run: true, questions: [{ id: 'question', kind: 'question', status: 'pending', tool_name: 'ask', tool_call_id: 'call', position: 0, args: { question: 'Where?', choices: ['Here'] } }] })
    fold.feed({ event_kind: 'run_ended', outcome })
    const end = items.find(item => item.kind === 'end')
    assert.equal(end?.kind === 'end' && end.label, `run ${outcome === 'completed' ? 'complete' : outcome} · 1 request · 12 tokens in · 3 out`)
    assert.equal(items.filter(item => item.kind === 'end').length, 1)
    const pending = items.find(item => item.kind === 'ask')
    assert.equal(pending?.kind === 'ask' && pending.state, 'waiting')
  })
}
