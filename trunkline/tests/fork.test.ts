import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClientProvider } from '@tanstack/react-query'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiConversation, ApiThread } from '../src/lib/api/events.ts'

let server: ViteDevServer
let forkThread: typeof import('../src/lib/api/client.ts').forkThread
let liveThreadSummary: typeof import('../src/lib/api/live.ts').liveThreadSummary
let liveThreadDetail: typeof import('../src/lib/api/live.ts').liveThreadDetail
let queryClient: typeof import('../src/lib/queryClient.ts').queryClient
let useConsole: typeof import('../src/state/console.ts').useConsole
let ChatHeader: typeof import('../src/features/chat/ChatHeader.tsx').ChatHeader

const thread: ApiThread = {
  id: 'native-thread', kind: 'native_thread', chat_type: 'thread', chat_id: 'native-session',
  channel_tentacle_id: 'codex-native', channel_thread_id: null, title: 'Native work',
  project_id: null, status: 'active', created_at: '2026-09-29T00:00:00Z',
  updated_at: '2026-09-29T00:00:00Z', handoffs: [], active_agent_tentacle_id: 'codex-native',
}
const conversation: ApiConversation = {
  id: 'native-conversation', external_id: 'native-session', thread_id: thread.id,
  agent_tentacle_id: 'codex-native', subagent_id: '', parent_conversation_id: null,
  name: null, status: 'active', permission_mode: null, allowed_tools: [], runs: [],
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ forkThread } = await server.ssrLoadModule('/src/lib/api/client.ts'))
  ;({ liveThreadDetail, liveThreadSummary } = await server.ssrLoadModule('/src/lib/api/live.ts'))
  ;({ queryClient } = await server.ssrLoadModule('/src/lib/queryClient.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ ChatHeader } = await server.ssrLoadModule('/src/features/chat/ChatHeader.tsx'))
})
after(async () => { await server?.close() })
afterEach(() => {
  mock.restoreAll()
  queryClient.clear()
  useConsole.getInitialState().detail = null
})

test('fork sends a CSRF-protected POST without accepting an owner identity', async () => {
  const destination = { ...thread, id: 'destination', channel_tentacle_id: 'trunkline', channel_thread_id: 'new-key' }
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json(destination, { status: 201 }))
  assert.deepEqual(await forkThread('source/id'), destination)
  const [path, init] = fetch.mock.calls[0].arguments
  assert.equal(path, '/api/trunkline/threads/source%2Fid/fork')
  assert.equal(init?.method, 'POST')
  assert.equal(new Headers(init?.headers).get('X-Octomate-Request'), '1')
  assert.equal(init?.body, undefined)
})

test('a refused fork surfaces the server reason without retrying', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'No completed Codex turn has been fully uploaded' }, { status: 409 }))
  await assert.rejects(forkThread(thread.id), /No completed Codex turn/)
  assert.equal(fetch.mock.callCount(), 1)
})

for (const kind of ['native', 'child', 'driven', 'claude'] as const) {
  test(`the ${kind} conversation exposes the appropriate fork action`, () => {
    const source = { ...conversation }
    if (kind === 'child') source.subagent_id = 'child'
    if (kind === 'driven') source.agent_tentacle_id = 'codex'
    if (kind === 'claude') source.agent_tentacle_id = 'claude-native'
    const detail = liveThreadDetail({ thread, conversations: [source], messages: [], project: null, batches: [] })
    assert.equal(detail.canFork, kind === 'native')
    useConsole.getInitialState().detail = detail
    queryClient.setQueryData(['threads'], {})
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(ChatHeader)))
    assert.equal(html.includes('>Fork</button>'), kind === 'native')
    assert.ok(html.includes('aria-label="Choose thread operation"'))
    assert.equal(html.includes('aria-disabled="true"'), kind !== 'native')
    assert.equal(html.includes('title="No other destinations are available."'), kind !== 'native')
    assert.ok(!html.includes('title="Start an independent thread'))
  })
}


test('a fork displays its conversation agent without a handoff', () => {
  const forked: ApiThread = {
    ...thread, channel_tentacle_id: 'trunkline', active_agent_tentacle_id: 'codex',
  }
  assert.equal(liveThreadSummary(forked).agentLabel, 'codex')
})

for (const agent of ['codex-native', 'codex']) {
  test(`${agent} shows the recorded model and permission preset in the session and composer`, () => {
    const source: ApiConversation = {
      ...conversation, agent_tentacle_id: agent,
      runs: [{
        id: 'completed-turn', kind: agent === 'codex' ? 'octomate' : 'external',
        conversation_id: conversation.id, name: 'fork', cwd: null,
        model_name: 'gpt-6-luna', permission_mode: 'auto_review',
        parent_run_id: null, parent_tool_call_id: null,
        started_at: '2026-09-29T00:00:00Z', messages: [],
      }],
    }
    const detail = liveThreadDetail({ thread, conversations: [source], messages: [], project: null, batches: [] })
    assert.equal(detail.sessions.at(-1)?.route, `${agent} · gpt-6-luna`)
    assert.equal(detail.sessions.at(-1)?.mode, 'auto_review')
  })
}
