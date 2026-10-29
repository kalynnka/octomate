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
  kind: 'message', id, thread_id: 'thread', platform_message_id: null, reply_id: '', happened_at: at, direction: 'inbound',
  actor_kind: actor, agent_tentacle_id: null, segments: [], message_text: text, created_at: at,
  sender: actor === 'human'
    ? { id: 'ada', channel_tentacle_id: 'trunkline', channel_user_id: 'ada', name: 'Ada', nickname: null, gender: null, age: null, title: null, user_id: 'account-ada' }
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
  parts: [{ part_kind: 'text', content: 'fixed' }], usage: { input_tokens: 6004, cache_read_tokens: 6000 },
}

const reads = (
  modelMessages: ApiModelMessage[],
  unbound: ApiThreadMessage[],
  cursors: ThreadReads['cursors'],
): ThreadReads => ({
  thread, modelMessages, unbound, cursors, totals: { model: 4, unbound: 1 }, conversations: [conversation],
  project: null, batches: [], usage: { input_tokens: 60040, output_tokens: 7, cache_read_tokens: 60000 },
})
const page = <T>(items: T[]) => ({ items: [...items].reverse(), total: 4, next_cursor: 'end', has_more: false })
const kinds = (ledger: LedgerItem[]) => ledger.map((item) => item.kind)

for (const [agent, bound] of [['codex-native', true], ['codex-native', false], ['deepseek-native', true], ['deepseek-native', false]] as const) {
  test(`${agent} question replies remain readable ${bound ? 'after binding' : 'before their run commits'}`, () => {
    const deepseek = agent === 'deepseek-native'
    const text = deepseek ? JSON.stringify({ kind: 'answer_to_pending_question', tool: 'ask_user_question', callId: 'native-q', answers: [{ id: 'inspect', selected: ['Yes'] }] }) : `<send_user_message_question_reply>\n${JSON.stringify([{
      questionItemId: JSON.stringify(['request_user_input_async', 'native-q', 0]), question: 'Inspect?', answer: 'Yes',
    }])}\n</send_user_message_question_reply>`
    const replied = said('060', text, 'human')
    const asked: ApiModelMessage = {
      id: '030', run_id: 'run', kind: 'response', timestamp: at, thread_messages: [],
      parts: [{ part_kind: 'builtin-tool-call', tool_name: deepseek ? 'ask_user_question' : 'request_user_input_async', tool_call_id: 'native-q', args: { questions: [deepseek ? { id: 'inspect', question: 'Inspect?' } : { title: 'Inspect?', options: ['Yes', 'No'] }] } }],
    }
    const reply: ApiModelMessage = {
      id: '070', run_id: 'later-run', kind: 'request', timestamp: at,
      parts: [{ part_kind: 'user-prompt', content: text }], thread_messages: [replied],
    }
    const native = reads(bound ? [asked, reply] : [asked], bound ? [] : [replied], { model: null, unbound: null })
    native.conversations = [{ ...conversation, agent_tentacle_id: agent }]
    const ledger = liveThreadDetail(native).ledger
    const card = ledger.find(item => item.kind === 'ask')
    assert.equal(card?.kind === 'ask' && card.state, 'answered')
    assert.deepEqual(card?.kind === 'ask' && card.questions[0].answer, deepseek ? ['Yes'] : 'Yes')
    const user = ledger.find(item => item.kind === 'user')
    assert.deepEqual(user?.kind === 'user' && [user.who, user.text], ['Ada', 'Yes'])
    assert.equal(user?.uid, '060')
  })
}

