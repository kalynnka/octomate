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
  channel_tentacle_id: 'codex-native', channel_thread_id: null, title: 'Native work',
  project_id: null, status: 'active', created_at: '2026-09-29T00:00:00Z',
  updated_at: '2026-09-29T00:00:00Z', handoffs: [], active_agent_tentacle_id: 'codex-native',
}
const conversation: ApiConversation = {
  id: 'native-conversation', external_id: 'native-session', thread_id: thread.id,
  agent_tentacle_id: 'codex-native', subagent_id: '', parent_conversation_id: null,
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
  const detail = liveThreadDetail({ thread: native, conversations: [], messages: [], project: null, batches: [] })
  assert.equal(detail.kind, 'native_thread')
  assert.equal(detail.sendKey, undefined)
})

test('an imported session displays its conversation agent without a handoff', () => {
  const imported: ApiThread = {
    ...thread, channel_tentacle_id: 'trunkline', active_agent_tentacle_id: 'codex',
  }
  assert.equal(liveThreadSummary(imported).agentLabel, 'codex')
})

test('an import notice of several lines reads as a system row per line', () => {
  const notice: ApiThreadMessage = {
    id: 'notice', thread_id: thread.id, platform_message_id: null,
    happened_at: '2026-09-29T00:00:00Z', direction: 'inbound', actor_kind: 'system',
    agent_tentacle_id: null, sender: null, segments: [], created_at: '2026-09-29T00:00:00Z',
    message_text: 'Forked from conversation native-session.\n\nCurrent channel address:\ntrunkline/thread/owner/landed/owner.',
  }
  const detail = liveThreadDetail({ thread, conversations: [], messages: [notice], project: null, batches: [] })
  assert.deepEqual(detail.ledger.flatMap((item) => (item.kind === 'system' ? [item.text] : [])), [
    'Forked from conversation native-session.',
    'Current channel address:',
    'trunkline/thread/owner/landed/owner.',
  ])
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
