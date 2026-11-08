import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiConversation, ApiThread, ApiThreadMessage } from '../src/lib/api/events.ts'

let server: ViteDevServer
let liveThreadSummary: typeof import('../src/lib/api/live.ts').liveThreadSummary
let liveThreadDetail: typeof import('../src/lib/api/live.ts').liveThreadDetail

const thread: ApiThread = {
  id: 'native-thread', kind: 'native_thread', chat_type: 'thread', chat_id: 'native-session',
  channel_tentacle_id: 'codex', channel_thread_id: null, title: 'Native work',
  project_id: null, status: 'active', created_at: '2026-09-29T00:00:00Z',
  updated_at: '2026-09-29T00:00:00Z', handoffs: [], active_agent_tentacle_id: 'codex',
}
const conversation: ApiConversation = {
  id: 'native-conversation', external_id: 'native-session', thread_id: thread.id,
  agent_tentacle_id: 'codex', subagent_id: '', parent_conversation_id: null,
  name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [], runs: [],
}

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ liveThreadDetail, liveThreadSummary } = await server.ssrLoadModule('/src/lib/api/live.ts'))
})
after(async () => { await server?.close() })

test('native thread kind keeps a synced session read-only regardless of its channel name', () => {
  const native: ApiThread = { ...thread, kind: 'native_thread', channel_tentacle_id: 'trunkline', channel_thread_id: 'key' }
  const detail = liveThreadDetail({
      thread: native, conversations: [], modelMessages: [], unbound: [], cursors: { model: null, unbound: null },
      totals: { model: 0, unbound: 0 }, project: null, batches: [], usage: {},
    })
  assert.equal(detail.kind, 'native_thread')
  assert.equal(detail.sendKey, undefined)
})

test('an imported session displays its conversation agent without a handoff', () => {
  const imported: ApiThread = {
    ...thread, channel_tentacle_id: 'trunkline', active_agent_tentacle_id: 'codex',
  }
  assert.equal(liveThreadSummary(imported).agentLabel, 'codex')
})

for (const owner of ['codex', 'coding']) {
  test(`a native address owned by ${owner} preserves its marker and runtime session link`, () => {
    const native: ApiThread = { ...thread, channel_tentacle_id: owner, active_agent_tentacle_id: owner }
    const source: ApiConversation = {
      ...conversation, agent_tentacle_id: owner,
      runs: [{
        id: 'turn', kind: 'external', conversation_id: conversation.id, name: 'codex',
        cwd: '/workspace', model_name: null, permission_mode: null,
        parent_run_id: null, parent_tool_call_id: null, started_at: null,
      }],
    }
    const detail = liveThreadDetail({
      thread: native, conversations: [source], modelMessages: [], unbound: [], cursors: { model: null, unbound: null },
      totals: { model: 0, unbound: 0 }, project: null, batches: [], usage: {},
    })
    assert.equal(liveThreadSummary(native).tone, 'native')
    assert.equal(liveThreadSummary(native).agentLabel, owner)
    assert.equal(detail.channel, owner)
    assert.equal(detail.sendKey, undefined)
    assert.equal(detail.vscode?.session, 'vscode://openai.chatgpt/local/native-session')
    assert.equal(liveThreadSummary({ ...native, kind: 'thread' }).tone, 'active')
  })
}

test('an import notice keeps the address label and value in one system paragraph', () => {
  const notice: ApiThreadMessage = {
    kind: 'message', id: 'notice', thread_id: thread.id, platform_message_id: null, reply_id: '',
    happened_at: '2026-09-29T00:00:00Z', direction: 'inbound', actor_kind: 'system',
    agent_tentacle_id: null, sender: null, segments: [], created_at: '2026-09-29T00:00:00Z',
    message_text: 'Forked from conversation native-session.\n\nCurrent channel address:\ntrunkline/thread/owner/landed/owner.',
  }
  const detail = liveThreadDetail({
    thread, conversations: [], modelMessages: [], unbound: [notice], cursors: { model: null, unbound: null },
    totals: { model: 0, unbound: 1 }, project: null, batches: [], usage: {},
  })
  assert.deepEqual(detail.ledger.flatMap((item) => (item.kind === 'system' ? [item.text] : [])), [
    'Forked from conversation native-session.',
    'Current channel address:\ntrunkline/thread/owner/landed/owner.',
  ])
})

for (const kind of ['external', 'octomate'] as const) {
  test(`${kind} shows the recorded model and permission preset in the session and composer`, () => {
    const source: ApiConversation = {
      ...conversation, agent_tentacle_id: 'codex',
      runs: [{
        id: 'completed-turn', kind,
        conversation_id: conversation.id, name: 'fork', cwd: null,
        model_name: 'gpt-6-luna', permission_mode: 'auto_review',
        parent_run_id: null, parent_tool_call_id: null,
        started_at: '2026-09-29T00:00:00Z',
      }],
    }
    const detail = liveThreadDetail({
      thread, conversations: [source], modelMessages: [], unbound: [], cursors: { model: null, unbound: null },
      totals: { model: 0, unbound: 0 }, project: null, batches: [], usage: {},
    })
    assert.equal(detail.sessions.at(-1)?.route, 'codex · gpt-6-luna')
    assert.equal(detail.sessions.at(-1)?.mode, 'auto_review')
  })
}
