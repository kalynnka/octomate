import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer, type ViteDevServer } from 'vite'
import type { ApiModelMessage, WireDeferredQuestion, WireEvent } from '../src/lib/api/events.ts'
import type { LedgerItem } from '../src/lib/api/types.ts'
import { LiveSocket, installLiveSocket } from './liveSocket.ts'

let server: ViteDevServer
let batchFeelers: typeof import('../src/lib/api/fold.ts').batchFeelers
let TurnFold: typeof import('../src/lib/api/fold.ts').TurnFold
let replayRun: typeof import('../src/lib/api/fold.ts').replayRun
let promptText: typeof import('../src/lib/api/fold.ts').promptText
let useConsole: typeof import('../src/state/console.ts').useConsole
let LedgerRow: typeof import('../src/features/chat/cards.tsx').LedgerRow

const question = (id: string, position: number, text: string): WireDeferredQuestion => ({
  id, kind: 'question', status: 'pending', tool_name: 'AskUserQuestion', tool_call_id: 'q', position,
  args: { question: text, choices: ['Yes', 'No'] },
})
const result: WireEvent = { event_kind: 'run_result', output: 'Done', usage: { requests: 1, tool_calls: 0, input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 } }

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ batchFeelers, TurnFold, replayRun, promptText } = await server.ssrLoadModule('/src/lib/api/fold.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ LedgerRow } = await server.ssrLoadModule('/src/features/chat/cards.tsx'))
})
afterEach(() => mock.restoreAll())
after(async () => { await server?.close() })

test('closed batches have no controls and invalid forms retain their draft and feedback', () => {
  const field = { ...question('field', 0, 'Count?'), result: '0', args: { question: 'Count?', error: 'Must be positive' } }
  const [draft] = batchFeelers('form', [field], [])
  const draftHtml = renderToStaticMarkup(createElement(LedgerRow, { item: { ...draft, uid: 'form' } as LedgerItem, cardMax: '100%' }))
  assert.ok(draftHtml.includes('Must be positive'))
  assert.ok(draftHtml.includes('0'))
  const cards = batchFeelers('form', [{ ...field, status: 'expired' }], [{ id: 'approval', kind: 'approval', status: 'expired', tool_name: 'shell', tool_call_id: 'call', args: { tool_name: 'shell' } }], true)
  for (const card of cards) {
    const html = renderToStaticMarkup(createElement(LedgerRow, { item: { ...card, uid: 'closed' } as LedgerItem, cardMax: '100%' }))
    assert.match(html, /request closed/i)
    assert.ok(!html.includes('aria-pressed') && !html.includes('Submit') && !html.includes('Approve') && !html.includes('Deny'))
  }
})

test('answered questions remain editable while their batch is open', () => {
  const [card] = batchFeelers('batch', [{ ...question('first', 0, 'Region?'), status: 'answered', result: 'Yes' }], [{ id: 'approval', kind: 'approval', status: 'pending', tool_name: 'shell', tool_call_id: 'a', args: { tool_name: 'shell' } }])
  assert.equal(card.kind, 'ask')
  if (card.kind !== 'ask') return
  assert.equal(card.state, 'waiting')
  assert.equal(card.questions[0].answer, 'Yes')
  const html = renderToStaticMarkup(createElement(LedgerRow, { item: { ...card, uid: 'card' } as LedgerItem, cardMax: '100%' }))
  assert.ok(html.includes('aria-pressed="true"'))
  assert.ok(html.includes('aria-pressed="false"'))
})

