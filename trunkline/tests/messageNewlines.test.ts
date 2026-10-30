import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer, type ViteDevServer } from 'vite'
import type { LedgerItem } from '../src/lib/api/types.ts'

let server: ViteDevServer
let Markdown: typeof import('../src/components/Markdown.tsx').Markdown
let DraftMarkdown: typeof import('../src/features/chat/DraftMarkdown.tsx').DraftMarkdown
let LedgerRow: typeof import('../src/features/chat/cards.tsx').LedgerRow
let useConsole: typeof import('../src/state/console.ts').useConsole
let useAuth: typeof import('../src/state/auth.ts').useAuth

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    server: { middlewareMode: true, watch: null, ws: false }, appType: 'custom',
  })
  ;({ Markdown } = await server.ssrLoadModule('/src/components/Markdown.tsx'))
  ;({ DraftMarkdown } = await server.ssrLoadModule('/src/features/chat/DraftMarkdown.tsx'))
  ;({ LedgerRow } = await server.ssrLoadModule('/src/features/chat/cards.tsx'))
  ;({ useConsole } = await server.ssrLoadModule('/src/state/console.ts'))
  ;({ useAuth } = await server.ssrLoadModule('/src/state/auth.ts'))
})
after(async () => { await server?.close() })

test('Markdown preserves single newlines and paragraph boundaries', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '**First**\nSecond\n\nThird' }))
  assert.ok(html.includes('<strong>First</strong><br>\nSecond</p>\n<p>Third</p>'))
})

test('the draft renders headings and bullets while leaving code and HTML literal', () => {
  const html = renderToStaticMarkup(createElement(DraftMarkdown, { text: '# Heading\n- one\n  - two\n\n```\n# code\n- code\n```\n<script>literal</script>' }))
  assert.ok(html.includes('font-weight:700'))
  assert.equal((html.match(/•/g) ?? []).length, 2)
  assert.ok(html.includes('# code'))
  assert.ok(html.includes('- code'))
  assert.ok(html.includes('&lt;script&gt;literal&lt;/script&gt;'))
})

test('group participants keep bubbles on the left while linked self messages stay right', () => {
  const state = useConsole.getInitialState()
  const auth = useAuth.getInitialState()
  const previous = { detail: state.detail, user: auth.user }
  state.detail = { key: 'group', kind: 'group', msgCount: 0, sessions: [], ledger: [], ctxK: 0, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cacheRate: null } }
  auth.user = { id: 'me', username: 'alice', name: 'Alice', nickname: null }
  try {
    for (const [userId, alignment] of [['me', 'flex-end'], ['other', 'flex-start'], [null, 'flex-start']] as const) {
      const item: LedgerItem = { kind: 'user', uid: 'message', t: '12:00', who: 'Alice', userId, text: 'Hello' }
      const html = renderToStaticMarkup(createElement(LedgerRow, { item, cardMax: '100%' }))
      assert.ok(html.includes(`align-items:${alignment}`))
      assert.ok(html.includes('border-radius:16px'))
    }
  } finally {
    state.detail = previous.detail
    auth.user = previous.user
  }
})

test('newline rendering keeps code literal and raw HTML escaped', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```\na\nb\n```\n\n<script>alert(1)</script>' }))
  assert.ok(html.includes('<pre><code>a\nb\n</code></pre>'))
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(!html.includes('<script>'))
})

test('language fences highlight code without changing its escaped content', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```python\nimport json\ncount = 42\nprint("<script>literal</script>")\n```' }))
  assert.ok(html.includes('class="hljs-keyword">import</span>'))
  assert.ok(html.includes('class="hljs-number">42</span>'))
  assert.ok(html.includes('class="hljs-string">'))
  assert.ok(html.includes('&lt;script&gt;literal&lt;/script&gt;'))
  assert.ok(!html.includes('<script>'))
})

test('unknown languages remain escaped plain code', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```unknown-language\n<script>literal</script>\n```' }))
  assert.ok(html.includes('&lt;script&gt;literal&lt;/script&gt;'))
  assert.ok(!html.includes('hljs-'))
})

test('Mermaid fences keep their source and printing caret while streaming', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```mermaid\nflowchart LR\nA["<script>literal</script>"] -->', cursor: true }))
  assert.ok(html.includes('class="trk-mermaid"'))
  assert.ok(html.includes('&lt;script&gt;literal&lt;/script&gt;'))
  assert.match(html, /--&gt;<span[^>]*class="lt-caret trk-print-caret"/)
  assert.ok(!html.includes('<script>'))
})

for (const kind of ['user', 'agent'] as const) {
  test(`${kind} reply previews show the author and escape quoted HTML`, () => {
    const reply = { who: 'Ada', text: '<script>quoted</script>\nSecond line' }
    const item: LedgerItem = kind === 'user'
      ? { kind, uid: 'reply', t: '12:00', who: 'Bob', text: 'Answer', reply }
      : { kind, uid: 'reply', label: 'inkling', blocks: [{ type: 'p', text: 'Answer' }], reply }
    const html = renderToStaticMarkup(createElement(LedgerRow, { item, cardMax: '100%' }))
    assert.ok(html.includes('<blockquote aria-label="Reply to"'))
    assert.ok(html.includes('Ada'))
    assert.ok(html.includes('&lt;script&gt;quoted&lt;/script&gt;\nSecond line'))
    assert.ok(!html.includes('<script>'))
    assert.ok(html.indexOf('</blockquote>') < html.indexOf('Answer'))
  })
}

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
