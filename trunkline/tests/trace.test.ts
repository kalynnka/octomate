import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { LedgerItem } from '../src/lib/api/types.ts'
import type { MessageAnnotations } from '../src/lib/api/events.ts'
import { toolSummary, traceRows } from '../src/features/chat/trace.ts'

const read: Extract<LedgerItem, { kind: 'tool' }> = {
  kind: 'tool', uid: 'read', name: 'Read', icon: 'file', status: 'done',
  detail: { type: 'plain', args: '{}', res: 'Contents' },
}
const command: typeof read = { ...read, uid: 'command', name: 'codex_command_execution', status: 'run' }
const annotations: MessageAnnotations = {
  source: 'codex', raw: 'Original prompt and context',
  contexts: [{ title: 'IDE context', text: '## Active file\n\nsrc/main.py', rollout_ids: [] }],
  references: [{ path: 'src/main.py', roles: ['Active file'], start_line: null, end_line: null, note: null }],
}

test('prompt context starts the assistant side before thinking and tools', () => {
  const prompt: LedgerItem = { kind: 'user', uid: 'prompt', t: '', who: 'operator', text: 'Review this file.', annotations }
  const thinking: LedgerItem = { kind: 'think', uid: 'thinking', agentId: 'codex', text: 'Inspecting the file', thinking: false, dur: '' }
  const rows = traceRows([prompt, thinking, { ...read, agentId: 'codex' }], false)
  assert.deepEqual(rows.map(row => row.kind), ['user', 'context', 'trace'])
  assert.equal(rows[0], prompt)
  assert.deepEqual(rows[1], { kind: 'context', uid: 'prompt.context.0.0', agentId: 'codex', source: 'codex', context: annotations.contexts[0], references: [] })
  assert.deepEqual(traceRows([prompt], true), rows.slice(0, 2))
})

for (const kind of ['agent', 'stream'] as const) {
  test(`${kind} keeps prepended context before its content and memory sources after it`, () => {
    const withMemory: MessageAnnotations = { ...annotations, contexts: [...annotations.contexts, { title: 'Memory sources', text: 'Memory citation', rollout_ids: [] }] }
    const message: LedgerItem = kind === 'agent'
      ? { kind, uid: 'reply', agentId: 'codex', label: 'Codex', blocks: [{ type: 'p', text: 'Answer.', annotations: withMemory }] }
      : { kind, uid: 'reply', agentId: 'codex', text: 'Answer.', streaming: true, annotations: withMemory }
    const rows = traceRows([message], true)
    assert.deepEqual(rows.map(row => row.kind), ['context', 'annotated'])
    assert.equal(rows[1].kind === 'annotated' && rows[1].item, message)
    assert.equal(rows[0].kind === 'context' && rows[0].context.title, 'IDE context')
    assert.equal(rows[1].kind === 'annotated' && rows[1].after[0].context.title, 'Memory sources')
  })
}

test('only opened-file context is replaced by chips; memory and selection remain cards', () => {
  const selection = { title: 'Selected code', text: 'run()', rollout_ids: [] }
  const prompt: LedgerItem = { kind: 'user', uid: 'prompt', t: '', who: 'operator', text: 'Review this file.', annotations: {
    ...annotations,
    contexts: [{ title: 'Opened file', text: 'src/main.py', rollout_ids: [] }, { title: 'Memory sources', text: 'Memory citation', rollout_ids: [] }, selection],
    references: [{ ...annotations.references[0], roles: ['Opened file', 'Selection'] }, { path: 'MEMORY.md', roles: ['Memory source'], start_line: 1, end_line: 2, note: null }],
  } }
  const rows = traceRows([prompt], false)
  assert.deepEqual(rows.map(row => row.kind), ['annotated', 'context'])
  assert.equal(rows[0].kind === 'annotated' && rows[0].item, prompt)
  assert.equal(rows[0].kind === 'annotated' && rows[0].after[0].context.title, 'Memory sources')
  assert.deepEqual(rows[0].kind === 'annotated' && rows[0].after[0].references, [prompt.annotations!.references[1]])
  assert.equal(rows[1].kind === 'context' && rows[1].context, selection)
})