test('native async questions wait for the human reply, not the tool acknowledgement', () => {
  let items: LedgerItem[] = []
  const fold = new TurnFold({
    push(draft) {
      const item = { ...draft, uid: draft.uid ?? String(items.length) } as LedgerItem
      items.push(item)
      return item.uid
    },
    patch(uid, patch) { items = items.map(item => item.uid === uid ? { ...item, ...patch } as LedgerItem : item) },
    done() {},
  }, 'Codex')
  const args = JSON.stringify({ questions: [{ title: 'Inspect the tab?', options: ['Inspect', 'No'] }, { title: 'Refresh?' }] })
  fold.feed({ event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_name: 'request_user_input_async', tool_call_id: 'native-q', args } })
  fold.feed({ event_kind: 'function_tool_result', part: { part_kind: 'tool-return', tool_name: 'request_user_input_async', tool_call_id: 'native-q', content: '{"accepted":true}' } })
  const [waiting] = items
  assert.equal(waiting.kind, 'ask')
  if (waiting.kind !== 'ask') return
  assert.equal(waiting.state, 'waiting')
  assert.equal(waiting.batchId, undefined)
  assert.deepEqual(waiting.questions.map(q => q.body), ['Inspect the tab?', 'Refresh?'])
  const [replayed] = replayRun([{
    id: 'message', run_id: 'run', kind: 'response', timestamp: '2026-10-14T10:00:00Z', thread_messages: [],
    parts: [
      { part_kind: 'builtin-tool-call', tool_name: 'request_user_input_async', tool_call_id: 'native-q', args },
      { part_kind: 'builtin-tool-return', tool_name: 'request_user_input_async', tool_call_id: 'native-q', content: '{"accepted":true}' },
    ],
  }], 'Codex', undefined, true)
  assert.deepEqual({ ...replayed.item, uid: waiting.uid }, waiting)
  const html = renderToStaticMarkup(createElement(LedgerRow, { item: waiting, cardMax: '100%' }))
  assert.ok(html.includes('Inspect the tab?') && html.includes('Inspect') && html.includes('Refresh?'))
  assert.ok(html.includes('Answer in Codex'))
  assert.ok(!html.includes('<textarea') && !html.includes('aria-pressed') && !html.includes('Submit'))

  for (const [index, question, answer] of [[0, 'Inspect the tab?', 'Inspect'], [1, 'Refresh?', 'No']] as const) {
    const reply = `<send_user_message_question_reply>\n${JSON.stringify([{ questionItemId: JSON.stringify(['request_user_input_async', 'native-q', index]), question, answer }])}\n</send_user_message_question_reply>`
    fold.feed({ event_kind: 'enqueued_messages', enqueue_id: `reply-${index}`, messages: [{ kind: 'request', parts: [{ part_kind: 'user-prompt', content: reply }], timestamp: null }] })
    const card = items[0]
    assert.equal(card.kind === 'ask' && card.state, index === 0 ? 'waiting' : 'answered')
    const user = items.at(-1)
    assert.equal(user?.kind === 'user' && user.text, answer)
  }
  const answered = items[0]
  assert.deepEqual(answered.kind === 'ask' && answered.questions.map(q => q.answer), ['Inspect', 'No'])
  const settledHtml = renderToStaticMarkup(createElement(LedgerRow, { item: answered, cardMax: '100%' }))
  assert.ok(settledHtml.includes('Answered in Codex') && settledHtml.includes('Answer: Inspect'))
})

for (const { agent, tool, args, result, answers } of [
  {
    agent: 'Claude', tool: 'AskUserQuestion',
    args: { questions: [
      { question: 'Which packages?', multiSelect: true, options: [{ label: 'core', description: 'Shared code' }, { label: 'cli' }] },
      { question: 'Any notes?', options: [] },
    ] },
    result: 'Your questions have been answered: "Which packages?"="core, cli", "Any notes?"="Keep the "old" API\nfor now". You can now continue with these answers in mind.',
    answers: ['core, cli', 'Keep the "old" API\nfor now'],
  },
  {
    agent: 'DeepSeek', tool: 'ask_user_question',
    args: { questions: [
      { id: 'packages', question: 'Which packages?', multi_select: true, options: [{ label: 'core', description: 'Shared code' }, { label: 'cli' }] },
      { id: 'notes', question: 'Any notes?' },
    ] },
    result: JSON.stringify({ answers: [{ id: 'notes', selected: [] }, { id: 'packages', selected: ['core', 'cli'], custom: 'Keep the API' }] }),
    answers: [['core', 'cli', 'Keep the API'], []],
  },
  {
    agent: 'Codex', tool: 'request_user_input',
    args: { questions: [
      { id: 'packages', question: 'Which packages?', options: [{ label: 'core', description: 'Shared code' }, { label: 'cli' }] },
      { id: 'notes', question: 'Any notes?', options: [] },
    ] },
    result: JSON.stringify({ answers: { notes: { answers: ['Keep the API'] }, packages: { answers: ['core'] } } }),
    answers: [['core'], ['Keep the API']],
  },
]) {
  test(`${agent} native questions and answers agree live and in history, without replacing driven batches`, () => {
    const label = `${agent} session`
    let items: LedgerItem[] = []
    const fold = new TurnFold({
      push(draft) { const item = { ...draft, uid: String(items.length) } as LedgerItem; items.push(item); return item.uid },
      patch(uid, patch) { items = items.map(item => item.uid === uid ? { ...item, ...patch } as LedgerItem : item) },
      done() {},
    }, label)
    const call = { part_kind: 'tool-call' as const, tool_name: tool, tool_call_id: 'native-q', args }
    fold.feed({ event_kind: 'function_tool_call', part: call })
    const waiting = items[0]
    assert.equal(waiting.kind, 'ask')
    if (waiting.kind !== 'ask') return
    assert.equal(waiting.nativeAgent, label)
    assert.equal(waiting.state, 'waiting')
    assert.equal(waiting.batchId, undefined)
    const html = renderToStaticMarkup(createElement(LedgerRow, { item: waiting, cardMax: '100%' }))
    assert.ok(html.includes(`Answer in ${label}`) && html.includes('Shared code'))
    assert.ok(!html.includes('<textarea') && !html.includes('aria-pressed') && !html.includes('Submit'))

    if (agent === 'DeepSeek') {
      fold.feed({ event_kind: 'function_tool_result', part: { part_kind: 'tool-return', tool_name: tool, tool_call_id: 'native-q', content: '{"pending":true,"callId":"native-q"}' } })
      assert.equal(items[0].kind === 'ask' && items[0].state, 'waiting')
    }
    const returned = { part_kind: 'tool-return' as const, tool_name: tool, tool_call_id: 'native-q', content: result }
    fold.feed({ event_kind: 'function_tool_result', part: returned })
    const answered = items[0]
    assert.equal(answered.kind === 'ask' && answered.state, 'answered')
    assert.deepEqual(answered.kind === 'ask' && answered.questions.map(q => q.answer), answers)
    const messages: ApiModelMessage[] = [
      { id: 'call', run_id: 'run', kind: 'response', timestamp: '2026-10-14T10:00:00Z', thread_messages: [], parts: [call] },
      { id: 'result', run_id: 'run', kind: 'request', timestamp: null, thread_messages: [], parts: [returned] },
    ]
    const [replayed] = replayRun(messages, label, undefined, true)
    assert.deepEqual({ ...replayed.item, uid: answered.uid }, answered)
    assert.equal(replayRun(messages, agent)[0].item.kind, 'tool')
    const driven: string[] = []
    new TurnFold({ push(draft) { driven.push(draft.kind); return 'driven' }, patch() {}, done() {} })
      .feed({ event_kind: 'function_tool_call', part: call })
    assert.deepEqual(driven, ['tool'])
    const settledHtml = renderToStaticMarkup(createElement(LedgerRow, { item: answered, cardMax: '100%' }))
    assert.ok(settledHtml.includes(`Answered in ${label}`))

    fold.feed({ event_kind: 'function_tool_result', part: { ...returned, content: 'The user declined', outcome: 'failed' } })
    assert.equal(items[0].kind === 'ask' && items[0].state, 'failed')
    const failedHtml = renderToStaticMarkup(createElement(LedgerRow, { item: items[0], cardMax: '100%' }))
    assert.ok(failedHtml.includes('Not answered: The user declined'))
  })
}

