import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClientProvider } from '@tanstack/react-query'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiThread, GatewayEvent, GatewayRequest, ThreadOperations, WireEvent } from '../src/lib/api/events.ts'

let server: ViteDevServer
let streamGateway: typeof import('../src/lib/api/client.ts').streamGateway
let fetchThreadOperations: typeof import('../src/lib/api/client.ts').fetchThreadOperations
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let ChatHeader: typeof import('../src/features/chat/ChatHeader.tsx').ChatHeader
let GatewayDialog: typeof import('../src/features/chat/GatewayDialog.tsx').GatewayDialog
const globals = ['document', 'requestAnimationFrame'].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
const destination: ApiThread = {
  id: 'destination-id', kind: 'thread', chat_type: 'thread', chat_id: 'account',
  channel_tentacle_id: 'lark', channel_thread_id: 'platform-id', title: 'Arrived',
  project_id: null, status: 'active', created_at: '', updated_at: '', handoffs: [],
}
const request: GatewayRequest = { action: 'teleport', body: { destination: { kind: 'channel', channel: 'lark' }, hint: 'Continue here' } }
const gateway: GatewayEvent = { event_kind: 'gateway', action: 'teleport', destination: {
  channel_tentacle_id: 'lark', chat_type: 'thread', chat_id: 'account', channel_thread_id: 'platform-id', user_id: 'owner', shared: false,
} }
const result: WireEvent = { event_kind: 'run_result', output: 'Arrived', usage: { requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 } }
const options: ThreadOperations = {
  teleport: { destinations: [], reason: 'This native session cannot teleport.' },
  summon: { destinations: [{ target: { kind: 'here' }, label: 'This thread', routes: [{ agent_id: 'claude', model: 'sonnet', claim: { ability: 'Code review', efforts: ['low', 'high'] } }] }], reason: null },
}
const sse = (...events: WireEvent[]) => new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } })

before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom' })
  ;({ streamGateway, fetchThreadOperations } = await server.ssrLoadModule('/src/lib/api/client.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ ChatHeader } = await server.ssrLoadModule('/src/features/chat/ChatHeader.tsx'))
  ;({ GatewayDialog } = await server.ssrLoadModule('/src/features/chat/GatewayDialog.tsx'))
})
beforeEach(() => {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { getElementById: () => null } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  useConsole.setState({ selThreadId: 'source', ntOn: false, running: false, gatewayPending: null, live: [], notices: [] })
})
afterEach(() => {
  mock.restoreAll()
  queryClient.clear()
  useConsole.getInitialState().detail = null
  useConsole.getInitialState().selThreadId = ''
  for (const [key, descriptor] of globals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
})
after(async () => { await server?.close() })

for (const action of ['teleport', 'summon'] as const) {
  test(`${action} posts the typed payload once and waits for its confirmed destination`, async () => {
    const payload: GatewayRequest = action === 'teleport' ? request : { action, body: {
      destination: { kind: 'here' }, agent_id: 'claude', model: 'sonnet', brief: 'Review the patch', hint: 'Handing over', effort: 'high',
    } }
    const fetch = mock.method(globalThis, 'fetch', async () => sse(result, { ...gateway, action }))
    const events: WireEvent[] = []
    assert.deepEqual(await streamGateway('source/id', payload, (event) => events.push(event)), { ...gateway, action })
    assert.deepEqual(events, [result])
    assert.equal(fetch.mock.callCount(), 1)
    const [url, init] = fetch.mock.calls[0].arguments
    assert.equal(url, `/api/trunkline/threads/source%2Fid/${action}`)
    assert.equal(init?.method, 'POST')
    assert.equal(new Headers(init?.headers).get('X-Octomate-Request'), '1')
    assert.equal(new Request('https://example.test', init).credentials, 'same-origin')
    assert.deepEqual(JSON.parse(String(init?.body)), payload.body)
  })
}

test('eligibility preserves the backend reasons and routes', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json(options))
  assert.deepEqual(await fetchThreadOperations('source/id'), options)
  assert.equal(fetch.mock.calls[0].arguments[0], '/api/trunkline/threads/source%2Fid/operations')
})

test('HTTP refusals preserve their reason and are not retried', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'The agent is busy.' }, { status: 409 }))
  await assert.rejects(streamGateway('source', request, () => {}), /The agent is busy/)
  assert.equal(fetch.mock.callCount(), 1)
})