test('consecutive calls stay open through results and fold when the run ends', () => {
  assert.deepEqual(traceRows([read, command], true), [{ kind: 'trace', uid: read.uid, items: [read, command], active: true }])
  const finished = { ...command, status: 'done' as const }
  assert.deepEqual(traceRows([read, finished], true), [{ kind: 'trace', uid: read.uid, items: [read, finished], active: true }])
  assert.deepEqual(traceRows([read, finished], false), [{ kind: 'trace', uid: read.uid, items: [read, finished], active: false }])
})

test('thinking and tools share a group in transcript order', () => {
  const thinking: Extract<LedgerItem, { kind: 'think' }> = { kind: 'think', uid: 'thinking', dur: '', text: '**Next step**', format: 'summary', thinking: true }
  const items = [thinking, read, { ...thinking, uid: 'next' }, command]
  assert.deepEqual(traceRows(items, true), [{ kind: 'trace', uid: thinking.uid, items, active: true }])
  assert.deepEqual(traceRows(items, false), [{ kind: 'trace', uid: thinking.uid, items, active: false }])
  assert.deepEqual(traceRows([thinking], true), [{ kind: 'trace', uid: thinking.uid, items: [thinking], active: true }])
})

test('messages, actions and run boundaries end the group', () => {
  const boundaries: LedgerItem[] = [
    { kind: 'agent', uid: 'message', label: 'Codex', blocks: [{ type: 'p', text: 'Next step' }] },
    { kind: 'approval', uid: 'approval', title: 'Allow command', desc: '', tool: 'Bash', meta: '', state: 'waiting' },
    { kind: 'divider', uid: 'end', label: 'Finished' },
  ]
  for (const boundary of boundaries) {
    assert.deepEqual(traceRows([read, boundary, command], true), [
      read, boundary,
      { kind: 'trace', uid: command.uid, items: [command], active: true },
    ])
  }
})

test('finished single-item groups render the original thinking or tool item directly', () => {
  const thinking: Extract<LedgerItem, { kind: 'think' }> = { kind: 'think', uid: 'thinking', dur: '', text: '**Next step**', format: 'summary', thinking: false }
  const failed: typeof read = { ...read, badge: { label: 'failed', tone: 'terra' } }
  for (const item of [thinking, read, failed]) {
    assert.deepEqual(traceRows([item], true), [{ kind: 'trace', uid: item.uid, items: [item], active: true }])
    const rows = traceRows([item], false)
    assert.equal(rows.length, 1)
    assert.equal(rows[0], item)
  }
})

test('consecutive activity from different agents keeps separate groups', () => {
  const first = { ...read, agentId: 'codex' }
  const second = { ...command, agentId: 'claude' }
  assert.deepEqual(traceRows([first, second], true), [
    first, { kind: 'trace', uid: second.uid, items: [second], active: true },
  ])
})

test('tool summaries describe recorded capabilities and deduplicate repeated calls', () => {
  const load = { ...read, uid: 'load', name: 'ToolSearch' }
  assert.equal(toolSummary([load, read, command]), 'Loaded tools, read files, running a command')
  assert.equal(toolSummary([load, read, { ...command, status: 'done' }]), 'Loaded tools, read files, ran a command')
  assert.equal(toolSummary([read, { ...read, name: 'mcp__filesystem__read_file' }, command, command]), 'Read files, running 2 commands')
  assert.equal(toolSummary([{ ...read, name: 'custom_tool' }]), 'Used tools')
  assert.equal(toolSummary([
    { ...read, name: 'exec' },
    { ...command, status: 'done', activities: ['read', 'list', 'search', 'command'] },
  ]), 'Read files, listed files, searched, ran a command')
})
