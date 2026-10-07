import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement, useState } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiAgentInfo, ApiAgentRoute, ApiCommandDescriptor, ApiThread, ChannelAddress, CommandStreamEvent, GatewayEvent, GatewayRequest, OperationAvailability, ThreadOperations, WireEvent } from '../src/lib/api/events.ts'

let server: ViteDevServer
let streamGateway: typeof import('../src/lib/api/client.ts').streamGateway
let fetchThreadOperations: typeof import('../src/lib/api/client.ts').fetchThreadOperations
let fetchAddresses: typeof import('../src/lib/api/client.ts').fetchAddresses
let forms: typeof import('../src/features/chat/gateway.ts')
let commands: typeof import('../src/features/chat/commands.ts')
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let ChatHeader: typeof import('../src/features/chat/ChatHeader.tsx').ChatHeader
let SummonRoute: typeof import('../src/features/chat/GatewayRoute.tsx').SummonRoute
let DestinationPicker: typeof import('../src/features/chat/GatewayDestination.tsx').DestinationPicker
let CommandPanel: typeof import('../src/features/chat/CommandPanel.tsx').CommandPanel
const globals = ['document', 'requestAnimationFrame'].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const)
const destination: ApiThread = {
  id: 'destination-id', kind: 'thread', chat_type: 'thread', chat_id: 'account',
  channel_tentacle_id: 'lark', channel_thread_id: 'platform-id', title: 'Arrived',
  project_id: null, status: 'active', created_at: '', updated_at: '', handoffs: [],
}
const here: ChannelAddress = { channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: 'source-thread', shared: false }
const fresh: ChannelAddress = { channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: null, shared: false }
const room: ChannelAddress = {
  channel_tentacle_id: 'discord', chat_type: 'group', chat_id: '402', user_id: '100', channel_thread_id: null, shared: true,
  metadata: { name: 'general', server: 'Community' },
}
const routes: ApiAgentRoute[] = [
  { agent_id: 'claude', model: 'sonnet', claim: { ability: 'Code review', efforts: ['low', 'high'], default_effort: null } },
  { agent_id: 'claude', model: 'haiku', claim: { ability: 'Quick answers', efforts: [], default_effort: null } },
]
const compact: ApiCommandDescriptor = { id: 'claude:compact', name: 'compact', description: 'Summarize the conversation to free context.', argument_hint: '[instructions]', accepts_attachments: null }
const cost: ApiCommandDescriptor = { id: 'claude:cost', name: 'cost', description: 'Token usage and spend for this session.', argument_hint: null, accepts_attachments: null }
const request: GatewayRequest = { action: 'teleport', body: { destination: room, hint: 'Continue here' } }
const gateway: GatewayEvent = { event_kind: 'gateway', action: 'teleport', announcement: 'Continue here', destination: {
  channel_tentacle_id: 'lark', chat_type: 'thread', chat_id: 'account', channel_thread_id: 'platform-id', user_id: 'owner', shared: false,
} }
const result: WireEvent = { event_kind: 'run_result', output: 'Arrived', usage: { requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 } }
const refused: OperationAvailability = { destinations: [], here: null, routes: {}, reason: 'This native session cannot teleport.' }
// No suggested address at all: the in-place handover alone keeps Summon open.
const inPlace: OperationAvailability = { destinations: [], here, routes: { trunkline: routes }, reason: null }
const options: ThreadOperations = { source: here, teleport: refused, summon: inPlace, barred: {} }
const sse = (...events: CommandStreamEvent[]) => new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } })

before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom' })
  ;({ streamGateway, fetchThreadOperations, fetchAddresses } = await server.ssrLoadModule('/src/lib/api/client.ts'))
  forms = await server.ssrLoadModule('/src/features/chat/gateway.ts') as typeof forms
  commands = await server.ssrLoadModule('/src/features/chat/commands.ts') as typeof commands
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ ChatHeader } = await server.ssrLoadModule('/src/features/chat/ChatHeader.tsx'))
  ;({ SummonRoute } = await server.ssrLoadModule('/src/features/chat/GatewayRoute.tsx'))
  ;({ DestinationPicker } = await server.ssrLoadModule('/src/features/chat/GatewayDestination.tsx'))
  ;({ CommandPanel } = await server.ssrLoadModule('/src/features/chat/CommandPanel.tsx'))
})
beforeEach(() => {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { getElementById: () => null } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  useConsole.setState({ selThreadId: 'source', ntOn: false, running: false, gatewayPending: null, gatewayMode: null, live: [], notices: [] })
})
afterEach(() => {
  mock.restoreAll()
  queryClient.clear()
  useConsole.getInitialState().detail = null
  useConsole.getInitialState().selThreadId = ''
  useConsole.getInitialState().gatewayMode = null
  for (const [key, descriptor] of globals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else Reflect.deleteProperty(globalThis, key)
  }
})
after(async () => { await server?.close() })

