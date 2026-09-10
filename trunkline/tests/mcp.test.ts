import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClientProvider } from '@tanstack/react-query'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiMcp, ApiMcpAuthorizationResult, ApiProfileInfo } from '../src/lib/api/events.ts'

let server: ViteDevServer
let connectMcp: typeof import('../src/lib/api/client.ts').connectMcp
let confirmMcp: typeof import('../src/lib/api/client.ts').confirmMcp
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let ControlPage: typeof import('../src/features/control/ControlPage.tsx').ControlPage
let McpAuthorizationDialog: typeof import('../src/features/control/McpAuthorizationDialog.tsx').McpAuthorizationDialog

const mcp: ApiMcp = {
  id: 'work-id', name: 'Work', namespace: 'personal/work', url: 'https://mcp.example/mcp',
  instructions: '', tentacle_id: 'github', enabled: true, auth_kind: 'oauth',
  created_at: '2026-09-11T00:00:00Z', updated_at: '2026-09-11T00:00:00Z',
}
const profile: ApiProfileInfo = {
  user: { id: 'alice-id', username: 'alice', name: 'Alice', nickname: null }, profiles: [], mcps: [],
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ connectMcp, confirmMcp } = await server.ssrLoadModule('/src/lib/api/client.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ ControlPage } = await server.ssrLoadModule('/src/features/control/ControlPage.tsx'))
  ;({ McpAuthorizationDialog } = await server.ssrLoadModule('/src/features/control/McpAuthorizationDialog.tsx'))
})
after(async () => { await server?.close() })
afterEach(() => { mock.restoreAll(); queryClient.clear(); useConsole.getInitialState().mgmtSec = '' })

test('authorization and confirmation keep the authenticated request contract and device delay', async () => {
  const responses = [
    Response.json({ operation_id: 'op', expires_at: '2026-09-11T01:00:00Z', authorization_uri: 'https://relay.example/oauth/start/op' }),
    Response.json({ status: 'pending_device', retry_after_seconds: 7 }),
  ]
  const fetch = mock.method(globalThis, 'fetch', async () => {
    const response = responses.shift()
    assert.ok(response, 'Unexpected extra request')
    return response
  })
  await connectMcp(mcp.id, 'authorization_code')
  assert.deepEqual(await confirmMcp(mcp.id), { status: 'pending_device', retry_after_seconds: 7 })
  assert.deepEqual(fetch.mock.calls.map((call) => call.arguments[0]), [
    '/api/mcp/work-id/connect?flow=authorization_code', '/api/mcp/work-id/confirm',
  ])
  for (const call of fetch.mock.calls) {
    const init = call.arguments[1]
    assert.equal(init?.method, 'POST')
    assert.equal(new Request('https://relay.example', init).credentials, 'same-origin')
    assert.equal(new Headers(init?.headers).get('X-Octomate-Request'), '1')
  }
})

test('a refused connection surfaces the server error without replaying the request', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'MCP instance unavailable' }, { status: 404 }))
  await assert.rejects(connectMcp('foreign-id', 'device'), /MCP instance unavailable/)
  assert.equal(fetch.mock.callCount(), 1)
})

const cases: { auth: ApiMcp['auth_kind']; status: ApiMcpAuthorizationResult['status'] | 'unavailable'; enabled: boolean; text: string; action: string | null }[] = [
  { auth: 'none', status: null, enabled: true, text: 'Ready', action: null },
  { auth: 'bearer', status: null, enabled: true, text: 'Ready', action: null },
  { auth: 'oauth', status: 'active', enabled: true, text: 'Ready', action: null },
  { auth: 'oauth', status: 'pending_browser', enabled: true, text: 'Pending', action: null },
  { auth: 'oauth', status: 'pending_device', enabled: true, text: 'Pending', action: null },
  { auth: 'oauth', status: 'invalid', enabled: true, text: 'Connect', action: 'Connect' },
  { auth: 'oauth', status: null, enabled: true, text: 'Connect', action: 'Connect' },
  { auth: 'oauth', status: 'active', enabled: false, text: 'Disabled', action: null },
  { auth: 'oauth', status: 'unavailable', enabled: true, text: 'Unavailable', action: null },
]

for (const item of cases) {
  test(`MCP row: ${item.auth}/${item.status}/${item.enabled} shows ${item.text}`, () => {
    useConsole.getInitialState().mgmtSec = 'mcp'
    queryClient.setQueryData(['mcp-servers'], [{ ...mcp, auth_kind: item.auth, enabled: item.enabled }])
    queryClient.setQueryData(['profile'], { ...profile, mcps: [{ ...mcp,
      oauth: item.auth !== 'oauth' || item.status === 'unavailable' ? null : { status: item.status, flows: ['device', 'authorization_code'] },
    }] })
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ControlPage)))
    assert.match(html, new RegExp(`>${item.text}<`))
    assert.doesNotMatch(html, />Not connected</)
    if (item.action) assert.match(html, new RegExp(`>${item.action}</button>`))
    else assert.doesNotMatch(html, />(Connect|Continue|Reconnect|Enable)<\/button>/)
  })
}

test('authorization offers both configured methods for this installation', () => {
  queryClient.setQueryData(['profile'], { ...profile, mcps: [{ ...mcp, oauth: { status: null, flows: ['device', 'authorization_code'] } }] })
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient },
    createElement(McpAuthorizationDialog, { mcp, onClose: () => {} })))
  assert.match(html, />Device code<\/option>/)
  assert.match(html, />Browser<\/option>/)
  assert.match(html, /personal\/work/)
})
