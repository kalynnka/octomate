import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer, type ViteDevServer } from 'vite'
import type { LedgerItem } from '../src/lib/api/types.ts'

let server: ViteDevServer
let Markdown: typeof import('../src/components/Markdown.tsx').Markdown
let LedgerRow: typeof import('../src/features/chat/cards.tsx').LedgerRow
let useConsole: typeof import('../src/state/console.ts').useConsole

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ Markdown } = await server.ssrLoadModule('/src/components/Markdown.tsx'))
  ;({ LedgerRow } = await server.ssrLoadModule('/src/features/chat/cards.tsx'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
})
after(async () => { await server?.close() })

test('Markdown preserves single newlines and paragraph boundaries', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '**First**\nSecond\n\nThird' }))
  assert.ok(html.includes('<strong>First</strong><br>\nSecond</p>\n<p>Third</p>'))
})

test('newline rendering keeps code literal and raw HTML escaped', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```\na\nb\n```\n\n<script>alert(1)</script>' }))
  assert.ok(html.includes('<pre><code>a\nb\n</code></pre>'))
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(!html.includes('<script>'))
})

for (const [text, ending] of [
  ['Hello **world**', '</strong>'],
  ['- first\n- last', 'last'],
  ['> last', 'last'],
  ['# Heading', 'Heading'],
  ['| One | Two |\n| --- | --- |\n| first | last |', 'last'],
  ['```\n<script>last</script>', '&lt;script&gt;last&lt;/script&gt;'],
]) {
  test(`the printing caret follows the final content in ${JSON.stringify(text)}`, () => {
    const html = renderToStaticMarkup(createElement(Markdown, { text, cursor: true }))
    assert.match(html, new RegExp(`${ending}<span[^>]*class="lt-caret trk-print-caret"`))
    assert.equal((html.match(/trk-print-caret/g) ?? []).length, 1)
    assert.ok(!html.includes('<script>'))
  })
}

test('an idle turn displays all its text without a stale printing cursor', () => {
  const initial = useConsole.getInitialState()
  const running = initial.running
  initial.running = false
  try {
    const item: LedgerItem = { kind: 'stream', uid: 'reply', text: 'Finished reply', streaming: true }
    const html = renderToStaticMarkup(createElement(LedgerRow, { item, cardMax: '100%' }))
    assert.ok(html.includes('Finished reply'))
    assert.ok(!html.includes('lt-caret'))
  } finally {
    initial.running = running
  }
})

const text = 'First line\n\nSecond line'
const items: LedgerItem[] = [
  { kind: 'user', uid: 'user', t: '12:00', who: 'test', text },
  { kind: 'system', uid: 'system', text },
  { kind: 'think', uid: 'think', dur: '', thinking: true, text },
  { kind: 'notice', uid: 'notice', text },
]
for (const item of items) {
  test(`${item.kind} messages retain plain-text newlines`, () => {
    const html = renderToStaticMarkup(createElement(LedgerRow, { item, cardMax: '100%' }))
    assert.ok(html.includes(text))
    assert.ok(html.includes('white-space:pre-wrap'))
  })
}
