import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer, type ViteDevServer } from 'vite'
import type { LedgerItem } from '../src/lib/api/types.ts'
import type { ThinkingPart } from '../src/lib/api/events.ts'

let server: ViteDevServer
let Markdown: typeof import('../src/components/Markdown.tsx').Markdown
let DraftMarkdown: typeof import('../src/features/chat/DraftMarkdown.tsx').DraftMarkdown
let LedgerRow: typeof import('../src/features/chat/cards.tsx').LedgerRow
let useConsole: typeof import('../src/state/console.ts').useConsole
let useAuth: typeof import('../src/state/auth.ts').useAuth
let TurnFold: typeof import('../src/lib/api/fold.ts').TurnFold
let replayRun: typeof import('../src/lib/api/fold.ts').replayRun

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
  ;({ TurnFold, replayRun } = await server.ssrLoadModule('/src/lib/api/fold.ts'))
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

test('language fences preserve escaped code before highlighting loads', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```python\nimport json\ncount = 42\nprint("<script>literal</script>")\n```' }))
  assert.ok(html.includes('data-language="python"'))
  assert.ok(html.includes('import json\ncount = 42\n'))
  assert.ok(!html.includes('hljs-'))
  assert.ok(html.includes('&lt;script&gt;literal&lt;/script&gt;'))
  assert.ok(!html.includes('<script>'))
})

test('unknown languages remain escaped plain code', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```unknown-language\n<script>literal</script>\n```' }))
  assert.ok(html.includes('&lt;script&gt;literal&lt;/script&gt;'))
  assert.ok(!html.includes('hljs-'))
})

for (const [fence, language] of [['python', 'python'], ['js', 'js'], ['unknown-language', 'unknown-language'], ['', 'Plain text']]) {
  test(`${fence || 'unlabelled'} code gets its language and blank lines counted without changing the source`, () => {
    const html = renderToStaticMarkup(createElement(Markdown, { text: `\`\`\`${fence}\nfirst\n\nthird\n\`\`\`` }))
    assert.ok(html.includes(`<span>${language}</span><span>3 lines</span>`))
    assert.ok(html.includes('class="trk-code-gutter" aria-hidden="true">1\n2\n3</span>'))
    assert.ok(html.includes('first\n\nthird\n</code></pre>'))
  })
}

test('indented code has the same plain-text frame and gutter', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '    first\n    second' }))
  assert.ok(html.includes('<span>Plain text</span><span>2 lines</span>'))
  assert.ok(html.includes('<pre><code>first\nsecond\n</code></pre>'))
})

test('line numbers preserve multiline comments before highlighting loads', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```js\n/* first\nsecond */\nconst count = 1\n```' }))
  assert.ok(html.includes('/* first\nsecond */\nconst count = 1\n</code>'))
  assert.ok(html.includes('<span>3 lines</span>'))
})

test('a code language label cannot inject markup or attributes', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```x"><img/src=x/onerror=alert(1)>\nsafe\n```' }))
  assert.ok(html.includes('x&quot;&gt;&lt;img/src=x/onerror=alert(1)&gt;'))
  assert.ok(!html.includes('<img'))
})