for (const action of ['teleport', 'summon'] as const) {
  test(`${action} posts the typed payload once and waits for its confirmed destination`, async () => {
    const payload: GatewayRequest = action === 'teleport' ? request : { action, body: {
      agent_id: 'claude', model: 'sonnet', brief: 'Review the patch', hint: 'Handing over', effort: 'high',
    } }
    const fetch = mock.method(globalThis, 'fetch', async () => sse({ ...gateway, action }, result))
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

test('a destination level is listed on its own, and a refusal keeps its reason', async () => {
  const server = { ...room, chat_id: '', metadata: { name: 'Community', inside: '201' } }
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json([server]))
  assert.deepEqual(await fetchAddresses('source/id', 'discord'), [server])
  assert.equal(fetch.mock.calls[0].arguments[0], '/api/trunkline/threads/source%2Fid/channels/discord/addresses')
  fetch.mock.mockImplementation(async () => Response.json([room]))
  assert.deepEqual(await fetchAddresses('source/id', 'discord', '201'), [room])
  assert.equal(fetch.mock.calls[1].arguments[0], '/api/trunkline/threads/source%2Fid/channels/discord/addresses?inside=201')
  fetch.mock.mockImplementation(async () => Response.json({ detail: 'This channel cannot be browsed for a destination.' }, { status: 409 }))
  await assert.rejects(fetchAddresses('source', 'lark'), (error: Error & { status?: number }) =>
    error.status === 409 && /cannot be browsed/.test(error.message))
})

test('summon hands this conversation over where it is, with the draft as its brief', () => {
  const draft = { text: ' Review the patch ', destination: null, route: routes[0], effort: 'high' as const }
  assert.deepEqual(forms.gatewayRequest('summon', draft), { action: 'summon', body: {
    agent_id: 'claude', model: 'sonnet', brief: 'Review the patch', hint: 'Continuing with another agent.', effort: 'high',
  } })
  assert.deepEqual(forms.gatewayRequest('summon', { ...draft, effort: 'auto' }), { action: 'summon', body: {
    agent_id: 'claude', model: 'sonnet', brief: 'Review the patch', hint: 'Continuing with another agent.',
  } })
  assert.equal(forms.gatewayRequest('summon', { ...draft, text: '  ' }), null)
  assert.equal(forms.gatewayRequest('summon', { ...draft, route: undefined }), null)
})

test('teleport needs a destination, and its prompt is the message sent where it lands', () => {
  const draft = { text: '', destination: null, route: undefined, effort: 'auto' as const }
  assert.equal(forms.gatewayRequest('teleport', draft), null)
  const destination = { address: fresh, path: ['Trunkline'] }
  assert.deepEqual(forms.gatewayRequest('teleport', { ...draft, destination }),
    { action: 'teleport', body: { destination: fresh, hint: 'Continuing this conversation here.' } })
  assert.deepEqual(forms.gatewayRequest('teleport', { ...draft, destination, text: 'Pick this up on the phone' }),
    { action: 'teleport', body: { destination: fresh, hint: 'Continuing this conversation here.', prompt: 'Pick this up on the phone' } })
})

test('a retained gateway command cannot be submitted as a prompt or brief', () => {
  const draft = { destination: { address: fresh, path: ['Trunkline'] }, route: routes[0], effort: 'high' }
  for (const text of ['/', '/teleport', '/teleport discord/']) {
    assert.ok(forms.isGatewayCommand(text, 'teleport'))
    assert.equal(forms.gatewayRequest('teleport', { ...draft, text }), null)
  }
  for (const text of ['/', '/summon', '/summon claude']) {
    assert.ok(forms.isGatewayCommand(text, 'summon'))
    assert.equal(forms.gatewayRequest('summon', { ...draft, text }), null)
  }
  assert.equal(forms.isGatewayCommand('/etc/hosts needs a fix', 'summon'), false)
  assert.equal(forms.isGatewayCommand('teleport discord/', 'teleport'), false)
})

test('the effort scale only ever lands on a level the route takes', () => {
  assert.equal(forms.pickRoute(routes, 'claude', 'haiku'), routes[1])
  assert.equal(forms.pickRoute(routes, null, null), routes[0])
  assert.deepEqual(forms.routeEffort(routes[0], 'auto'), { efforts: ['auto', 'low', 'high'], effort: 'auto' })
  const known = { ...routes[0], claim: { ...routes[0].claim, default_effort: 'high' as const } }
  assert.deepEqual(forms.routeEffort(known, 'auto'), { efforts: ['low', 'high'], effort: 'high' })
  // A level picked on another route is kept where this one takes it, else the new route's default.
  assert.deepEqual(forms.routeEffort(known, 'low'), { efforts: ['low', 'high'], effort: 'low' })
  assert.deepEqual(forms.routeEffort(known, 'xhigh'), { efforts: ['low', 'high'], effort: 'high' })
  const native = { ...routes[0], claim: { ability: 'Native', efforts: ['none', 'max', 'ultra', 'plugin-effort'], default_effort: null } }
  assert.deepEqual(forms.routeEffort(native, 'ultra'), { efforts: ['auto', 'none', 'max', 'ultra', 'plugin-effort'], effort: 'ultra' })
  assert.equal(forms.routeEffort(native, 'xhigh').effort, 'auto')
})

test('HTTP refusals preserve their reason and are not retried', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'The agent is busy.' }, { status: 409 }))
  await assert.rejects(streamGateway('source', request, () => {}), /The agent is busy/)
  assert.equal(fetch.mock.callCount(), 1)
})

test('a failed or incomplete stream cannot report a successful gateway action', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => sse(gateway, { event_kind: 'run_error', message: 'Landing failed', trace_id: 'abc' }))
  await assert.rejects(streamGateway('source', request, () => {}), /Landing failed/)
  fetch.mock.mockImplementation(async () => sse(result))
  await assert.rejects(streamGateway('source', request, () => {}), /without confirming a destination/)
  // Another spell's move does not confirm this one.
  fetch.mock.mockImplementation(async () => sse({ ...gateway, action: 'scheme' }, result))
  await assert.rejects(streamGateway('source', request, () => {}), /without confirming a destination/)
})

