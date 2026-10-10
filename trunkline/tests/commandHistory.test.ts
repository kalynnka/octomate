import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiThread, ApiThreadMessage, CommandOutcome } from '../src/lib/api/events.ts'

let server: ViteDevServer
let liveThreadDetail: typeof import('../src/lib/api/live.ts').liveThreadDetail

const thread: ApiThread = {
  id: 'thread', kind: 'thread', chat_type: 'thread', chat_id: 'owner',
  channel_tentacle_id: 'trunkline', channel_thread_id: 'key', title: 'Work',
  project_id: null, status: 'active', created_at: '2026-10-09T00:00:00Z',
  updated_at: '2026-10-09T00:00:00Z', handoffs: [],
}
const receipt = {
  kind: 'command', id: 'receipt', thread_id: thread.id, platform_message_id: 'command',
  happened_at: thread.created_at, created_at: thread.created_at,
  direction: 'inbound', actor_kind: 'human', agent_tentacle_id: 'codex', sender: null,
  segments: [{ type: 'text', data: { text: '/status' } }], message_text: '/status',
} as const

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ liveThreadDetail } = await server.ssrLoadModule('/src/lib/api/live.ts'))
})
after(async () => { await server?.close() })

for (const outcome of [
  { status: 'completed', segments: [{ type: 'text', data: { text: 'Usage: 42 tokens' } }, { type: 'reply', data: { content: 'Quota: 80% left' } }] },
  { status: 'failed', message: 'The runtime is offline.' },
] satisfies CommandOutcome[]) {
  test(`reopening a thread restores a command's ${outcome.status} feedback after its invocation`, () => {
    const command = { ...receipt, segments: [...receipt.segments], outcome }
    const detail = liveThreadDetail({
      thread, conversations: [], modelMessages: [], unbound: [command], cursors: { model: null, unbound: null },
      totals: { model: 0, unbound: 0 }, project: null, batches: [], usage: {},
    })
    assert.deepEqual(detail.ledger.map((item) => item.kind), ['user', 'agent'])
    assert.equal(detail.ledger[0].kind === 'user' && detail.ledger[0].text, '/status')
    const reply = detail.ledger[1]
    assert.equal(reply.kind, 'agent')
    if (reply.kind !== 'agent') return
    assert.equal(reply.label, 'relay')
    assert.deepEqual(reply.blocks, outcome.status === 'completed'
      ? [{ type: 'p', text: 'Usage: 42 tokens' }, { type: 'p', text: 'Quota: 80% left' }]
      : [{ type: 'p', text: 'The runtime is offline.' }])
  })
}

for (const outcome of [null, { status: 'completed', segments: [] }] satisfies (CommandOutcome | null)[]) {
  test(`${outcome === null ? 'an unfinished' : 'a streamed'} command adds no empty or duplicate history reply`, () => {
    const command = { ...receipt, segments: [...receipt.segments], outcome }
    const reply = {
      ...receipt, kind: 'message', id: 'reply', direction: 'outbound', actor_kind: 'agent',
      segments: [], message_text: 'Review complete.',
    } satisfies ApiThreadMessage
    const detail = liveThreadDetail({
      thread, conversations: [], modelMessages: [], unbound: [command, reply], cursors: { model: null, unbound: null },
      totals: { model: 0, unbound: 0 }, project: null, batches: [], usage: {},
    })
    assert.deepEqual(detail.ledger.map((item) => item.kind), ['user', 'agent'])
    const answer = detail.ledger[1]
    assert.deepEqual(answer.kind === 'agent' && answer.blocks, [{ type: 'p', text: 'Review complete.' }])
  })
}
