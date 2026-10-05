import assert from 'node:assert/strict'
import { after, afterEach, before, mock, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import type { WireDeferredQuestion, WireEvent } from '../src/lib/api/events.ts'
import type { LedgerItem } from '../src/lib/api/types.ts'

let server: ViteDevServer
let batchFeelers: typeof import('../src/lib/api/fold.ts').batchFeelers
let useConsole: typeof import('../src/state/console.ts').useConsole

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
  ;({ batchFeelers } = await server.ssrLoadModule('/src/lib/api/fold.ts'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
})
afterEach(() => mock.restoreAll())
after(async () => { await server?.close() })

test("a batch's questions are one card, in the order they were asked", () => {
  const [card, ...rest] = batchFeelers('batch', [question('second', 1, 'Then?'), question('first', 0, 'First?')], [])
  assert.deepEqual(rest, [])
  assert.equal(card.kind, 'ask')
  if (card.kind !== 'ask') return
  assert.deepEqual(card.questions.map((q) => [q.actionId, q.body]), [['first', 'First?'], ['second', 'Then?']])
  assert.equal(card.batchId, 'batch')
})

test('answering a batch sends every answer in one response, so the run resumes once', async () => {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: { getElementById: () => null } })
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: () => 0 })
  const fetch = mock.method(globalThis, 'fetch', async () =>
    new Response(`data: ${JSON.stringify(result)}\n\n`, { headers: { 'Content-Type': 'text/event-stream' } }))
  const [draft] = batchFeelers('batch', [question('first', 0, 'First?'), question('second', 1, 'Then?')], [])
  useConsole.setState({ selThreadId: 'thread', live: [{ ...draft, uid: 'card' } as LedgerItem], running: false })

  useConsole.getState().actions.answerAsk('card', ['Yes', 'No'], 'answered 2 questions')
  for (let at = 0; at < 50 && fetch.mock.callCount() === 0; at++) await new Promise((resolve) => setTimeout(resolve, 0))

  assert.equal(fetch.mock.callCount(), 1)
  const [url, init] = fetch.mock.calls[0].arguments
  assert.equal(url, '/api/trunkline/batches/batch/resolve')
  assert.deepEqual(JSON.parse(String(init?.body)), { answers: { first: 'Yes', second: 'No' } })
  const [answered] = useConsole.getState().live
  assert.equal(answered.kind === 'ask' && answered.state, 'answered')
})