test('a move the agent makes after the operation is where it ends', async () => {
  const onward: GatewayEvent = { ...gateway, action: 'scheme', announcement: null, destination: { ...gateway.destination, chat_type: 'dm', channel_thread_id: null } }
  mock.method(globalThis, 'fetch', async () => sse(gateway, result, onward))
  assert.deepEqual(await streamGateway('source', request, () => {}), onward)
})

const landedAt: ChannelAddress = { channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: 'landed-key', shared: false }
const started: WireEvent = { event_kind: 'custom', name: 'run_started', address: landedAt }
const refusedRun: WireEvent = { event_kind: 'run_error', message: 'The model refused.', trace_id: 'abc' }
const moved: GatewayEvent = { ...gateway, destination: landedAt }

test('a run started where the move landed carries its own failure', async () => {
  mock.method(globalThis, 'fetch', async () => sse(moved, started, refusedRun))
  const events: WireEvent[] = []
  assert.deepEqual(await streamGateway('source', request, (event) => events.push(event)), moved)
  assert.deepEqual(events, [started, refusedRun])
})

/** Teleport from `source` over `events`, with selection doing what the real one does to the state. */
async function followMove(...events: WireEvent[]) {
  const landed: ApiThread = { ...destination, id: 'landed-id', channel_tentacle_id: 'trunkline', chat_id: 'owner', channel_thread_id: 'landed-key' }
  mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => String(url).endsWith('/teleport') ? sse(...events) : Response.json([landed]))
  const select = mock.method(useConsole.getState().actions, 'selectThread', async (_channel: string, id: string) => {
    useConsole.setState({ selThreadId: id, live: [], running: false })
  })
  useConsole.setState({ detail: { key: 'source', live: true, sendKey: 'source-key', msgCount: 0, sessions: [], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } } })
  await useConsole.getState().actions.gateway('source', request)
  return { selected: select.mock.calls.map((call) => call.arguments), state: useConsole.getState() }
}

test('a run starting where the move landed opens that thread at once and streams there', async () => {
  const { selected, state } = await followMove(moved, started, result)
  assert.deepEqual(selected, [['trunkline', 'landed-id']])
  assert.ok(state.live.some((item) => item.kind === 'stream' && item.text === 'Arrived'))
  assert.equal(state.running, false)
  assert.equal(state.gatewayPending, null)
})

test('a run that fails after the move shows its failure where it landed', async () => {
  const { selected, state } = await followMove(moved, started, refusedRun)
  assert.deepEqual(selected, [['trunkline', 'landed-id']])
  assert.ok(state.live.some((item) => item.kind === 'notice' && item.text.includes('The model refused.')))
  assert.deepEqual(state.notices, [])
})

test('successful arrival selects the thread matching the complete channel address', async () => {
  const wrong = { ...destination, id: 'wrong', chat_id: 'other-account' }
  mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => String(url).endsWith('/teleport') ? sse(gateway, result) : Response.json([wrong, destination]))
  const select = mock.method(useConsole.getState().actions, 'selectThread', async () => {})
  useConsole.getState().actions.setGatewayMode({ threadId: 'source', action: 'teleport' })
  await useConsole.getState().actions.gateway('source', request)
  assert.deepEqual(select.mock.calls[0].arguments, ['lark', 'destination-id'])
  assert.equal(useConsole.getState().gatewayPending, null)
  assert.equal(useConsole.getState().gatewayMode, null)
})

test('arrival does not navigate away from a different thread selected during the run', async () => {
  mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => {
    if (String(url).endsWith('/teleport')) {
      useConsole.setState({ selThreadId: 'another' })
      return sse(gateway, result)
    }
    return Response.json([destination])
  })
  const select = mock.method(useConsole.getState().actions, 'selectThread', async () => {})
  await useConsole.getState().actions.gateway('source', request)
  assert.equal(select.mock.callCount(), 0)
})

test('a turn the agent moved opens where it landed, and one that stayed opens nothing', async () => {
  for (const events of [[result, gateway], [result]]) {
    mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => String(url).endsWith('/messages') ? sse(...events) : Response.json([destination]))
    const select = mock.method(useConsole.getState().actions, 'selectThread', async () => {})
    useConsole.setState({ detail: { key: 'source', live: true, sendKey: 'source-key', msgCount: 0, sessions: [], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } } })
    useConsole.getState().actions.sendDirective('take this elsewhere')
    const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
    for (let at = 0; at < 200 && (useConsole.getState().running || (events.length === 2 && select.mock.callCount() === 0)); at++) await tick()
    for (let at = 0; at < 5; at++) await tick()
    assert.deepEqual(select.mock.calls.map((call) => call.arguments), events.length === 2 ? [['lark', 'destination-id']] : [])
    mock.restoreAll()
  }
})

test('missing destination visibility is reported in the message panel', async () => {
  mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => String(url).endsWith('/teleport') ? sse(gateway, result) : Response.json([]))
  const select = mock.method(useConsole.getState().actions, 'selectThread', async () => {})
  await useConsole.getState().actions.gateway('source', request)
  assert.equal(select.mock.callCount(), 0)
  assert.ok(useConsole.getState().notices.some((one) => one.kind === 'notice' && one.tone === 'error' && one.text.includes('not visible')))
})