test('a thread is rebuilt from its model messages, in the words its rows were said in', () => {
  const whole = liveThreadDetail(reads([prompt, narration, built, answer], [notice], { model: null, unbound: null }))
  assert.deepEqual(kinds(whole.ledger), ['session-open', 'system', 'user', 'stream', 'tool', 'agent'])
  const user = whole.ledger.find((item) => item.kind === 'user')
  assert.deepEqual(user?.kind === 'user' && [user.who, user.text], ['Ada', 'fix the build'])
  assert.equal(user?.kind === 'user' && user.userId, 'account-ada')
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

test('mentions keep their position and use saved names, known profiles, or the platform id', () => {
  const message = said('060', 'before after', 'human')
  message.segments = [
    { type: 'text', data: { text: 'before ' } },
    { type: 'at', data: { user_id: 'ada' } },
    { type: 'text', data: { text: ' after\n' } },
    { type: 'at', data: { user_id: 'ada', name: 'Group nickname' } },
    { type: 'at', data: { user_id: 'unknown' } },
  ]
  const detail = liveThreadDetail(reads([], [message], { model: null, unbound: null }))
  const user = detail.ledger.find((item) => item.kind === 'user')
  assert.equal(user?.kind === 'user' && user.text, 'before @Ada after\n@Group nickname@unknown')
})

for (const direction of ['inbound', 'outbound'] as const) {
  test(`${direction} replies quote loaded messages, including mentions, without repeating the quote in the body`, () => {
    const original = { ...said('010', ' question', 'human'), platform_message_id: 'q', segments: [
      { type: 'at', data: { user_id: 'ada' } }, { type: 'text', data: { text: ' question' } },
    ] }
    const reply: ApiThreadMessage = { ...said('060', 'answer', 'human'), direction, reply_id: 'q', segments: [
      { type: 'reply', data: { id: 'q', content: 'Old quote' } }, { type: 'text', data: { text: 'answer' } },
    ] }
    const detail = liveThreadDetail(reads([], [original, reply], { model: null, unbound: null }))
    const item = detail.ledger.find((item) => item.uid === reply.id)
    assert.ok(item?.kind === 'user' || item?.kind === 'agent')
    assert.deepEqual(item.reply, { who: 'Ada', text: '@Ada question' })
    assert.equal(item.kind === 'user' ? item.text : item.blocks[0].type === 'p' && item.blocks[0].text, 'answer')
  })
}

test('replayed model answers carry the reply reference from their bound channel message', () => {
  const outbound: ApiThreadMessage = { ...said('reply', 'fixed', 'human'), direction: 'outbound', reply_id: 'q' }
  const request = { ...prompt, thread_messages: [{ ...prompt.thread_messages[0], platform_message_id: 'q' }] }
  const response = { ...answer, thread_messages: [outbound] }
  const detail = liveThreadDetail(reads([request, response], [], { model: null, unbound: null }))
  const item = detail.ledger.find((item) => item.kind === 'agent')
  assert.deepEqual(item?.kind === 'agent' && item.reply, { who: 'Ada', text: 'fix the build' })
})

for (const [nativeId, label] of [['codex-native', 'Codex'], ['claude-native', 'Claude'], ['deepseek-native', 'DeepSeek']]) {
  test(`${nativeId} labels native and imported replies by runtime, while keeping the uploading user's identity on prompts`, () => {
    const owner = { ...said('owner', '', 'human').sender!, name: 'test', channel_tentacle_id: nativeId }
    const outbound: ApiThreadMessage = {
      ...said('045', 'Native answer', 'human'), direction: 'outbound', actor_kind: 'agent',
      agent_tentacle_id: nativeId, sender: owner, platform_message_id: 'native-answer',
    }
    const quoted = { ...said('060', 'Follow-up', 'human'), sender: owner, reply_id: 'native-answer' }
    const bound = { ...answer, thread_messages: [{ ...outbound, id: '046', platform_message_id: 'bound-answer' }] }
    const bare = { ...answer, id: '055', thread_messages: [] }
    const native: ThreadReads = {
      ...reads([bound, bare], [outbound, quoted], { model: null, unbound: null }),
      thread: { ...thread, kind: 'native_thread', channel_tentacle_id: nativeId },
      conversations: [{ ...conversation, agent_tentacle_id: nativeId }],
    }
    const imported = { ...native, thread }
    for (const source of [native, imported]) {
      const detail = liveThreadDetail(source)
      assert.deepEqual(detail.ledger.flatMap((item) => item.kind === 'agent' ? [item.label] : []), [label, label, label])
      const human = detail.ledger.find((item) => item.kind === 'user')
      assert.ok(human?.kind === 'user')
      assert.equal(human.who, 'test')
      assert.equal(human.reply?.who, label)
    }
  })
}

test('channel bot replies retain the bot name and agent name together', () => {
  const outbound: ApiThreadMessage = {
    ...said('045', 'Channel answer', 'human'), direction: 'outbound', actor_kind: 'agent', agent_tentacle_id: 'inkling',
    sender: { ...said('bot', '', 'human').sender!, name: '玥葉', channel_tentacle_id: 'napcat' },
  }
  const detail = liveThreadDetail(reads([], [outbound], { model: null, unbound: null }))
  const item = detail.ledger.find((item) => item.kind === 'agent')
  assert.equal(item?.kind === 'agent' && item.label, '玥葉 (Inkling)')
})

test('a reply segment retains its quote even without a reply_id or a loaded original', () => {
  const message = { ...said('060', 'answer', 'human'), segments: [
    { type: 'reply', data: { id: 'q', user_id: 'ada', content: '<b>Earlier question</b>' } },
  ] }
  const detail = liveThreadDetail(reads([], [message], { model: null, unbound: null }))
  const item = detail.ledger.find((item) => item.kind === 'user')
  assert.deepEqual(item?.kind === 'user' && item.reply, { who: 'Ada', text: '<b>Earlier question</b>' })
})

test('paging in a reply target refreshes the preview and never borrows a message from another thread', () => {
  const reply = { ...said('060', 'answer', 'human'), reply_id: 'q' }
  const other = { ...said('055', 'Wrong quote', 'human'), thread_id: 'other', platform_message_id: 'q' }
  const latest = liveThreadDetail(reads([], [other, reply], { model: null, unbound: 'older' }))
  const item = latest.ledger.find((item) => item.uid === reply.id)
  assert.ok(item?.kind === 'user')
  assert.equal(item.reply?.text, 'Earlier message')
  const original = { ...said('005', 'Original question', 'human'), platform_message_id: 'q' }
  const whole = withOlderPage(latest, latest.reads!, { read: 'unbound', page: page([original]) })
  const loaded = whole.ledger.find((item) => item.uid === reply.id)
  assert.deepEqual(loaded?.kind === 'user' && loaded.reply, { who: 'Ada', text: 'Original question' })
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
    input: 60040, output: 7, cacheRead: 60000, cacheWrite: 0, cacheRate: 60000 / 60040,
  })
  assert.equal(latest.ctxK, 6)
})