test('a failed or incomplete stream cannot report a successful gateway action', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => sse(result, gateway, { event_kind: 'run_error', message: 'Landing failed' }))
  await assert.rejects(streamGateway('source', request, () => {}), /Landing failed/)
  fetch.mock.mockImplementation(async () => sse(result))
  await assert.rejects(streamGateway('source', request, () => {}), /without confirming a destination/)
})

test('successful arrival selects the thread matching the complete channel address', async () => {
  const wrong = { ...destination, id: 'wrong', chat_id: 'other-account' }
  mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => String(url).endsWith('/teleport') ? sse(result, gateway) : Response.json([wrong, destination]))
  const select = mock.method(useConsole.getState().actions, 'selectThread', async () => {})
  await useConsole.getState().actions.gateway('source', request)
  assert.deepEqual(select.mock.calls[0].arguments, ['lark', 'destination-id'])
  assert.equal(useConsole.getState().gatewayPending, null)
})

test('arrival does not navigate away from a different thread selected during the run', async () => {
  mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => {
    if (String(url).endsWith('/teleport')) {
      useConsole.setState({ selThreadId: 'another' })
      return sse(result, gateway)
    }
    return Response.json([destination])
  })
  const select = mock.method(useConsole.getState().actions, 'selectThread', async () => {})
  await useConsole.getState().actions.gateway('source', request)
  assert.equal(select.mock.callCount(), 0)
})

test('missing destination visibility is reported in the message panel', async () => {
  mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => String(url).endsWith('/teleport') ? sse(result, gateway) : Response.json([]))
  const select = mock.method(useConsole.getState().actions, 'selectThread', async () => {})
  await useConsole.getState().actions.gateway('source', request)
  assert.equal(select.mock.callCount(), 0)
  assert.ok(useConsole.getState().notices.some((one) => one.kind === 'notice' && one.tone === 'error' && one.text.includes('not visible')))
})

test('an error after a run result still appears in the message panel', async () => {
  mock.method(globalThis, 'fetch', async () => sse(result, { event_kind: 'run_error', message: 'The destination could not create a thread.' }))
  await useConsole.getState().actions.gateway('source', request)
  assert.ok(useConsole.getState().notices.some((one) => one.kind === 'notice' && one.text.includes('could not create a thread')))
})

test('the header uses backend eligibility and the summon form offers only supplied routes', () => {
  useConsole.getInitialState().selThreadId = 'source'
  useConsole.getInitialState().detail = { key: 'source', msgCount: 0, sessions: [], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } }
  queryClient.setQueryData(['threads'], {})
  queryClient.setQueryData(['thread-operations', 'source'], options)
  const header = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ChatHeader)))
  assert.ok(header.includes('>Summon</span></button>'))
  const form = renderToStaticMarkup(createElement(GatewayDialog, { action: 'summon', availability: options.summon, onSubmit() {}, onClose() {} }))
  assert.ok(form.includes('aria-label="claude models"'))
  assert.ok(form.includes('>sonnet</button>'))
  assert.ok(!form.includes('Opening message'))
  assert.ok(form.includes('maxLength="8000"'))
  assert.ok(form.includes('type="range"'))
  assert.ok(form.includes('aria-valuetext="Agent default"'))
  assert.match(form, /type="submit" disabled=""/)
})

for (const [teleport, summon, fork, expected] of [
  [true, true, true, 'Teleport'],
  [false, true, true, 'Summon'],
  [false, false, true, 'Fork'],
  [false, false, false, 'Teleport'],
] as const) {
  test(`operation priority picks ${expected} with eligibility ${teleport}/${summon}/${fork}`, () => {
    useConsole.getInitialState().selThreadId = 'source'
    useConsole.getInitialState().detail = { key: 'source', canFork: fork, msgCount: 0, sessions: [], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } }
    queryClient.setQueryData(['threads'], {})
    queryClient.setQueryData(['thread-operations', 'source'], {
      teleport: teleport ? options.summon : options.teleport,
      summon: summon ? options.summon : options.teleport,
    })
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ChatHeader)))
    assert.ok(html.includes(`>${expected}</span></button>`))
    assert.equal(html.includes('aria-disabled="true"'), !teleport && !summon && !fork)
  })
}