test('an error after a run result still appears in the message panel', async () => {
  mock.method(globalThis, 'fetch', async () => sse(result, { event_kind: 'run_error', message: 'The destination could not create a thread.', trace_id: 'abc' }))
  await useConsole.getState().actions.gateway('source', request)
  assert.ok(useConsole.getState().notices.some((one) => one.kind === 'notice' && one.text.includes('could not create a thread')))
})

test('the header follows the relay: an in-place summon is open with no suggested address', () => {
  useConsole.getInitialState().selThreadId = 'source'
  useConsole.getInitialState().detail = { key: 'source', msgCount: 0, sessions: [], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } }
  queryClient.setQueryData(['threads'], {})
  queryClient.setQueryData(['thread-operations', 'source'], options)
  const header = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ChatHeader)))
  assert.ok(header.includes('>Summon</span></button>'))
  assert.ok(header.includes('aria-pressed="false"'))
  assert.ok(header.includes('title="Current surface"'))
})

test('the header selects the operation opened through a command', () => {
  useConsole.getInitialState().selThreadId = 'source'
  useConsole.getInitialState().gatewayMode = { threadId: 'source', action: 'summon' }
  useConsole.getInitialState().detail = { key: 'source', msgCount: 0, sessions: [], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } }
  queryClient.setQueryData(['threads'], {})
  queryClient.setQueryData(['thread-operations', 'source'], { ...options, teleport: { ...inPlace, destinations: [fresh] } })
  const header = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ChatHeader)))
  assert.ok(header.includes('>Summon</span></button>'))
  assert.ok(header.includes('aria-pressed="true" title="Back to chat"'))
})

test('the summon route offers only the supplied routes and a real effort slider', () => {
  const closed = renderToStaticMarkup(createElement(SummonRoute, { routes, route: routes[0], efforts: ['auto', 'low', 'high'], effort: 'auto', open: false, onOpen() {}, onRoute() {}, onEffort() {} }))
  assert.ok(closed.includes('aria-expanded="false"'))
  assert.ok(!closed.includes('type="range"'))
  const open = renderToStaticMarkup(createElement(SummonRoute, { routes, route: routes[0], efforts: ['auto', 'low', 'high'], effort: 'auto', open: true, onOpen() {}, onRoute() {}, onEffort() {} }))
  assert.ok(open.includes('aria-label="claude models"'))
  assert.match(open, /aria-pressed="true"[^>]*>sonnet<\/button>/)
  assert.match(open, /aria-pressed="false"[^>]*>haiku<\/button>/)
  assert.ok(open.includes('Code review'))
  assert.ok(open.includes('type="range"'))
  assert.ok(open.includes('aria-valuetext="Auto · agent default"'))
  assert.ok(open.includes('model runs low–high'))
  assert.ok(open.includes('>Auto<'))
  // A route that says what it runs at by default starts there, and Auto is gone.
  const known = { ...routes[0], claim: { ...routes[0].claim, default_effort: 'high' as const } }
  const preset = renderToStaticMarkup(createElement(SummonRoute, { routes: [known], route: known, ...forms.routeEffort(known, 'auto'), open: true, onOpen() {}, onRoute() {}, onEffort() {} }))
  assert.ok(preset.includes('aria-valuetext="high · agent default"'))
  assert.ok(preset.includes('max="1"'))
  assert.ok(!preset.includes('>Auto<'))
  const native = { ...routes[0], claim: { ability: 'Native', efforts: ['none', 'max', 'ultra', 'plugin-effort'], default_effort: null } }
  const nativeScale = renderToStaticMarkup(createElement(SummonRoute, { routes: [native], route: native, ...forms.routeEffort(native, 'ultra'), open: true, onOpen() {}, onRoute() {}, onEffort() {} }))
  assert.ok(nativeScale.includes('aria-valuetext="ultra"'))
  for (const name of native.claim.efforts) assert.ok(nativeScale.includes(`>${name}<`))
  assert.ok(!nativeScale.includes('>xhigh<'))
  const none = renderToStaticMarkup(createElement(SummonRoute, { routes: [], route: undefined, efforts: ['auto'], effort: 'auto', open: true, onOpen() {}, onRoute() {}, onEffort() {} }))
  assert.match(none, /<button[^>]*disabled=""/)
  assert.ok(!none.includes('type="range"'))
  const reason = 'No other agent is connected on any channel.'
  const refused = renderToStaticMarkup(createElement(SummonRoute, { routes: [], route: undefined, efforts: ['auto'], effort: 'auto', open: true, reason, onOpen() {}, onRoute() {}, onEffort() {} }))
  assert.ok(refused.includes(reason))
  assert.ok(refused.includes('role="status"'))
})

test('the command finder and destination menu show the same refusal or empty-list reason', async () => {
  // Render the completed lookup; an SSR render cannot finish a mount-time retry.
  const client = new QueryClient({ defaultOptions: { queries: { retryOnMount: false } } })
  const key = ['thread-addresses', 'source', 'discord', null]
  const message = 'Link your profile on the destination channel first.'
  mock.method(globalThis, 'fetch', async () => Response.json({ detail: message }, { status: 409 }))
  await assert.rejects(client.fetchQuery({ queryKey: key, queryFn: () => fetchAddresses('source', 'discord'), retry: false }))
  const error = client.getQueryState(key)?.error
  assert.ok(error instanceof Error)
  assert.equal(forms.destinationReason(error, 'servers'), message)
  for (const reason of [forms.destinationReason(error, 'servers'), forms.destinationReason(null, 'servers')]) {
    const panel = renderToStaticMarkup(createElement(CommandPanel, {
      line: commands.readCommand('/teleport discord/', commands.COMMANDS)!,
      matches: [{ argument: { value: 'discord/', about: 'gateway · servers' }, hits: [] }],
      offered: 1, total: 4, agent: 'claude', nativeNote: null, argumentReason: reason,
      closed: {}, route: null, effort: null, onRun() {}, onDismiss() {}, onSettled() {},
    }))
    const picker = renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(DestinationPicker, {
      threadId: 'source', sourceChannel: 'trunkline', suggestions: [], routes: { discord: routes }, unrouted: 'does not run claude',
      barred: {}, selection: null, crumbs: [{ channel: 'discord', label: 'Discord' }], open: true, onOpen() {}, onCrumbs() {}, onSelect() {},
    })))
    assert.ok(panel.includes(reason))
    assert.ok(picker.includes(reason), picker)
    client.setQueryData(key, [])
  }
  client.clear()
})