test('DeepSeek replies after a timeout update the question and show only the human answer', () => {
  let items: LedgerItem[] = []
  const fold = new TurnFold({
    push(draft) { const item = { ...draft, uid: draft.uid ?? String(items.length) } as LedgerItem; items.push(item); return item.uid },
    patch(uid, patch) { items = items.map(item => item.uid === uid ? { ...item, ...patch } as LedgerItem : item) },
    done() {},
  }, 'DeepSeek')
  fold.feed({ event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_name: 'ask_user_question', tool_call_id: 'dsh-q', args: { questions: [{ id: 'scope', question: 'Which scope?' }] } } })
  const reply = JSON.stringify({ kind: 'answer_to_pending_question', tool: 'ask_user_question', callId: 'dsh-q', questions: [{ id: 'scope', question: 'Which scope?' }], answers: [{ id: 'scope', selected: ['core'], custom: 'Just this' }] })
  fold.feed({ event_kind: 'enqueued_messages', enqueue_id: 'reply', messages: [{ kind: 'request', timestamp: null, parts: [{ part_kind: 'user-prompt', content: reply }] }] })
  assert.equal(items[0].kind === 'ask' && items[0].state, 'answered')
  assert.equal(items[1].kind === 'user' && items[1].text, 'core, Just this')
})

test('a batched native reply shows only the user answers, in their original order', () => {
  const replies = [
    { questionItemId: JSON.stringify(['request_user_input_async', 'native-q', 0]), question: 'Inspect the tab?', answer: 'Inspect' },
    { questionItemId: JSON.stringify(['request_user_input_async', 'native-q', 1]), question: 'Refresh?', answer: 'No\nKeep the network logs' },
  ]
  const text = `<send_user_message_question_reply>${JSON.stringify(replies)}</send_user_message_question_reply>`
  assert.equal(promptText(text), 'Inspect\n\nNo\nKeep the network logs')
})