test('artifact references survive pagination with their row identity and no text card', () => {
  const artifact: ApiThreadMessage = { ...said('060', '', 'system'), kind: 'artifact', artifact_id: 'saved-app' }
  const detail = liveThreadDetail(reads([], [artifact], { model: null, unbound: null }))
  assert.deepEqual(detail.ledger.filter((item) => item.kind === 'artifact'), [{ kind: 'artifact', uid: '060', artifactId: 'saved-app' }])
  assert.equal(detail.ledger.some((item) => item.kind === 'system'), false)
  const paged = withOlderPage(detail, detail.reads!, { read: 'unbound', page: page([artifact]) })
  assert.equal(paged.ledger.filter((item) => item.kind === 'artifact').length, 1)
})

for (const text of ['Here is the chart.', '']) {
  test(`captured HTML stays after its originating response on reload (${text ? 'caption' : 'reference only'})`, () => {
    const artifact: ApiThreadMessage = { ...said('015', '', 'system'), direction: 'outbound', kind: 'artifact', artifact_id: 'saved-html' }
    const response: ApiModelMessage = { ...answer, parts: [{ part_kind: 'text', content: text }], thread_messages: [artifact] }
    const next: ApiModelMessage = { ...prompt, id: '060', thread_messages: [said('055', 'Next question', 'human')] }
    const detail = liveThreadDetail(reads([prompt, response, next], [], { model: null, unbound: null }))
    assert.deepEqual(detail.ledger.map(item => item.kind), text
      ? ['session-open', 'user', 'agent', 'artifact', 'user']
      : ['session-open', 'user', 'artifact', 'user'])
    assert.equal(detail.ledger.find(item => item.kind === 'artifact')?.uid, artifact.id)
    const paged = withOlderPage(detail, detail.reads!, { read: 'model', page: page([prompt, response]) })
    assert.deepEqual(paged.ledger, detail.ledger)
  })
}

for (const runtime of ['claude-native', 'codex-native', 'deepseek-native']) {
  test(`${runtime} preserves append order despite source clocks, including the pagination horizon`, () => {
    const native = { ...thread, kind: 'native_thread' as const, channel_tentacle_id: runtime, handoffs: [] }
    const earlier = { ...answer, id: '010', timestamp: '2026-10-09T11:00:00Z', parts: [{ part_kind: 'text' as const, content: 'Earlier' }] }
    const later = { ...answer, id: '090', timestamp: '2026-10-09T09:00:00Z' }
    const middle = { ...notice, id: '020', happened_at: at }
    const source = { ...reads([earlier, later], [middle], { model: 'more', unbound: 'more' }), thread: native }
    assert.equal(holdingBack(source), 'unbound')
    const latest = liveThreadDetail(source)
    assert.deepEqual(latest.ledger.map((item) => item.uid), ['020.0', '090.0'])
    const whole = withOlderPage(latest, source, { read: 'unbound', page: page([]) })
    assert.deepEqual(whole.ledger.map((item) => item.uid), ['010.0', '020.0', '090.0'])
  })
}