test('the destination picker opens on the connected surfaces and marks what lands directly', () => {
  queryClient.setQueryData(['channels'], [
    { id: 'trunkline', label: 'Trunkline', sub: 'mention-free', brand: 'orange' },
    { id: 'discord', label: 'Discord', sub: 'gateway', brand: 'blue' },
    { id: 'lark', label: 'Lark', sub: 'webhook', brand: 'grey' },
    { id: 'napcat', label: 'NapCat', sub: 'onebot', brand: 'grey' },
  ])
  const picker = (open: boolean, selection: { address: ChannelAddress; path: string[] } | null) =>
    renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(DestinationPicker, {
      threadId: 'source', sourceChannel: 'trunkline', suggestions: [fresh], routes: { trunkline: routes, discord: routes, napcat: routes }, unrouted: 'does not run claude',
      barred: { napcat: 'This channel has no threads, so no conversation can land here.' }, selection, crumbs: [], open, onOpen() {}, onCrumbs() {}, onSelect() {},
    })))
  assert.ok(picker(false, null).includes('choose destination'))
  assert.ok(!picker(false, null).includes('Surfaces'))
  const root = picker(true, null)
  assert.ok(root.includes('4 surfaces'))
  assert.ok(root.includes('a new private thread'))
  assert.ok(root.includes('● here'))
  assert.ok(root.includes('gateway · servers'))
  // Lark has no route for the op, so it is listed and cannot be opened.
  assert.ok(!root.includes('webhook · destinations'))
  assert.match(root, /<button[^>]*disabled=""[^>]*>(?:(?!<\/button>).)*Lark(?:(?!<\/button>).)*does not run claude/)
  // NapCat runs the agent, but nothing can land there: listed, disabled, with why.
  assert.match(root, /<button[^>]*disabled=""[^>]*>(?:(?!<\/button>).)*NapCat(?:(?!<\/button>).)*no conversation can land here/)
  assert.ok(root.includes('title="Open Discord"'))
  // Only the surface that lands directly can be the selection at this level.
  assert.equal(root.match(/aria-pressed=/g)?.length, 1)
  const chosen = picker(true, { address: fresh, path: ['Trunkline'] })
  assert.ok(chosen.includes('aria-pressed="true"'))
  assert.ok(chosen.includes('→ Trunkline'))
  queryClient.setQueryData(['channels'], [{ id: 'discord', label: 'Discord', sub: 'gateway', brand: 'blue' }])
  assert.ok(picker(true, null).includes('1 surface<'))
})

test('a line is a command only while it can still name a gateway op or one of the agent\'s own', () => {
  const names = (text: string, offered = commands.COMMANDS) => {
    const line = commands.readCommand(text, offered)
    return line?.phase === 'name' ? line.matches.map((one) => one.command.name) : line
  }
  assert.deepEqual(names('/'), ['summon', 'teleport', 'effort', 'new'])
  assert.deepEqual(names('/o'), ['summon', 'teleport', 'effort'])
  assert.deepEqual(names('/tp'), ['teleport'])
  // Anything else starting with a slash is still a directive.
  for (const text of ['/etc/hosts is wrong', '/compact', '/summon\nnow', '/fork now', '/new now', 'run /fork', 'summon cl', 'teleport', '']) {
    assert.equal(commands.readCommand(text, commands.COMMANDS), null, text)
  }
  const line = commands.readCommand('/summon cl', commands.COMMANDS)
  assert.deepEqual(line && line.phase === 'argument' && [line.command.name, line.typed], ['summon', 'cl'])
  // An agent's own command joins once its catalog lists it, and takes what follows as typed.
  const withNative = [...commands.COMMANDS, commands.nativeCommand(compact), commands.nativeCommand(cost)]
  assert.deepEqual(names('/co', withNative), ['compact', 'cost'])
  const native = commands.readCommand('/cost  in detail', withNative)
  assert.deepEqual(native?.phase === 'argument' && [native.command.native?.id, native.typed], ['claude:cost', ' in detail'])
  assert.deepEqual(commands.fuzzy('tp', 'teleport'), [0, 4])
  assert.equal(commands.fuzzy('pl', 'teleport'), null)
  assert.deepEqual(commands.highlight('/teleport', [1, 5]), [
    { text: '/', hit: false }, { text: 't', hit: true }, { text: 'ele', hit: false }, { text: 'p', hit: true }, { text: 'ort', hit: false },
  ])
})