test('Mermaid fences keep their source and printing caret while streaming', () => {
  const html = renderToStaticMarkup(createElement(Markdown, { text: '```mermaid\nflowchart LR\nA["<script>literal</script>"] -->', cursor: true }))
  assert.ok(html.includes('class="trk-mermaid"'))
  assert.ok(html.includes('&lt;script&gt;literal&lt;/script&gt;'))
  assert.match(html, /--&gt;<span[^>]*class="lt-caret trk-print-caret"/)
  assert.ok(!html.includes('<script>'))
  assert.ok(!html.includes('trk-code-header'))
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
  { kind: 'notice', uid: 'notice', text },
]
for (const item of items) {
  test(`${item.kind} messages retain plain-text newlines`, () => {
    const html = renderToStaticMarkup(createElement(LedgerRow, { item, cardMax: '100%' }))
    assert.ok(html.includes(text))
    assert.ok(html.includes('white-space:pre-wrap'))
  })
}

test('thinking renders Markdown while distinguishing summaries from normal reasoning', () => {
  const text = '**Awaiting macOS admin setup**\n\nCheck `permissions` and [the guide](https://example.com).\nNext step.\n\n<script>unsafe()</script>'
  for (const format of ['reasoning', 'summary'] as const) {
    const item: LedgerItem = { kind: 'think', uid: format, dur: '1s', thinking: true, text, format }
    const html = renderToStaticMarkup(createElement(LedgerRow, { item, cardMax: '100%' }))
    assert.ok(html.includes('<strong>Awaiting macOS admin setup</strong>'))
    assert.ok(html.includes('<code>permissions</code>'))
    assert.ok(html.includes('Next step.'))
    assert.ok(html.includes('&lt;script&gt;unsafe()&lt;/script&gt;'))
    assert.ok(html.includes('trk-print-caret'))
    assert.ok(!html.includes('**Awaiting'))
    assert.equal(html.includes('trk-thinking-format'), format === 'summary')
  }
})

for (const format of [undefined, 'reasoning', 'summary'] as const) {
  test(`saved ${format ?? 'unmarked'} thinking without readable text has only an unavailable badge`, () => {
    const cards = replayRun([{
      id: 'response', run_id: 'run', kind: 'response', timestamp: '2026-10-30T10:00:00Z', thread_messages: [],
      parts: [{ part_kind: 'thinking', content: '', provider_name: 'openai_codex', provider_details: { reasoning_format: format } }],
    }], 'Codex')
    assert.equal(cards.length, 1)
    assert.equal(cards[0].item.kind, 'think')
    assert.equal(cards[0].item.kind === 'think' && cards[0].item.thinking, false)
    const html = renderToStaticMarkup(createElement(LedgerRow, { item: { ...cards[0].item, uid: cards[0].id }, cardMax: '100%' }))
    assert.ok(html.includes('aria-expanded="false"'))
    assert.equal((html.match(/>\(unavailable\)<\/span>/g) ?? []).length, 1)
    assert.ok(html.includes('no readable reasoning text or summary'))
    assert.ok(html.includes('trk-thinking-format'))
    assert.ok(!html.includes('>(summary)</span>'))
    assert.ok(!html.includes('trk-print-caret'))
  })
}

for (const [streamed, elapsed, expected] of [[false, 5000, ''], [true, 250, '<1s'], [true, 4200, '4s']] as const) {
  test(`thinking duration is ${expected || 'unknown'} for ${streamed ? 'streamed' : 'completed'} reasoning`, (t) => {
    let now = 1000
    t.mock.method(Date, 'now', () => now)
    const items: LedgerItem[] = []
    const fold = new TurnFold({
      push(draft) { const uid = `part-${items.length}`; items.push({ ...draft, uid }); return uid },
      patch(uid, patch) { Object.assign(items.find(item => item.uid === uid)!, patch) },
      done() {},
    })
    const part: ThinkingPart = { part_kind: 'thinking', content: 'Checking', provider_name: 'openai_codex' }
    fold.feed({ event_kind: 'part_start', index: 0, part })
    if (streamed) fold.feed({ event_kind: 'part_delta', index: 0, delta: { part_delta_kind: 'thinking', content_delta: ' files' } })
    now += elapsed
    if (streamed) {
      fold.feed({ event_kind: 'function_tool_call', part: { part_kind: 'tool-call', tool_name: 'Read', tool_call_id: 'read', args: {} } })
      now += 5000
    }
    fold.feed({ event_kind: 'part_end', index: 0, part: { ...part, content: 'Checked files' } })
    assert.equal(items[0].kind, 'think')
    if (items[0].kind !== 'think') return
    assert.equal(items[0].dur, expected)
    assert.equal(items[0].thinking, false)
    assert.equal(items[0].text, 'Checked files')
  })
}

test('live and replayed reasoning retain their formats and authoritative completion text', () => {
  const items: LedgerItem[] = []
  const fold = new TurnFold({
    push(draft) {
      const uid = `part-${items.length}`
      items.push({ ...draft, uid })
      return uid
    },
    patch(uid, patch) { Object.assign(items.find(item => item.uid === uid)!, patch) },
    done() {},
  })
  const parts: ThinkingPart[] = [
    { part_kind: 'thinking', content: '**Reasoning**', provider_name: 'deepseek' },
    { part_kind: 'thinking', content: '**Summary**', provider_name: 'codex', provider_details: { reasoning_format: 'summary' } },
  ]
  parts.forEach((part, index) => fold.feed({ event_kind: 'part_start', index, part }))
  fold.feed({ event_kind: 'part_delta', index: 0, delta: { part_delta_kind: 'thinking', content_delta: ' in progress' } })
  assert.equal(items[0].kind === 'think' && items[0].text, '**Reasoning** in progress')
  parts.forEach((part, index) => {
    part.content += ' complete'
    fold.feed({ event_kind: 'part_end', index, part })
    if (index === 0) assert.equal(items[1].kind === 'think' && items[1].thinking, true)
  })
  const replayed = replayRun([{
    id: 'response', run_id: 'run', kind: 'response', parts, timestamp: '2026-10-30T10:00:00Z', thread_messages: [],
  }], 'Codex')
  for (const [index, item] of items.entries()) {
    assert.equal(item.kind, 'think')
    if (item.kind !== 'think') continue
    assert.equal(item.text, parts[index].content)
    assert.equal(item.thinking, false)
    assert.equal(item.format, index === 0 ? 'reasoning' : 'summary')
    assert.deepEqual({ ...replayed[index].item, uid: item.uid, dur: item.dur }, item)
  }
})

test('tool icons distinguish execution, files and unknown tools in live calls and replay', () => {
  for (const [name, icon] of [
    ['exec', 'code'], ['functions.exec', 'code'], ['mcp__tools__exec', 'code'],
    ['exec_command', 'terminal'], ['Bash', 'terminal'], ['shell_command', 'terminal'],
    ['codex_command_execution', 'terminal'], ['functions.write_stdin', 'terminal'],
    ['Read', 'file'], ['mcp__search__read_file', 'file'],
    ['Grep', 'search'], ['Glob', 'search'], ['web_search', 'search'],
    ['apply_patch', 'file-diff'], ['codex_file_change', 'file-diff'],
    ['send_message', 'send'], ['mcp__files__unknown', 'wrench'],
  ] as const) {
    const part = { part_kind: 'tool-call' as const, tool_name: name, tool_call_id: 'tool', args: {} }
    const result = { part_kind: 'tool-return' as const, tool_name: name, tool_call_id: 'tool', content: 'Done' }
    const items: LedgerItem[] = []
    const fold = new TurnFold({
      push(draft) { items.push({ ...draft, uid: 'tool' }); return 'tool' },
      patch(uid, patch) { Object.assign(items.find(item => item.uid === uid)!, patch) },
      done() {},
    })
    fold.feed({ event_kind: 'function_tool_call', part })
    for (const status of ['run', 'done'] as const) {
      if (status === 'done') fold.feed({ event_kind: 'function_tool_result', part: result })
      const replayed = replayRun([
        { id: 'call', run_id: 'run', kind: 'response', timestamp: '2026-11-05T00:00:00Z', thread_messages: [], parts: [part] },
        { id: 'result', run_id: 'run', kind: 'request', timestamp: null, thread_messages: [], parts: status === 'done' ? [result] : [] },
      ], 'Codex')
      assert.equal(items[0].kind === 'tool' && items[0].icon, icon, `${name} live ${status}`)
      assert.equal(replayed[0].item.kind === 'tool' && replayed[0].item.icon, icon, `${name} replay ${status}`)
    }
  }
})

for (const serialized of [false, true]) {
  test(`tool briefs use ${serialized ? 'serialized' : 'structured'} recorded targets before clipping in live calls and replay`, () => {
    for (const [name, args, kind, target] of [
      ['Read', { file_path: '/work/MEMORY.md' }, 'read', 'MEMORY.md'],
      ['mcp__filesystem__read_multiple_files', { paths: ['/work/a.py', '/work/b.py'] }, 'read', 'a.py, b.py'],
      ['Bash', { command: 'git status --short' }, 'command', 'git status --short'],
      ['functions.exec_command', { cmd: 'git diff\n--stat' }, 'command', 'git diff --stat'],
      ['Grep', { pattern: 'TODO' }, 'search', 'TODO'],
      ['Edit', { file_path: '/work/app.py' }, 'edit', 'app.py'],
      ['ToolSearch', { query: 'files' }, 'load', 'tools: files'],
      ['codex_command_execution', { command: 'cat /work/MEMORY.md', padding: '.'.repeat(800), commandActions: [{ type: 'read', path: '/work/MEMORY.md' }] }, 'read', 'MEMORY.md'],
      ['codex_command_execution', { command: 'rg FIXME /work', commandActions: [{ type: 'search', query: 'FIXME', path: '/work' }] }, 'search', 'FIXME'],
      ['codex_command_execution', { command: 'ls /work', commandActions: [{ type: 'list_files', path: '/work' }] }, 'list', '/work'],
      ['codex_command_execution', { command: '/bin/zsh -lc "git status --short"', commandActions: [{ type: 'unknown', cmd: 'git status --short' }] }, 'command', 'git status --short'],
    ] as const) {
      const part = { part_kind: 'tool-call' as const, tool_name: name, tool_call_id: 'brief', args: serialized ? JSON.stringify(args) : args }
      const result = { part_kind: 'tool-return' as const, tool_name: name, tool_call_id: 'brief', content: 'Finished' }
      const items: LedgerItem[] = []
      const fold = new TurnFold({
        push(draft) { items.push({ ...draft, uid: 'brief' }); return 'brief' },
        patch(uid, patch) { Object.assign(items.find(item => item.uid === uid)!, patch) },
        done() {},
      })
      fold.feed({ event_kind: 'function_tool_call', part })
      assert.equal(items[0].kind, 'tool')
      assert.deepEqual(items[0].kind === 'tool' && items[0].action, { kind, target })
      fold.feed({ event_kind: 'function_tool_result', part: result })
      const replayed = replayRun([
        { id: 'call', run_id: 'run', kind: 'response', timestamp: '2026-11-05T00:00:00Z', thread_messages: [], parts: [part] },
        { id: 'result', run_id: 'run', kind: 'request', timestamp: null, thread_messages: [], parts: [result] },
      ], 'Codex')
      assert.deepEqual({ ...replayed[0].item, uid: 'brief' }, items[0])
      const html = renderToStaticMarkup(createElement(LedgerRow, { item: items[0], cardMax: '100%' }))
      assert.ok(!html.includes('>Tool</span>'))
      assert.ok(html.includes(target))
      if (kind === 'read') assert.equal(items[0].kind === 'tool' && items[0].icon, 'file')
    }
  })
}

test('freeform code gets a brief while unknown tools and incomplete JSON keep their original names', () => {
  const cards = replayRun([{
    id: 'call', run_id: 'run', kind: 'response', timestamp: '2026-11-05T00:00:00Z', thread_messages: [], parts: [
      { part_kind: 'tool-call', tool_name: 'exec', tool_call_id: 'code', args: 'const result = await tools.read_file(...)' },
      { part_kind: 'tool-call', tool_name: 'apply_patch', tool_call_id: 'patch', args: '*** Begin Patch\n*** Update File: app.py\n*** End Patch' },
      { part_kind: 'tool-call', tool_name: 'Read', tool_call_id: 'partial', args: '{"file_path":' },
      { part_kind: 'tool-call', tool_name: 'custom_tool', tool_call_id: 'unknown', args: { command: 'not necessarily a shell command' } },
    ],
  }], 'Codex')
  assert.deepEqual(cards.map(card => card.item.kind === 'tool' && card.item.action), [{ kind: 'code', target: 'code' }, { kind: 'edit', target: 'files' }, undefined, undefined])
})

for (const serialized of [false, true]) {
  test(`command classifications survive ${serialized ? 'serialized' : 'structured'} arguments and display clipping live and on replay`, () => {
    const args = { command: 'cat app.py', padding: '.'.repeat(800), commandActions: [{ type: 'read' }, { type: 'list_files' }, { type: 'search' }, { type: 'read' }, { type: 'unknown' }] }
    const part = { part_kind: 'tool-call' as const, tool_name: 'codex_command_execution', tool_call_id: 'command', args: serialized ? JSON.stringify(args) : args }
    const items: LedgerItem[] = []
    const fold = new TurnFold({
      push(draft) { items.push({ ...draft, uid: 'command' }); return 'command' },
      patch(uid, patch) { Object.assign(items.find(item => item.uid === uid)!, patch) },
      done() {},
    })
    fold.feed({ event_kind: 'function_tool_call', part })
    fold.feed({ event_kind: 'function_tool_result', part: { part_kind: 'tool-return', tool_name: part.tool_name, tool_call_id: part.tool_call_id, content: 'Contents' } })
    const replayed = replayRun([
      { id: 'call', run_id: 'run', kind: 'response', timestamp: '2026-11-04T10:00:00Z', thread_messages: [], parts: [part] },
      { id: 'result', run_id: 'run', kind: 'request', timestamp: null, thread_messages: [], parts: [{ part_kind: 'tool-return', tool_name: part.tool_name, tool_call_id: part.tool_call_id, content: 'Contents' }] },
    ], 'Codex')
    for (const item of [items[0], replayed[0].item]) {
      assert.equal(item.kind, 'tool')
      if (item.kind !== 'tool') continue
      assert.deepEqual(item.activities, ['read', 'list', 'search', 'command'])
      assert.ok(item.detail.type === 'plain' && !item.detail.args.includes('commandActions'))
    }
  })
}

test('progress messages stay separate and completed plans use the final harness text', () => {
  const items: LedgerItem[] = []
  const fold = new TurnFold({
    push(draft) {
      const uid = `message-${items.length}`
      items.push({ ...draft, uid })
      return uid
    },
    patch(uid, patch) { Object.assign(items.find(item => item.uid === uid)!, patch) },
    done() {},
  })
  fold.feed({ event_kind: 'part_start', index: 0, part: { part_kind: 'text', content: 'Checking setup' } })
  fold.feed({ event_kind: 'part_end', index: 0, part: { part_kind: 'text', content: 'Checking setup and permissions.' } })
  fold.feed({ event_kind: 'part_start', index: 1, part: { part_kind: 'text', content: 'Setup is ready.' } })
  fold.feed({ event_kind: 'part_end', index: 1, part: { part_kind: 'text', content: 'Setup is ready.' } })
  assert.deepEqual(items.map(item => item.kind === 'stream' && [item.text, item.streaming]), [
    ['Checking setup and permissions.', false], ['Setup is ready.', false],
  ])
})

for (const [agent, label] of [
  [undefined, 'Octomate'],
  ['Claude', 'Octomate(Claude)'],
  ['Codex', 'Octomate(Codex)'],
  ['DeepSeek', 'Octomate(Dsh)'],
  ['dsh', 'Octomate(Dsh)'],
] as const) {
  test(`${agent ?? 'unidentified'} bot messages use the same Octomate identity live and on replay`, () => {
    const items: LedgerItem[] = []
    const fold = new TurnFold({
      push(draft) { items.push({ ...draft, uid: 'reply' }); return 'reply' },
      patch() {}, done() {},
    }, agent)
    fold.feed({ event_kind: 'message_sent', segments: [{ type: 'text', data: { text: 'Answer' } }] })
    const replayed = replayRun([{
      id: 'response', run_id: 'run', kind: 'response', timestamp: '2026-10-30T10:00:00Z', thread_messages: [],
      parts: [{ part_kind: 'text', content: 'Answer' }],
    }], agent ?? 'agent')
    assert.deepEqual(replayed[0].item, { kind: 'agent', label, blocks: [{ type: 'p', text: 'Answer' }] })
    assert.deepEqual(items[0], { ...replayed[0].item, uid: 'reply' })
    const html = renderToStaticMarkup(createElement(LedgerRow, { item: items[0], cardMax: '100%' }))
    assert.ok(html.includes(`>${label}</span>`))
  })
}
