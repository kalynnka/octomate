import type { CSSProperties } from 'react'
import { Brackets } from '@/components/Brackets'
import { FileReference as FileReferenceControl } from '@/components/FileReference'
import { Disclose, Fold } from '@/components/Fold'
import { Icon } from '@/components/Icon'
import { Markdown } from '@/components/Markdown'
import { serif } from '@/components/text'
import type { FileReference, MessageAnnotations } from '@/lib/api/events'
import { useConsole } from '@/state/console'
import type { ContextRow } from './trace'

function ReferenceTag({ reference: ref, source, workspace }: { reference: FileReference; source: MessageAnnotations['source']; workspace?: string }) {
  const memory = ref.roles.includes('Memory source')
  const lines = ref.start_line === null ? '' : `:${ref.start_line}${ref.end_line !== null && ref.end_line !== ref.start_line ? `–${ref.end_line}` : ''}`
  const reference = `${ref.path}${lines}`
  if (memory) return <div className="trk-memory-reference">
    <FileReferenceControl path={reference}><span>Memory source · {source}</span></FileReferenceControl>
    {ref.note && <div>{ref.note}</div>}
  </div>
  const label = ref.path.split(/[\\/]/).at(-1) ?? ref.path
  return <span className="trk-reference">
    <FileReferenceControl path={reference} label={`${label}${lines}`} chip>
      <span>{ref.roles.join(' · ')} · {source}</span>
      {workspace && <span>Workspace: {workspace}</span>}
      {ref.note && <span>{ref.note}</span>}
    </FileReferenceControl>
  </span>
}

export function MessageReferences({ annotations, workspace, position }: { annotations?: MessageAnnotations | null; workspace?: string; position: 'before' | 'after' }) {
  if (!annotations || position === 'after') return null
  const references = annotations.references.filter(ref => !ref.roles.includes('Memory source'))
  if (!references.length) return null
  return <div className="trk-message-references" data-position={position}>
    <div className="trk-reference-tags" aria-label="Message annotations">
      {references.map(ref => <ReferenceTag key={`${ref.path}:${ref.start_line}:${ref.end_line}`} reference={ref} source={annotations.source} workspace={workspace} />)}
    </div>
  </div>
}

export function MessageContext({ item, cardMax }: { item: ContextRow; cardMax: string }) {
  const open = useConsole(s => s.open[item.uid] ?? false)
  const { toggleCardOpen } = useConsole(s => s.actions)
  const { context } = item
  return <div id={`pm-${item.uid}`} className="trk-message-context trk-trace-row" data-expanded={open ? '' : undefined} style={{ '--trk-trace-max': cardMax } as CSSProperties}>
    {open && <Brackets />}
    <button type="button" className="trk-thinking-toggle hov-wash" aria-label={context.title} aria-expanded={open} aria-controls={`${item.uid}-content`} onClick={() => toggleCardOpen(item.uid, false)}>
      {!open && <span className="trk-trace-hover-brackets" aria-hidden="true"><Brackets size={6} /></span>}
      <Icon name={context.title === 'Browser instructions' ? 'globe' : context.title === 'Memory sources' ? 'file' : 'code'} />
      <span className="trk-trace-title" style={{ font: 'var(--meta)' }}>{context.title}</span>
      <Disclose open={open} />
    </button>
    <Fold open={open}>
      <div id={`${item.uid}-content`} inert={!open} className="trk-context-content" style={{ ...serif(12), lineHeight: 1.5 }}>
        {item.references.length > 0
          ? item.references.map(ref => <ReferenceTag key={`${ref.path}:${ref.start_line}:${ref.end_line}`} reference={ref} source={item.source} />)
          : <Markdown text={context.text} />}
        {context.rollout_ids.length > 0 && <div className="trk-context-rollouts">Rollouts{context.rollout_ids.map(value => <div key={value}>{value}</div>)}</div>}
      </div>
    </Fold>
  </div>
}