test('an argument narrows to what was typed and completes from the one under the cursor', () => {
  const offered = [{ value: 'claude', about: 'Code review' }, { value: 'codex', about: 'Refactors' }, { value: 'inkling', about: 'Triage' }]
  const read = (text: string) => commands.readCommand(text, commands.COMMANDS)!
  assert.equal(commands.completion(read('/su'), [], 0), 'mmon <agent> [--model <model>] [--effort <effort>]')
  // A name matched out of order has nothing to complete in place.
  assert.equal(commands.completion(read('/tp'), [], 0), '')
  const line = read('/summon c')
  const matches = commands.matching('c', offered)
  assert.deepEqual(matches.map((one) => one.argument.value), ['claude', 'codex'])
  assert.equal(commands.completion(line, matches, 1), 'odex [--model <model>] [--effort <effort>]')
  assert.equal(commands.completion(read('/summon '), commands.matching('', offered), 0), 'claude [--model <model>] [--effort <effort>]')
  assert.equal(commands.completion(read('/teleport '), [], 0), '[destination]')
  assert.equal(commands.completion(read('/summon zz'), [], 0), '')
})

test('summon completes optional native effort names and defaults only when effort is omitted', () => {
  const command = commands.COMMANDS.find((one) => one.name === 'summon')!
  const read = (typed: string) => ({ phase: 'argument' as const, command, typed })
  assert.deepEqual(commands.commandArguments(read('claude')).values, { agent: 'claude', model: '', effort: '' })
  assert.equal(commands.commandArguments(read('claude --effort ')).active?.name, 'effort')
  assert.deepEqual(commands.commandArguments(read('claude   --effort high')).values, { agent: 'claude', model: '', effort: 'high' })
  const effort = ['auto', 'low', 'high', 'plugin-effort'].map((value) => ({ value, about: '' }))
  for (const [typed, suffix] of [['claude --effort ', 'auto'], ['claude --effort h', 'igh'], ['claude   --effort h', 'igh'], ['claude --effort plugin-e', 'ffort'], ['claude --effort invalid', '']]) {
    const line = read(typed)
    assert.equal(commands.completion(line, commands.commandArguments(line, { effort }).matches, 0), suffix)
  }
  const route = { ...routes[0], claim: { ...routes[0].claim, default_effort: 'high' } }
  for (const typed of ['claude', 'claude ', 'claude --effort low']) {
    const args = commands.commandArguments(read(typed)).values
    const { effort } = forms.routeEffort(route, args.effort || 'auto')
    const request = forms.gatewayRequest('summon', { text: 'Review this', destination: null, route, effort })
    assert.equal(request?.action === 'summon' && request.body.effort, typed === 'claude --effort low' ? 'low' : 'high')
  }
})

test('the command finder lists the ops, then the agents with the route one would get', () => {
  const offeredCommands = [...commands.COMMANDS, commands.nativeCommand(compact), commands.nativeCommand(cost)]
  const panel = (text: string, props: Partial<Parameters<typeof CommandPanel>[0]> = {}) => renderToStaticMarkup(createElement(CommandPanel, {
    line: commands.readCommand(text, offeredCommands)!, matches: [], offered: 0, total: offeredCommands.length, agent: 'claude',
    nativeNote: null, closed: {}, route: null, effort: null,
    onRun() {}, onDismiss() {}, onSettled() {}, ...props,
  }))
  const naming = panel('/', { closed: { teleport: 'No connected channel runs this agent.' } })
  assert.ok(naming.includes('6 of 6 · claude'))
  assert.equal(naming.match(/role="option"/g)?.length, 6)
  assert.match(naming, /aria-selected="false" aria-disabled="true" title="No connected channel runs this agent\."/)
  assert.ok(naming.includes('>unavailable<'))
  // The gateway's ops come first, then the agent's own under a heading of their own.
  assert.ok(naming.indexOf('>Gateway<') < naming.indexOf('>claude · native<'))
  assert.ok(naming.indexOf('>claude · native<') < naming.indexOf('Summarize the conversation'))
  assert.ok(!naming.includes('<kbd'))
  // With none of the agent's own matching, its heading still says how discovery went.
  assert.ok(panel('/su', { nativeNote: 'discovering claude commands…' }).includes('>claude · native</span><span role="status"'))
  const free = panel('/compact keep the tests')
  assert.ok(free.includes('free text · claude · native'))
  assert.ok(free.includes('/compact keep the tests'))
  assert.ok(free.includes('[instructions]'))
  assert.ok(!free.includes('<kbd'))
  const levels = panel('/effort hi', {
    effort: { supported: ['auto', 'low', 'high'], preset: null, level: 'high', current: 'auto', route: 'claude · sonnet', onEffort() {} },
  })
  assert.ok(levels.includes('aria-valuetext="high"'))
  assert.ok(levels.includes('auto → high · claude · sonnet'))
  assert.ok(!levels.includes('<kbd'))
  const offered = [{ value: 'claude', about: 'Code review' }, { value: 'codex', about: 'Refactors' }]
  const known = { ...routes[0], claim: { ...routes[0].claim, default_effort: 'high' as const } }
  const summon = panel('/summon cl', {
    matches: commands.matching('cl', offered), offered: 2,
    route: { routes: [known, routes[1]], route: known, ...forms.routeEffort(known, 'auto'), onModel() {}, onEffort() {} },
  })
  assert.ok(summon.includes('1 of 2 agents · trunkline gateway'))
  assert.ok(!summon.includes('claude · native'))
  assert.ok(summon.includes('for claude'))
  assert.match(summon, /aria-pressed="true"[^>]*>sonnet<\/button>/)
  assert.ok(summon.includes('aria-valuetext="high · agent default"'))
  assert.ok(!summon.includes('<kbd'))
  assert.ok(summon.includes('Code review'))
  const manyModels = Array.from({ length: 11 }, (_, index) => ({ ...known, model: `model-${index}` }))
  const crowded = panel('/summon claude', {
    route: { routes: manyModels, route: manyModels[10], ...forms.routeEffort(known, 'auto'), onModel() {}, onEffort() {} },
  })
  assert.equal(crowded.match(/aria-pressed=/g)?.length, 6)
  assert.match(crowded, /aria-pressed="true"[^>]*>model-10<\/button>/)
  assert.ok(!crowded.includes('>model-9</button>'))
  assert.ok(crowded.includes('Show all 11 models'))
  const filtered = panel('/summon claude --model m9', {
    route: { routes: manyModels, route: manyModels[9], ...forms.routeEffort(known, 'auto'), onModel() {}, onEffort() {} },
  })
  assert.equal(filtered.match(/aria-pressed=/g)?.length, 1)
  assert.match(filtered, /aria-pressed="true"[^>]*>model-9<\/button>/)
  assert.ok(!filtered.includes('>model-10</button>'))
  assert.ok(!filtered.includes('role="listbox"'))
  const unmatched = panel('/summon claude --model unknown', {
    route: { routes: manyModels, route: known, ...forms.routeEffort(known, 'auto'), onModel() {}, onEffort() {} },
  })
  assert.ok(!unmatched.includes('aria-pressed='))
  const effortInput = panel('/summon claude --effort high', {
    matches: commands.matching('high', [{ value: 'high', about: 'High effort' }]), offered: 1,
    route: { routes: [known], route: known, ...forms.routeEffort(known, 'high'), onModel() {}, onEffort() {} },
  })
  assert.equal(effortInput.match(/type="range"/g)?.length, 1)
  assert.ok(!effortInput.includes('role="option"'))
  assert.ok(!effortInput.includes('role="listbox"'))
  assert.ok(panel('/summon zz', { offered: 2 }).includes('no agent matches &quot;zz&quot;'))
  assert.ok(panel('/summon ', { closed: { summon: 'No other agent is connected on any channel.' } }).includes('No other agent is connected on any channel.'))
})

