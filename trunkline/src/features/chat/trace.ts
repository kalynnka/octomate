import type { LedgerItem } from '@/lib/api/types'
import type { IconName } from '@/components/Icon'

type ToolItem = Extract<LedgerItem, { kind: 'tool' }>
export type TraceGroup = { kind: 'trace'; uid: string; items: Extract<LedgerItem, { kind: 'tool' | 'think' }>[]; active: boolean }

const toolIcon: Record<ToolItem['icon'], IconName> = {
  search: 'search',
  'file-diff': 'fileDiff',
  summon: 'userPlus',
  send: 'send',
  file: 'file',
  code: 'code',
  terminal: 'terminal',
  wrench: 'wrench',
}

/** The ledger and timeline use the same recorded action, icon and outcome. */
export function toolHeader(item: ToolItem) {
  const status = item.status === 'run' ? 'run' : item.badge?.label === 'failed' ? 'failed' : 'done'
  const color = status === 'run' ? 'var(--color-teal)' : status === 'failed' ? 'var(--color-red)' : 'var(--color-sage)'
  const verbs = { read: ['Reading', 'Read'], list: ['Listing', 'Listed'], search: ['Searching', 'Searched'], command: ['Running', 'Ran'], edit: ['Editing', 'Edited'], code: ['Running', 'Ran'], load: ['Loading', 'Loaded'] }
  const title = item.action ? `${verbs[item.action.kind][status === 'run' ? 0 : 1]} ${item.action.target}`.trim() : item.name
  return { title, icon: toolIcon[item.icon], status, color }
}

/** Consecutive thinking and calls share a group; results update calls in place. */
export function traceRows(items: LedgerItem[], running: boolean): (LedgerItem | TraceGroup)[] {
  const rows: (LedgerItem | TraceGroup)[] = []
  for (const item of items) {
    if (item.kind !== 'tool' && item.kind !== 'think') {
      rows.push(item)
      continue
    }
    let group = rows.at(-1)
    if (group?.kind !== 'trace' || group.items[0]?.agentId !== item.agentId) {
      group = { kind: 'trace', uid: item.uid, items: [], active: false }
      rows.push(group)
    }
    group.items.push(item)
  }
  const last = rows.at(-1)
  if (last?.kind === 'trace') last.active = running
  return rows.map(row => row.kind === 'trace' && !row.active && row.items.length === 1 ? row.items[0] : row)
}

/** Summarize known tool capabilities, without guessing from commands or output. */
export function toolSummary(items: ToolItem[]): string {
  const activities = new Map<string, { count: number; running: boolean }>()
  for (const item of items) {
    const name = item.name.toLowerCase().split(/__|\./).at(-1) ?? ''
    const activity = /^(toolsearch|tool_search|tool_search_tool_regex|search_tools|list_tools|mcp_list_tools)$/.test(name) ? 'load'
      : /^(read|read_file|read_text_file|read_multiple_files)$/.test(name) ? 'read'
      : /^(bash|shell|shell_command|exec_command|codex_command_execution)$/.test(name) ? 'command'
      : /^(search|web_search|codex_web_search|grep|glob)$/.test(name) ? 'search'
      : /^(edit|write|write_file|edit_file|apply_patch|codex_file_change)$/.test(name) ? 'edit'
      : 'tool'
    for (const kind of item.activities ?? [activity]) {
      const previous = activities.get(kind)
      activities.set(kind, { count: (previous?.count ?? 0) + 1, running: previous?.running === true || item.status === 'run' })
    }
  }
  if (activities.size > 1) activities.delete('tool')
  return [...activities].map(([activity, { count, running }], index) => {
    const labels: Record<string, string> = {
      load: running ? 'loading tools' : 'loaded tools',
      read: running ? 'reading files' : 'read files',
      list: running ? 'listing files' : 'listed files',
      command: `${running ? 'running' : 'ran'} ${count === 1 ? 'a command' : `${count} commands`}`,
      search: running ? 'searching' : 'searched',
      edit: running ? 'editing files' : 'edited files',
      tool: running ? 'using tools' : 'used tools',
    }
    const text = labels[activity]
    return index === 0 ? text[0].toUpperCase() + text.slice(1) : text
  }).join(', ')
}
