import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type {
  ApiConversation,
  ApiHandoff,
  ApiModelMessage,
  ApiThread,
  ApiThreadMessage,
} from '../src/lib/api/events.ts'
import type { ThreadReads } from '../src/lib/api/live.ts'
import type { LedgerItem } from '../src/lib/api/types.ts'

let server: ViteDevServer
let liveThreadDetail: typeof import('../src/lib/api/live.ts').liveThreadDetail
let withOlderPage: typeof import('../src/lib/api/live.ts').withOlderPage
let holdingBack: typeof import('../src/lib/api/live.ts').holdingBack

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ liveThreadDetail, withOlderPage, holdingBack } = await server.ssrLoadModule('/src/lib/api/live.ts'))
})
after(async () => { await server?.close() })

// Ids sort the way uuid7s do: by when the row was written.
const at = '2026-10-09T10:00:00Z'
const handoff: ApiHandoff = {
  id: '010', thread_id: 'thread', source_agent_tentacle_id: null, to_agent_tentacle_id: 'claude',
  to_model: null, reason: 'route claimed', hint: '', brief: '', target_conversation_id: 'conversation',
  source_conversation_id: null, created_at: at,
}
const thread: ApiThread = {
  id: 'thread', kind: 'thread', chat_type: 'thread', chat_id: 'owner', channel_tentacle_id: 'trunkline',
  channel_thread_id: 'trunkline-a', title: 'Fix the build', project_id: null, status: 'active',
  created_at: at, updated_at: at, handoffs: [handoff], active_agent_tentacle_id: 'claude',
}
const conversation: ApiConversation = {
  id: 'conversation', external_id: null, thread_id: 'thread', agent_tentacle_id: 'claude', subagent_id: '',
  parent_conversation_id: null, name: null, status: 'active', permission_mode: null, effort: null, allowed_tools: [],
  runs: [{
    id: 'run', kind: 'octomate', conversation_id: 'conversation', name: null, model_name: null,
    permission_mode: null, cwd: null, parent_run_id: null, parent_tool_call_id: null, started_at: null,
  }],
}

const said = (id: string, text: string, actor: 'human' | 'system'): ApiThreadMessage => ({
  kind: 'message', id, thread_id: 'thread', platform_message_id: null, happened_at: at, direction: 'inbound',
  actor_kind: actor, agent_tentacle_id: null, segments: [], message_text: text, created_at: at,
  sender: actor === 'human'
    ? { id: 'ada', channel_tentacle_id: 'trunkline', channel_user_id: 'ada', name: 'Ada', nickname: null, gender: null, age: null, title: null, user_id: null }
    : null,
})
const notice = said('015', 'Forked from conversation elsewhere.', 'system')
// The prompt as the model got it is dressed; the row it was built from is not.
const prompt: ApiModelMessage = {
  id: '020', run_id: 'run', kind: 'request', timestamp: null,
  parts: [{ part_kind: 'user-prompt', content: 'Ada (ada, user:ada) #msg:1: fix the build' }],
  thread_messages: [said('m-1', 'fix the build', 'human')],
}
const narration: ApiModelMessage = {
  id: '030', run_id: 'run', kind: 'response', timestamp: at, thread_messages: [],
  parts: [
    { part_kind: 'text', content: 'running make' },
    { part_kind: 'tool-call', tool_name: 'Bash', args: { command: 'make' }, tool_call_id: 'make' },
  ],
}
const built: ApiModelMessage = {
  id: '040', run_id: 'run', kind: 'request', timestamp: null, thread_messages: [],
  parts: [{ part_kind: 'tool-return', tool_name: 'Bash', content: 'built', tool_call_id: 'make' }],
}
const answer: ApiModelMessage = {
  id: '050', run_id: 'run', kind: 'response', timestamp: at, thread_messages: [],
  parts: [{ part_kind: 'text', content: 'fixed' }], usage: { input_tokens: 4, cache_read_tokens: 6000 },
}