for (const action of ['summon', 'teleport'] as const) {
  test(`${action} shares selections across slash entry, button entry and reopening`, () => {
    const selection = action === 'summon'
      ? { agent: 'claude', model: 'sonnet', effort: 'high' }
      : { destination: { address: room, path: ['Discord', 'general'] }, crumbs: [{ label: 'Discord', channel: 'discord' }] }
    function Draft() {
      const [step, advance] = useState(0)
      const [form, patch, seed] = forms.useGatewayForm(step === 5 ? 'another-thread' : 'source', step === 1 || step === 3 ? action : null)
      if (step === 0) seed(action, selection)
      else if (step === 5) {
        assert.equal(form.agent, null)
        assert.equal(form.destination, null)
        return null
      } else {
        assert.partialDeepStrictEqual(form, selection)
        if (step === 1) patch({ menu: action === 'summon' ? 'route' : 'destination' })
        if (step === 2) seed(action, {})
        if (step === 3) assert.equal(form.menu, action === 'summon' ? 'route' : 'destination')
      }
      advance(step + 1)
      return null
    }
    renderToStaticMarkup(createElement(Draft))
  })
}

test('a listed address reads as a place to open, one to land in, or one that is barred', () => {
  const crumbs = [{ label: 'Discord', channel: 'discord' }]
  const server = forms.addressRow({ ...room, chat_id: '', metadata: { name: 'Octomate Dev', inside: '201' } }, crumbs)
  assert.deepEqual(server.open, { label: 'Octomate Dev', channel: 'discord', inside: '201' })
  assert.equal(server.glyph, 'OD')
  assert.equal(server.sub, 'server · open to list its channels')
  const inside = [...crumbs, server.open!]
  const usable = forms.addressRow(room, inside)
  assert.deepEqual([usable.label, usable.sub, usable.barred], ['#general', 'Community · a new thread everyone there can read', undefined])
  assert.equal(forms.addressRow({ ...room, shared: false }, inside).sub, 'Community · a new private thread starts here')
  // Slack and Lark list one level, of channels and of groups.
  assert.equal(forms.level('slack', 0).many, 'channels')
  assert.equal(forms.addressRow({ ...room, channel_tentacle_id: 'lark', metadata: { name: 'team' } }, [{ label: 'Lark', channel: 'lark' }]).sub, 'group · a new thread everyone there can read')
  const barred = forms.addressRow({ ...room, metadata: { ...room.metadata, barred: 'The bot cannot see this channel.' } }, inside)
  assert.deepEqual([barred.label, barred.sub, barred.barred], ['#general', 'The bot cannot see this channel.', 'The bot cannot see this channel.'])
})

for (const [teleport, summon, expected] of [
  [true, true, 'Teleport'],
  [false, true, 'Summon'],
  [false, false, 'Teleport'],
] as const) {
  test(`operation priority picks ${expected} with eligibility ${teleport}/${summon}`, () => {
    useConsole.getInitialState().selThreadId = 'source'
    useConsole.getInitialState().detail = { key: 'source', msgCount: 0, sessions: [], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } }
    queryClient.setQueryData(['threads'], {})
    queryClient.setQueryData(['thread-operations', 'source'], {
      teleport: teleport ? inPlace : refused,
      summon: summon ? inPlace : refused,
    })
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ChatHeader)))
    assert.ok(html.includes(`>${expected}</span></button>`))
    assert.equal(html.includes('aria-disabled="true"'), !teleport && !summon)
  })
}

