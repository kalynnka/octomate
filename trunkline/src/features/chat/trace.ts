import type { LedgerItem } from '@/lib/api/types'
import type { MessageAnnotations } from '@/lib/api/events'
import type { IconName } from '@/components/Icon'

type ToolItem = Extract<LedgerItem, { kind: 'tool' }>
export type TraceGroup = { kind: 'trace'; uid: string; items: Extract<LedgerItem, { kind: 'tool' | 'think' }>[]; active: boolean }
export type ContextRow = { kind: 'context'; uid: string; agentId: string; source: MessageAnnotations['source']; context: MessageAnnotations['contexts'][number]; references: MessageAnnotations['references'] }
export type AnnotatedMessage = { kind: 'annotated'; uid: string; item: Extract<LedgerItem, { kind: 'user' | 'agent' | 'stream' }>; after: ContextRow[] }

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
export function traceRows(items: LedgerItem[], running: boolean): (LedgerItem | TraceGroup | ContextRow | AnnotatedMessage)[] {
  const rows: (LedgerItem | TraceGroup | ContextRow | AnnotatedMessage)[] = []
  for (const item of items) {
    if (item.kind !== 'tool' && item.kind !== 'think') {
      const annotations = item.kind === 'agent'
        ? item.blocks.flatMap(block => block.type === 'p' && block.annotations ? [block.annotations] : [])
        : (item.kind === 'user' || item.kind === 'stream') && item.annotations ? [item.annotations] : []
      const contexts = annotations.flatMap((annotation, block) => annotation.contexts
        .filter(context => !(context.title === 'Opened file' && annotation.references.some(ref => ref.roles.includes('Opened file'))))
        .map((context, index): ContextRow => ({
          kind: 'context', uid: `${item.uid}.context.${block}.${index}`, agentId: 'agentId' in item ? item.agentId ?? annotation.source : annotation.source,
          source: annotation.source, context, references: context.title === 'Memory sources' ? annotation.references.filter(ref => ref.roles.includes('Memory source')) : [],
        })))
      const before = contexts.filter(row => row.context.title !== 'Memory sources')
      const after = contexts.filter(row => row.context.title === 'Memory sources')
      const message: LedgerItem | AnnotatedMessage = after.length && (item.kind === 'user' || item.kind === 'agent' || item.kind === 'stream')
        ? { kind: 'annotated', uid: item.uid, item, after } : item
      if (item.kind === 'user') rows.push(message, ...before)
      else rows.push(...before, message)
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