test('ordinary text and malformed native reply envelopes remain visible verbatim', () => {
  for (const text of [
    'My answer is yes',
    '{"kind":"answer_to_pending_question","tool":"ask_user_question","callId":"q","answers":[]}',
    '<send_user_message_question_reply>not JSON</send_user_message_question_reply>',
    '<send_user_message_question_reply>[{"answer":"Yes"}]</send_user_message_question_reply>',
    'Quoted: <send_user_message_question_reply>[]</send_user_message_question_reply>',
  ]) assert.equal(promptText(text), text)
  const [card] = replayRun([{
    id: 'message', run_id: 'run', kind: 'response', timestamp: '2026-10-14T10:00:00Z', thread_messages: [],
    parts: [{ part_kind: 'builtin-tool-call', tool_name: 'request_user_input_async', tool_call_id: 'broken', args: 'not JSON' }],
  }], 'Codex', undefined, true)
  assert.equal(card.item.kind, 'tool')
})

test("a batch's questions are one card, in the order they were asked", () => {
  const [card, ...rest] = batchFeelers('batch', [question('second', 1, 'Then?'), question('first', 0, 'First?')], [])
  assert.deepEqual(rest, [])
  assert.equal(card.kind, 'ask')
  if (card.kind !== 'ask') return
  assert.deepEqual(card.questions.map((q) => [q.actionId, q.body]), [['first', 'First?'], ['second', 'Then?']])
  assert.equal(card.batchId, 'batch')
})

test('a waiting batch shows one question at a time, answered with one click', () => {
  const [draft] = batchFeelers('batch', [question('first', 0, 'First?'), question('second', 1, 'Then?'), question('third', 2, 'Last?')], [])
  const html = renderToStaticMarkup(createElement(LedgerRow, { item: { ...draft, uid: 'card' } as LedgerItem, cardMax: '100%' }))
  assert.ok(html.includes('First?'))
  assert.ok(!html.includes('Then?') && !html.includes('Last?'))
  assert.ok(html.includes('1/3') && html.includes('next ›') && html.includes('Submit 0/3'))
  assert.ok(!html.includes('Choose'))
})

test('a multi-select question keeps its flag on the card', () => {
  const asked = { ...question('checks', 0, 'Which checks?'), args: { question: 'Which checks?', choices: ['lint', 'tests'], multi_select: true } }
  const [card] = batchFeelers('batch', [asked, question('plain', 1, 'Then?')], [])
  assert.equal(card.kind, 'ask')
  if (card.kind !== 'ask') return
  assert.deepEqual(card.questions.map((q) => q.multiSelect), [true, false])
})

test('answering a batch sends every answer in one response, so the run resumes once', async () => {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { getElementById: () => null } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  installLiveSocket()
  const fetch = mock.method(globalThis, 'fetch', async () => {
    queueMicrotask(() => LiveSocket.last().deliver(result, { event_kind: 'reflex_asleep' }))
    return new Response(null, { status: 202 })
  })
  const [draft] = batchFeelers('batch', [question('first', 0, 'First?'), question('second', 1, 'Then?')], [])
  useConsole.setState({ selThreadId: 'thread', live: [{ ...draft, uid: 'card' } as LedgerItem], running: false })

  useConsole.getState().actions.answerAsk('card', ['Yes', ['lint', 'tests']], 'answered 2 questions')
  for (let at = 0; at < 50 && fetch.mock.callCount() === 0; at++) await new Promise((resolve) => setTimeout(resolve, 0))

  assert.equal(fetch.mock.callCount(), 1)
  const [url, init] = fetch.mock.calls[0].arguments
  assert.equal(url, '/api/trunkline/batches/batch/resolve')
  // A multi-select question's picks travel as a list, never as joined text.
  assert.deepEqual(JSON.parse(String(init?.body)), { answers: { first: 'Yes', second: ['lint', 'tests'] } })
  const [answered] = useConsole.getState().live
  assert.equal(answered.kind === 'ask' && answered.state, 'waiting')
})

test('answering a run still streaming sends the answer alone, over the socket already folding it', async () => {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { getElementById: () => null } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  installLiveSocket()
  const fetch = mock.method(globalThis, 'fetch', async () => new Response(null, { status: 202 }))
  const [draft] = batchFeelers('batch', [question('first', 0, 'First?')], [])
  useConsole.setState({ selThreadId: 'thread', live: [{ ...draft, uid: 'card' } as LedgerItem], running: true })

  useConsole.getState().actions.answerAsk('card', ['Yes'], 'answered 1 question')
  for (let at = 0; at < 50 && fetch.mock.callCount() === 0; at++) await new Promise((resolve) => setTimeout(resolve, 0))

  assert.equal(fetch.mock.callCount(), 1)
  assert.equal(fetch.mock.calls[0].arguments[0], '/api/trunkline/batches/batch/resolve')
  assert.deepEqual(LiveSocket.opened, [])
  assert.equal(useConsole.getState().running, true)
  const [answered] = useConsole.getState().live
  assert.equal(answered.kind === 'ask' && answered.state, 'waiting')
})
