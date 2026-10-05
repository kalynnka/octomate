import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClientProvider } from '@tanstack/react-query'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiAgentRoute, ApiThread, ChannelAddress, GatewayEvent, GatewayRequest, OperationAvailability, ThreadOperations, WireEvent } from '../src/lib/api/events.ts'

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
const request: GatewayRequest = { action: 'teleport', body: { destination: room, hint: 'Continue here' } }
const gateway: GatewayEvent = { event_kind: 'gateway', action: 'teleport', destination: {
  channel_tentacle_id: 'lark', chat_type: 'thread', chat_id: 'account', channel_thread_id: 'platform-id', user_id: 'owner', shared: false,
} }
const result: WireEvent = { event_kind: 'run_result', output: 'Arrived', usage: { requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 } }
const refused: OperationAvailability = { destinations: [], here: null, routes: {}, reason: 'This native session cannot teleport.' }
// No suggested address at all: the in-place handover alone keeps Summon open.
const inPlace: OperationAvailability = { destinations: [], here, routes: { trunkline: routes }, reason: null }
const options: ThreadOperations = { source: here, teleport: refused, summon: inPlace, barred: {} }
const sse = (...events: WireEvent[]) => new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), { headers: { 'Content-Type': 'text/event-stream' } })

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

test('the effort scale only ever lands on a level the route takes', () => {
  const supported = ['auto', 'medium', 'high'] as const
  assert.equal(forms.stepEffort('auto', 'minimal', supported), 'medium')
  assert.equal(forms.stepEffort('high', 'xhigh', supported), 'high')
  assert.equal(forms.stepEffort('medium', 'low', supported), 'auto')
  assert.equal(forms.nearestEffort('xhigh', supported), 'high')
  assert.equal(forms.nearestEffort('medium', supported), 'medium')
  assert.equal(forms.nearestEffort('low', ['auto']), 'auto')
  assert.equal(forms.pickRoute(routes, 'claude', 'haiku'), routes[1])
  assert.equal(forms.pickRoute(routes, null, null), routes[0])
  assert.deepEqual(forms.routeEffort(routes[0], 'auto'), { efforts: ['auto', 'low', 'high'], effort: 'auto' })
  const known = { ...routes[0], claim: { ...routes[0].claim, default_effort: 'high' as const } }
  assert.deepEqual(forms.routeEffort(known, 'auto'), { efforts: ['low', 'high'], effort: 'high' })
  // A level picked on another route is kept where this one takes it, else its nearest.
  assert.deepEqual(forms.routeEffort(known, 'low'), { efforts: ['low', 'high'], effort: 'low' })
  assert.deepEqual(forms.routeEffort(known, 'xhigh'), { efforts: ['low', 'high'], effort: 'high' })
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

const landedAt: ChannelAddress = { channel_tentacle_id: 'trunkline', chat_type: 'thread', chat_id: 'owner', user_id: 'owner', channel_thread_id: 'landed-key', shared: false }
const started: WireEvent = { event_kind: 'custom', name: 'run_started', address: landedAt }
const refusedRun: WireEvent = { event_kind: 'run_error', message: 'The model refused.' }

test('a run started where the move landed carries its own failure', async () => {
  mock.method(globalThis, 'fetch', async () => sse(started, refusedRun))
  const events: WireEvent[] = []
  assert.equal(await streamGateway('source', request, (event) => events.push(event)), undefined)
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
  const { selected, state } = await followMove(started, result, { ...gateway, destination: landedAt })
  assert.deepEqual(selected, [['trunkline', 'landed-id']])
  assert.ok(state.live.some((item) => item.kind === 'stream' && item.text === 'Arrived'))
  assert.equal(state.running, false)
  assert.equal(state.gatewayPending, null)
})

test('a run that fails after the move shows its failure where it landed', async () => {
  const { selected, state } = await followMove(started, refusedRun)
  assert.deepEqual(selected, [['trunkline', 'landed-id']])
  assert.ok(state.live.some((item) => item.kind === 'notice' && item.text.includes('The model refused.')))
  assert.deepEqual(state.notices, [])
})

test('successful arrival selects the thread matching the complete channel address', async () => {
  const wrong = { ...destination, id: 'wrong', chat_id: 'other-account' }
  mock.method(globalThis, 'fetch', async (url: Parameters<typeof fetch>[0]) => String(url).endsWith('/teleport') ? sse(result, gateway) : Response.json([wrong, destination]))
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
      return sse(result, gateway)
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
  assert.ok(preset.includes('aria-valuetext="High · agent default"'))
  assert.ok(preset.includes('max="4"'))
  assert.ok(!preset.includes('>Auto<'))
  const none = renderToStaticMarkup(createElement(SummonRoute, { routes: [], route: undefined, efforts: ['auto'], effort: 'auto', open: true, onOpen() {}, onRoute() {}, onEffort() {} }))
  assert.match(none, /<button[^>]*disabled=""/)
  assert.ok(!none.includes('type="range"'))
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

test('a line is a command only while it can still name a gateway op', () => {
  const names = (text: string) => {
    const line = commands.readCommand(text)
    return line?.phase === 'name' ? line.matches.map((one) => one.command.name) : line
  }
  assert.deepEqual(names('/'), ['summon', 'teleport'])
  assert.deepEqual(names('/o'), ['summon', 'teleport'])
  assert.deepEqual(names('/tp'), ['teleport'])
  // Anything else starting with a slash is still a directive.
  for (const text of ['/etc/hosts is wrong', '/compact', '/summon\nnow', '/fork now', 'run /fork', '']) {
    assert.equal(commands.readCommand(text), null, text)
  }
  const line = commands.readCommand('/summon cl')
  assert.deepEqual(line && line.phase === 'argument' && [line.command.name, line.typed], ['summon', 'cl'])
  assert.deepEqual(commands.fuzzy('tp', 'teleport'), [0, 4])
  assert.equal(commands.fuzzy('pl', 'teleport'), null)
  assert.deepEqual(commands.highlight('/teleport', [1, 5]), [
    { text: '/', hit: false }, { text: 't', hit: true }, { text: 'ele', hit: false }, { text: 'p', hit: true }, { text: 'ort', hit: false },
  ])
})

test('an argument narrows to what was typed and completes from the one under the cursor', () => {
  const offered = [{ value: 'claude', about: 'Code review' }, { value: 'codex', about: 'Refactors' }, { value: 'inkling', about: 'Triage' }]
  const naming = commands.readCommand('/su')!
  assert.equal(commands.completion(naming, [], 0), 'mmon <agent>')
  // A name matched out of order has nothing to complete in place.
  assert.equal(commands.completion(commands.readCommand('/tp')!, [], 0), '')
  const line = commands.readCommand('/summon c')!
  const matches = commands.matching('c', offered)
  assert.deepEqual(matches.map((one) => one.argument.value), ['claude', 'codex'])
  assert.equal(commands.completion(line, matches, 1), 'odex')
  assert.equal(commands.completion(commands.readCommand('/summon ')!, commands.matching('', offered), 0), 'claude')
  assert.equal(commands.completion(commands.readCommand('/teleport ')!, [], 0), '[destination]')
  assert.equal(commands.completion(commands.readCommand('/summon zz')!, [], 0), '')
})

test('the command finder lists the ops, then the agents with the route one would get', () => {
  const panel = (text: string, props: Partial<Parameters<typeof CommandPanel>[0]> = {}) => renderToStaticMarkup(createElement(CommandPanel, {
    line: commands.readCommand(text)!, matches: [], offered: 0, cursor: 0, closed: {}, route: null,
    onCursor() {}, onRun() {}, onDismiss() {}, onSettled() {}, ...props,
  }))
  const naming = panel('/', { cursor: 1, closed: { teleport: 'No connected channel runs this agent.' } })
  assert.ok(naming.includes('2 of 2 · trunkline gateway'))
  assert.equal(naming.match(/role="option"/g)?.length, 2)
  assert.match(naming, /aria-selected="true" aria-disabled="true" title="No connected channel runs this agent\."/)
  assert.ok(naming.includes('>unavailable<'))
  assert.ok(panel('/su').includes('needs &lt;agent&gt; · ⇥ to add it · runs in trunkline gateway'))
  assert.ok(panel('/tele').includes('↵ runs /teleport in trunkline gateway · ⇥ adds [destination]'))
  const offered = [{ value: 'claude', about: 'Code review' }, { value: 'codex', about: 'Refactors' }]
  const known = { ...routes[0], claim: { ...routes[0].claim, default_effort: 'high' as const } }
  const summon = panel('/summon cl', {
    matches: commands.matching('cl', offered), offered: 2,
    route: { routes: [known, routes[1]], route: known, ...forms.routeEffort(known, 'auto'), onModel() {}, onEffort() {} },
  })
  assert.ok(summon.includes('1 of 2 agents · trunkline gateway'))
  assert.ok(summon.includes('for claude'))
  assert.match(summon, /aria-pressed="true"[^>]*>sonnet<\/span>/)
  assert.ok(summon.includes('aria-valuetext="High · agent default"'))
  assert.ok(summon.includes('↵ runs /summon claude · ⇥ fills it in'))
  assert.ok(summon.includes('Code review'))
  assert.ok(panel('/summon zz', { offered: 2 }).includes('no agent matches &quot;zz&quot;'))
  assert.ok(panel('/summon ', { closed: { summon: 'No other agent is connected on any channel.' } }).includes('No other agent is connected on any channel.'))
})

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