const reads = (
  modelMessages: ApiModelMessage[],
  unbound: ApiThreadMessage[],
  cursors: ThreadReads['cursors'],
): ThreadReads => ({
  thread, modelMessages, unbound, cursors, totals: { model: 4, unbound: 1 }, conversations: [conversation],
  project: null, batches: [], usage: { input_tokens: 40, output_tokens: 7, cache_read_tokens: 60000 },
})
const page = <T>(items: T[]) => ({ items: [...items].reverse(), total: 4, next_cursor: 'end', has_more: false })
const kinds = (ledger: LedgerItem[]) => ledger.map((item) => item.kind)

test('a thread is rebuilt from its model messages, in the words its rows were said in', () => {
  const whole = liveThreadDetail(reads([prompt, narration, built, answer], [notice], { model: null, unbound: null }))
  assert.deepEqual(kinds(whole.ledger), ['session-open', 'system', 'user', 'stream', 'tool', 'agent'])
  const user = whole.ledger.find((item) => item.kind === 'user')
  assert.deepEqual(user?.kind === 'user' && [user.who, user.text], ['Ada', 'fix the build'])
  const tool = whole.ledger.find((item) => item.kind === 'tool')
  assert.equal(tool?.kind === 'tool' && tool.detail.type === 'plain' && tool.detail.res, 'built')
  const reply = whole.ledger.at(-1)
  assert.deepEqual(reply?.kind === 'agent' && [reply.label, reply.blocks], ['claude', [{ type: 'p', text: 'fixed' }]])
  assert.equal(whole.sessions[0].anchor, '010')
  assert.equal(whole.msgCount, 5)
})

test('older pages fold on above, and the cards already shown keep what was done to them', () => {
  const latest = liveThreadDetail(reads([built, answer], [notice], { model: 'older', unbound: null }))
  // The notice and the claim are older than anything the model read has reached,
  // and wait for it rather than show above a gap.
  assert.deepEqual(kinds(latest.ledger), ['agent'])
  assert.equal(latest.earlier, true)

  const marked = { ...latest, ledger: latest.ledger.map((item) => ({ ...item, marked: true })) }
  const whole = withOlderPage(marked, latest.reads!, { read: 'model', page: page([prompt, narration]) })
  assert.deepEqual(kinds(whole.ledger), ['session-open', 'system', 'user', 'stream', 'tool', 'agent'])
  assert.deepEqual(whole.ledger.at(-1), marked.ledger[0])
  assert.equal(whole.earlier, false)
  assert.equal(whole.sessions[0].anchor, '010')

  const once = liveThreadDetail(reads([prompt, narration, built, answer], [notice], { model: null, unbound: null }))
  const twice = withOlderPage(latest, latest.reads!, { read: 'model', page: page([prompt, narration]) })
  assert.deepEqual(twice.ledger, once.ledger)
})

test('the read to page next is the one whose oldest row is newest', () => {
  assert.equal(holdingBack(reads([built, answer], [notice], { model: 'older', unbound: 'older' })), 'model')
  assert.equal(holdingBack(reads([answer], [said('060', 'later', 'system')], { model: 'older', unbound: 'older' })), 'unbound')
  assert.equal(holdingBack(reads([built, answer], [notice], { model: null, unbound: 'older' })), 'unbound')
  assert.equal(holdingBack(reads([built, answer], [notice], { model: null, unbound: null })), null)
})

test('a request nothing was bound to speaks for itself', () => {
  const bare = { ...prompt, parts: [{ part_kind: 'user-prompt' as const, content: 'fix the build' }], thread_messages: [] }
  const detail = liveThreadDetail(reads([bare], [], { model: null, unbound: null }))
  const user = detail.ledger.find((item) => item.kind === 'user')
  assert.deepEqual(user?.kind === 'user' && [user.who, user.text], ['operator', 'fix the build'])
})

test('usage is the relay total, and the context is the newest response in hand', () => {
  const latest = liveThreadDetail(reads([built, answer], [], { model: 'older', unbound: null }))
  assert.deepEqual(latest.usage, {
    input: 40, output: 7, cacheRead: 60000, cacheWrite: 0, cacheRate: 60000 / 60040,
  })
  assert.equal(latest.ctxK, 6)
})