const session = {
  n: 'S1', id: 'SES-0001', conversationId: 'conversation-id', route: 'claude · sonnet', agent: 'claude', model: 'sonnet',
  effort: null, mode: null, kind: 'entry' as const, t: '', reason: '', status: 'active', tone: 'accent' as const,
}

test('an agent\'s own command streams its feedback into the ledger and ends on its outcome', async () => {
  const reply: CommandStreamEvent = { event_kind: 'message_sent', segments: [{ type: 'text', data: { text: 'session $1.84' } }] }
  const done: CommandStreamEvent = { event_kind: 'command_outcome', outcome: { status: 'completed', segments: [] } }
  const fetch = mock.method(globalThis, 'fetch', async () => sse(reply, done))
  useConsole.setState({ detail: { key: 'source', live: true, sendKey: 'source-key', msgCount: 0, sessions: [session], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } } })
  const context = { agent_id: 'claude', address: here, conversation_id: 'conversation-id' }
  useConsole.getState().actions.runCommand(context, cost, 'in detail')
  const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
  for (let at = 0; at < 200 && useConsole.getState().running; at++) await tick()

  const [url, init] = fetch.mock.calls[0].arguments
  assert.equal(url, '/api/commands/execute')
  const body = JSON.parse(String(init?.body))
  assert.deepEqual({ ...body, delivery_id: undefined }, { ...context, command_id: 'claude:cost', arguments: 'in detail', delivery_id: undefined })
  assert.match(body.delivery_id, /^[0-9a-f]{32}$/)
  const live = useConsole.getState().live
  assert.deepEqual(live.map((item) => item.kind), ['user', 'agent'])
  assert.ok(live[0].kind === 'user' && live[0].text === '/cost in detail')
  // The outcome closes the turn: no "stream closed" warning follows the feedback.
  assert.equal(useConsole.getState().running, false)
})

test('an effort set from the composer lands on the conversation and says so', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json({ id: 'conversation-id', effort: 'high' }))
  useConsole.setState({ detail: { key: 'source', live: true, sendKey: 'source-key', msgCount: 0, sessions: [session], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } } })
  await useConsole.getState().actions.setEffort('high')
  const [url, init] = fetch.mock.calls[0].arguments
  assert.equal(url, '/api/trunkline/conversations/conversation-id/effort')
  assert.equal(init?.method, 'PATCH')
  assert.deepEqual(JSON.parse(String(init?.body)), { effort: 'high' })
  assert.equal(useConsole.getState().detail?.sessions[0].effort, 'high')
  assert.ok(useConsole.getState().notices.some((one) => one.tone === 'info' && one.text === 'effort → high · claude · sonnet'))

  fetch.mock.mockImplementation(async () => Response.json({ detail: "claude (sonnet) does not take effort 'xhigh'; it claims low/high" }, { status: 422 }))
  await useConsole.getState().actions.setEffort('xhigh')
  assert.equal(useConsole.getState().detail?.sessions[0].effort, 'high')
  assert.ok(useConsole.getState().notices.some((one) => one.tone === 'error' && one.text.includes('does not take effort')))
})

test('effort choices match the native default and unambiguous runtime model names', () => {
  const route = { ...routes[0], agent_id: 'codex', model: 'openai:test-model' }
  const agent: ApiAgentInfo = {
    id: 'codex', description: '', gateway: true, default_model: route.model,
    routes: [route], permission_modes: [], default_permission_mode: null,
    driven_sessions: 0, native_sessions: 0,
  }
  for (const name of [null, 'test-model', 'openai:test-model']) {
    assert.deepEqual(forms.modelRoute(agent, name)?.claim.efforts, ['low', 'high'])
  }
  assert.equal(forms.modelRoute(agent, 'unknown'), undefined)
  assert.equal(forms.modelRoute({ ...agent, default_model: null }, null), undefined)
  assert.equal(forms.modelRoute({ ...agent, routes: [route, { ...route, model: 'other:test-model' }] }, 'test-model'), undefined)
})

for (const effort of ['high', 'auto']) {
  test(`the first message carries effort ${effort} without pinning the harness model`, async () => {
    const fetch = mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, init?: RequestInit) => init?.method === 'POST' ? sse(result) : Response.json([]))
    useConsole.setState({ ntOn: true, ntStarted: false, ntEffort: effort, ntRouteId: 'codex:', ntProject: null, ntPermissionMode: null })
    useConsole.getState().actions.sendNewThread('hello')
    for (let at = 0; at < 200 && useConsole.getState().running; at++) await new Promise((resolve) => setTimeout(resolve, 0))
    const sent = fetch.mock.calls.find((call) => call.arguments[1]?.method === 'POST')
    assert.ok(sent)
    const body = JSON.parse(String(sent.arguments[1]?.body))
    assert.deepEqual(body, { text: 'hello', model: 'codex:', ...(effort === 'auto' ? {} : { effort }) })
    assert.equal(useConsole.getState().running, false)
  })
}

test('changing a model clears the previous models effort selection', () => {
  useConsole.setState({ ntAgent: 'codex', ntModel: 'first', ntEffort: 'high' })
  useConsole.getState().actions.setNtRoute({ ntModel: 'second' })
  assert.equal(useConsole.getState().ntEffort, 'auto')
})
