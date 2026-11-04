import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { LedgerItem } from '../src/lib/api/types.ts'
import { toolSummary, traceRows } from '../src/features/chat/trace.ts'

const read: Extract<LedgerItem, { kind: 'tool' }> = {
  kind: 'tool', uid: 'read', name: 'Read', icon: 'file', status: 'done',
  detail: { type: 'plain', args: '{}', res: 'Contents' },
}
const command: typeof read = { ...read, uid: 'command', name: 'codex_command_execution', status: 'run' }

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
      { kind: 'trace', uid: read.uid, items: [read], active: false }, boundary,
      { kind: 'trace', uid: command.uid, items: [command], active: true },
    ])
  }
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
