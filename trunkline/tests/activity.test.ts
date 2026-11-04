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
const globals = ['WebSocket', 'location'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
const releases: (() => void)[] = []
const tick = () => new Promise(resolve => setImmediate(resolve))
const thinking: WireEvent = { event_kind: 'run_activity_changed', activity: 'thinking' }
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
  const { activityText, observeActivity, useThreadActivity, useActivity } = await server.ssrLoadModule('/src/state/activity.ts')
  activity = { activityText, observeActivity, useThreadActivity, useActivity }
  ;({ TurnFold } = await server.ssrLoadModule('/src/lib/api/fold.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
})
beforeEach(() => {
  installLiveSocket()
  mock.timers.enable({ apis: ['setTimeout'] })
})
afterEach(() => {
  releases.splice(0).forEach(release => release())
  queryClient.clear()
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
  LiveSocket.last().deliver(thinking)
  const first = activity.useActivity.getState()['id:first']
  mock.timers.tick(119_000)
  LiveSocket.last().deliver(thinking)
  assert.equal(activity.useActivity.getState()['id:first'], first)
  mock.timers.tick(119_999)
  assert.equal(text(), 'Thinking')
  mock.timers.tick(1)
  assert.equal(text(), undefined)
  LiveSocket.last().deliver({ event_kind: 'reflex_phase_changed', phase: 'routing' })
  assert.equal(text(), 'Routing')
})

for (const terminal of ['run_ended', 'reflex_asleep', 'run_error'] as const) {
  test(`${terminal} clears immediately and its old timer cannot clear a new label`, async () => {
    observe()
    await tick()
    const socket = LiveSocket.last()
    socket.deliver(thinking)
    mock.timers.tick(60_000)
    socket.deliver({ event_kind: terminal })
    assert.equal(text(), undefined)
    socket.deliver({ event_kind: 'run_activity_changed', activity: 'writing' })
    mock.timers.tick(60_000)
    assert.equal(text(), 'Writing the response')
  })
}

for (const event of [
  { event_kind: 'part_end', index: 0, part: { part_kind: 'text', content: 'Done' } },
  { event_kind: 'function_tool_result', part: { part_kind: 'tool-return', tool_call_id: 't', tool_name: 'tool', content: 'Done' } },
  { event_kind: 'output_tool_result', part: { part_kind: 'tool-return', tool_name: 'tool', tool_call_id: 't', content: 'Done' } },
] satisfies WireEvent[]) {
  test(`${event.event_kind} refreshes activity without completing sibling tools`, async () => {
    observe()
    await tick()
    const socket = LiveSocket.last()
    socket.deliver(event)
    assert.equal(text(), undefined)
    socket.deliver({ event_kind: 'run_activity_changed', activity: 'using_tools' })
    mock.timers.tick(100_000)
    socket.deliver(event)
    mock.timers.tick(100_000)
    assert.equal(text(), 'Using tools')
    mock.timers.tick(20_000)
    assert.equal(text(), undefined)
  })
}

test('sidebar and chat share observation; selecting another thread preserves the row', async () => {
  const row = observe()
  const chat = observe()
  await tick()
  assert.equal(LiveSocket.opened.length, 1)
  const first = LiveSocket.last()
  first.deliver(thinking)
  chat()
  observe('second')
  await tick()
  LiveSocket.last().deliver({ event_kind: 'reflex_phase_changed', phase: 'moving' })
  assert.equal(text(), 'Thinking')
  assert.equal(text('second'), 'Moving')
  assert.equal(first.closed, false)
  row()
  assert.equal(first.closed, true)
  assert.equal(text(), undefined)
  assert.equal(text('second'), 'Moving')
})

test('expiry leaves ordering, timestamps, run ownership and pending controls untouched', async () => {
  const rows = [{ id: 'second', updated_at: '2026-11-04T02:00:00Z' }, { id: 'first', updated_at: '2026-11-04T01:00:00Z' }]
  queryClient.setQueryData(['threads'], rows)
  const pending: LedgerItem = { kind: 'ask', uid: 'ask', title: 'Choose', tool: 'ask', meta: '', questions: [], state: 'waiting', batchId: 'batch' }
  useConsole.setState({ selThreadId: 'second', running: true, live: [pending] })
  const state = useConsole.getState()
  observe()
  await tick()
  LiveSocket.last().deliver({ ...batch, blocks_run: true })
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
  old.deliver(thinking)
  old.close()
  await tick()
  mock.timers.tick(2_000)
  await tick()
  const next = LiveSocket.last()
  assert.notEqual(next, old)
  next.deliver({ event_kind: 'reflex_phase_changed', phase: 'moving' })
  stale({ data: JSON.stringify({ event_kind: 'run_ended' }) })
  assert.equal(text(), 'Moving')
})

test('unmount aborts a pending handshake and cancels reconnect and expiry', async () => {
  const release = observe('new thread', true)
  const pending = LiveSocket.last()
  assert.ok(pending.url.endsWith('thread_key=new%20thread'))
  release()
  await tick()
  assert.equal(pending.closed, true)
  assert.deepEqual(activity.useActivity.getState(), {})
  mock.timers.tick(240_000)
  assert.equal(LiveSocket.opened.length, 1)
})

test('a handshake failure reconnects, but cleanup prevents further attempts', async () => {
  const release = observe()
  LiveSocket.last().onerror?.()
  await tick()
  mock.timers.tick(2_000)
  await tick()
  const socket = LiveSocket.last()
  socket.deliver(thinking)
  socket.close()
  await tick()
  release()
  mock.timers.tick(240_000)
  assert.equal(LiveSocket.opened.length, 2)
  assert.equal(text(), undefined)
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
