/**
 * Ledger cards — one renderer per LedgerItem kind, markup ported from the
 * design comp. Chat rows carry id "pm-{uid}" (session rules use their own
 * uid) so the timeline can jump to them.
 */
import { Fragment, useState, type CSSProperties, type ReactNode } from 'react'
import type { AgentBlock, AskOption, LedgerItem, QueueChip, ToolDetail } from '@/lib/api/types'
import { useConsole } from '@/state/console'
import { Icon, type IconName } from '@/components/Icon'
import { Disclose, Fold } from '@/components/Fold'
import { cardKind, display, ellipsis, fieldLabel, label, metaLine, microMeta, microSection, mono, sectionLabel, serif, statusNote } from '@/components/text'

const ghost3: CSSProperties = { color: 'var(--fg-3)' }

/** Corner brackets that frame tool and file cards. */
function Brackets() {
  const c = (pos: CSSProperties, borders: CSSProperties) => (
    <span style={{ position: 'absolute', width: 11, height: 11, ...pos, ...borders }} />
  )
  const b = '2px solid var(--trk-bracket)'
  return (
    <>
      {c({ top: -1, left: -1 }, { borderTop: b, borderLeft: b })}
      {c({ top: -1, right: -1 }, { borderTop: b, borderRight: b })}
      {c({ bottom: -1, left: -1 }, { borderBottom: b, borderLeft: b })}
      {c({ bottom: -1, right: -1 }, { borderBottom: b, borderRight: b })}
    </>
  )
}

/** Render `code` spans inside prose strings. */
function InlineCode({ text, size = 12.5 }: { text: string; size?: number }) {
  const parts = text.split('`')
  return (
    <>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <span
            key={i}
            style={{
              ...mono(size),
              background: 'color-mix(in srgb, var(--color-ink) 5%, transparent)',
              padding: '1px 5px',
            }}
          >
            {p}
          </span>
        ) : (
          <Fragment key={i}>{p}</Fragment>
        ),
      )}
    </>
  )
}

function CapsLabel({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return <span style={{ ...metaLine, color: 'var(--fg-3)', ...style }}>{children}</span>
}

function UserChips({ chips, width }: { chips: QueueChip[]; width: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, width }}>
      {chips.map((c) => (
        <div
          key={c.qid}
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 8,
            padding: '5px 10px',
            background: 'var(--card-bg)',
            border: '1px solid var(--line-divider)',
            boxShadow: `inset 2px 0 0 ${c.kind === 'cmt' ? 'var(--color-accent)' : 'var(--info)'}`,
          }}
        >
          <span
            style={{
              ...mono(8, 700),
              letterSpacing: '.06em',
              color: c.kind === 'cmt' ? 'var(--color-accent)' : 'var(--info-strong)',
              whiteSpace: 'nowrap',
              paddingTop: 2,
              flexShrink: 0,
            }}
          >
            {c.label}
          </span>
          {c.kind === 'quote' ? (
            <span style={{ flex: 1, minWidth: 0, ...mono(9), lineHeight: 1.55, ...ghost3, ...ellipsis }}>
              {c.body}
            </span>
          ) : (
            <span style={{ flex: 1, minWidth: 0, ...serif(11.5), fontStyle: 'italic', lineHeight: 1.5, color: 'var(--fg-2)' }}>
              {c.body}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

function UserRow({ item }: { item: Extract<LedgerItem, { kind: 'user' }> }) {
  return (
    <div id={`pm-${item.uid}`} style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
      <CapsLabel>{item.who} · {item.t}</CapsLabel>
      {item.chips && item.chips.length > 0 && <UserChips chips={item.chips} width="min(440px,72%)" />}
      <div
        style={{
          background: 'var(--card-bg)',
          border: '1px solid var(--line-divider)',
          borderRadius: 16,
          borderBottomRightRadius: 5,
          boxShadow: 'var(--shadow-soft)',
          padding: '10px 15px',
          ...serif(13.5),
          color: 'var(--fg-1)',
          maxWidth: '72%',
        }}
      >
        {item.text}
      </div>
    </div>
  )
}

function DiffBlock({ uid, index, diff }: { uid: string; index: number; diff: Extract<AgentBlock, { type: 'diff' }>['diff'] }) {
  const key = `diff-${uid}-${index}`
  const open = useConsole((s) => s.open[key] ?? true)
  const { toggleCardOpen } = useConsole((s) => s.actions)
  const toneStyle: Record<string, CSSProperties> = {
    ctx: { color: 'var(--fg-3)' },
    del: { color: 'var(--color-red)', background: 'rgb(163 51 50 / .08)' },
    add: { color: 'var(--color-sage)', background: 'rgb(122 138 91 / .12)' },
  }
  return (
    <div style={{ border: '1px solid var(--line-divider)', margin: '0 0 10px' }}>
      <div
        onClick={() => toggleCardOpen(key, true)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 13px',
          background: 'var(--trk-wash-strong)',
          cursor: 'pointer',
        }}
      >
        <Disclose open={open} style={{ ...mono(10), ...ghost3, width: 11 }} />
        <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>diff</span>
        <span style={{ ...mono(8, 700), letterSpacing: '.14em', ...ghost3 }}>{diff.file}</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...mono(8, 700), letterSpacing: '.1em', color: 'var(--color-sage)' }}>+{diff.adds}</span>
        <span style={{ ...mono(8, 700), letterSpacing: '.1em', color: 'var(--color-red)' }}>−{diff.dels}</span>
      </div>
      <Fold open={open}>
        <pre
          style={{
            margin: 0,
            padding: '12px 14px',
            borderTop: '1px solid var(--line-divider)',
            background: 'var(--trk-wash)',
            ...mono(11.5),
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            color: 'var(--fg-1)',
          }}
        >
          {diff.lines.map((l, i) => (
            <span key={i} style={{ display: 'block', ...toneStyle[l.tone] }}>
              {l.text}
            </span>
          ))}
        </pre>
      </Fold>
    </div>
  )
}

function AgentRow({ item, cardMax }: { item: Extract<LedgerItem, { kind: 'agent' }>; cardMax: string }) {
  return (
    <div id={`pm-${item.uid}`} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <CapsLabel>{item.label}</CapsLabel>
      <div style={{ maxWidth: cardMax, ...serif(14.5), lineHeight: 1.75, color: 'var(--fg-1)', textWrap: 'pretty' }}>
        {item.blocks.map((b, i) => {
          if (b.type === 'p')
            return (
              <p key={i} style={{ margin: i === item.blocks.length - 1 ? 0 : '0 0 10px' }}>
                <InlineCode text={b.text} />
              </p>
            )
          if (b.type === 'ul')
            return (
              <ul key={i} style={{ margin: '0 0 10px', paddingLeft: 20, color: 'var(--fg-2)' }}>
                {b.items.map((li, j) => (
                  <li key={j} style={{ marginBottom: j === b.items.length - 1 ? 0 : 4 }}>
                    <InlineCode text={li} size={12} />
                  </li>
                ))}
              </ul>
            )
          return <DiffBlock key={i} uid={item.uid} index={i} diff={b.diff} />
        })}
      </div>
    </div>
  )
}

function CodeRow({ item, cardMax }: { item: Extract<LedgerItem, { kind: 'code' }>; cardMax: string }) {
  const open = useConsole((s) => s.open[item.uid] ?? false)
  const { toggleCardOpen } = useConsole((s) => s.actions)
  return (
    <div id={`pm-${item.uid}`} style={{ maxWidth: cardMax, border: '1px solid var(--line-divider)' }}>
      <div
        onClick={() => toggleCardOpen(item.uid)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 13px',
          background: 'var(--trk-wash-strong)',
          cursor: 'pointer',
        }}
      >
        <Disclose open={open} style={{ ...mono(10), ...ghost3, width: 11 }} />
        <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>{item.lang}</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...fieldLabel, color: 'var(--fg-3)' }}>{item.lines.length} ln</span>
      </div>
      <Fold open={open}>
        <pre
          style={{
            margin: 0,
            padding: '12px 14px',
            borderTop: '1px solid var(--line-divider)',
            background: 'var(--trk-wash)',
            ...mono(11.5),
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            color: 'var(--fg-1)',
          }}
        >
          {item.lines.map((l, i) => (
            <span key={i} style={{ display: 'block' }}>
              {l}
            </span>
          ))}
        </pre>
      </Fold>
    </div>
  )
}

function ThinkRow({ item, cardMax, i }: { item: Extract<LedgerItem, { kind: 'think' }>; cardMax: string; i?: number }) {
  const open = useConsole((s) => s.open[item.uid] ?? true)
  const { toggleCardOpen } = useConsole((s) => s.actions)
  return (
    <div
      id={`pm-${item.uid}`}
      className="lt-entry"
      style={{ '--i': i ?? 0, maxWidth: cardMax, border: '1px solid var(--line-divider)', background: 'var(--card-bg)' } as CSSProperties}
    >
      <div onClick={() => toggleCardOpen(item.uid, true)} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px', cursor: 'pointer' }}>
        <Disclose open={open} style={{ ...mono(11), ...ghost3, width: 12 }} />
        <Icon name="sparkle" style={{ color: 'var(--fg-2)' }} />
        <span style={{ ...cardKind, color: 'var(--fg-1)' }}>Thinking</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...metaLine, color: 'var(--fg-3)' }}>{item.dur}</span>
      </div>
      <Fold open={open}>
        <div style={{ borderTop: '1px solid var(--line-divider)', padding: '10px 12px' }}>
          <div style={{ borderLeft: '2px solid var(--line-divider)', padding: '6px 14px' }}>
            <p style={{ margin: 0, ...serif(13), lineHeight: 1.65, color: 'var(--fg-2)', fontStyle: 'italic' }}>{item.text}</p>
          </div>
        </div>
      </Fold>
    </div>
  )
}

function PlanRow({ item, cardMax, i }: { item: Extract<LedgerItem, { kind: 'plan' }>; cardMax: string; i?: number }) {
  const open = useConsole((s) => s.open[item.uid] ?? true)
  const { toggleCardOpen } = useConsole((s) => s.actions)
  const activeIdx = item.steps.findIndex((s) => s.status === 'active')
  const counter = `${String(activeIdx + 1).padStart(2, '0')} / ${String(item.steps.length).padStart(2, '0')}`
  return (
    <div
      id={`pm-${item.uid}`}
      className="lt-entry"
      style={{ '--i': i ?? 0, maxWidth: cardMax, border: '1px solid var(--line-divider)', background: 'var(--card-bg)' } as CSSProperties}
    >
      <div onClick={() => toggleCardOpen(item.uid, true)} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 13px', cursor: 'pointer' }}>
        <Disclose open={open} style={{ ...mono(11), ...ghost3, width: 12 }} />
        <Icon name="listChecks" style={{ color: 'var(--fg-1)' }} />
        <span style={{ ...cardKind, color: 'var(--fg-1)' }}>Plan</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...metaLine, color: 'var(--fg-3)' }}>{counter}</span>
      </div>
      <Fold open={open}>
        <div style={{ borderTop: '1px solid var(--line-divider)', padding: '8px 13px' }}>
          {item.steps.map((s, idx) => (
            <div
              key={s.n}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: '8px 2px',
                borderBottom: idx === item.steps.length - 1 ? 'none' : '1px solid var(--line-color)',
              }}
            >
              <span style={{ width: 18, display: 'flex', justifyContent: 'center' }}>
                {s.status === 'done' && <Icon name="check" size={15} style={{ color: 'var(--color-sage)' }} />}
                {s.status === 'active' && (
                  <span style={{ width: 7, height: 7, borderRadius: 9999, background: 'var(--color-accent)', animation: 'trkPulse 1.2s infinite' }} />
                )}
                {s.status === 'idle' && <Icon name="circleDot" size={13} style={{ color: 'var(--fg-3)' }} />}
              </span>
              <span style={{ ...mono(9, 700), letterSpacing: '.1em', color: s.status === 'idle' ? 'var(--fg-3)' : 'var(--fg-2)' }}>{s.n}</span>
              <span
                style={{
                  ...serif(14),
                  flex: 1,
                  color: s.status === 'done' ? 'var(--fg-3)' : 'var(--fg-1)',
                  ...(s.status === 'done'
                    ? { textDecorationLine: 'line-through', textDecorationColor: 'var(--color-border)' }
                    : {}),
                }}
              >
                {s.text}
              </span>
              <span
                style={{
                  ...statusNote,
                  color:
                    s.status === 'done' ? 'var(--color-sage)' : s.status === 'active' ? 'var(--color-accent)' : 'var(--fg-3)',
                }}
              >
                ● {s.status === 'done' ? 'Done' : s.status === 'active' ? 'Active' : 'Idle'}
              </span>
            </div>
          ))}
        </div>
      </Fold>
    </div>
  )
}

const toolIcon: Record<string, IconName> = {
  search: 'search',
  'file-diff': 'fileDiff',
  summon: 'userPlus',
  send: 'send',
  file: 'file',
}

function ToolDetailView({ detail }: { detail: ToolDetail }) {
  const heading = (t: string) => <span style={{ ...statusNote, color: 'var(--fg-3)' }}>{t}</span>
  if (detail.type === 'plain') {
    return (
      <>
        <div style={{ borderTop: '1px solid var(--line-divider)', padding: '10px 14px' }}>
          {heading('Arguments')}
          <pre style={{ margin: '6px 0 0', ...mono(11.5), lineHeight: 1.6, color: 'var(--fg-1)', whiteSpace: 'pre-wrap' }}>{detail.args}</pre>
        </div>
        {detail.res && (
          <div style={{ borderTop: '1px solid var(--line-divider)', padding: '10px 14px' }}>
            {heading('Result')}
            <pre style={{ margin: '6px 0 0', ...mono(11.5), lineHeight: 1.6, color: 'var(--fg-2)', whiteSpace: 'pre-wrap' }}>{detail.res}</pre>
          </div>
        )}
      </>
    )
  }
  if (detail.type === 'kv') {
    return (
      <div style={{ borderTop: '1px solid var(--line-divider)', padding: '10px 14px 11px' }}>
        {heading(detail.heading)}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'max-content 1fr',
            gap: '3px 16px',
            marginTop: 7,
            padding: '8px 11px',
            background: 'var(--surface-sunken)',
            border: '1px solid var(--line-color)',
          }}
        >
          {detail.rows.map(([k, v]) => (
            <Fragment key={k}>
              <span style={{ ...sectionLabel, color: 'var(--fg-3)', paddingTop: 3 }}>{k}</span>
              <span style={{ ...mono(11.5), color: 'var(--fg-1)' }}>{v}</span>
            </Fragment>
          ))}
        </div>
      </div>
    )
  }
  if (detail.type === 'checks') {
    return (
      <>
        <div style={{ borderTop: '1px solid var(--line-divider)', padding: '10px 14px 11px' }}>
          {heading('Arguments')}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'max-content 1fr',
              gap: '3px 16px',
              marginTop: 7,
              padding: '8px 11px',
              background: 'var(--surface-sunken)',
              border: '1px solid var(--line-color)',
            }}
          >
            <span style={{ ...sectionLabel, color: 'var(--fg-3)', paddingTop: 3 }}>repo</span>
            <span style={{ ...mono(11.5), color: 'var(--fg-1)' }}>"maple/staging"</span>
            <span style={{ ...sectionLabel, color: 'var(--fg-3)', paddingTop: 3 }}>ref</span>
            <span style={{ ...mono(11.5), color: 'var(--fg-1)' }}>"deploy/482"</span>
          </div>
        </div>
        <div style={{ padding: '0 14px 12px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            {heading('Result')}
            {heading(detail.summary)}
          </div>
          <div style={{ marginTop: 7, border: '1px solid var(--line-color)' }}>
            {detail.rows.map((r, i) => (
              <div
                key={r.n}
                style={{
                  display: 'flex',
                  alignItems: 'baseline',
                  gap: 10,
                  padding: '5px 10px',
                  borderBottom: i === detail.rows.length - 1 ? 'none' : '1px solid var(--line-color)',
                }}
              >
                <span style={{ ...mono(8), ...ghost3 }}>{r.n}</span>
                <span style={{ flex: 1, ...mono(11), color: 'var(--fg-1)' }}>
                  {r.name}
                  {r.note && <span style={ghost3}> {r.note}</span>}
                </span>
                <span style={{ ...mono(8, 700), letterSpacing: '.14em', color: r.verdict === 'PASS' ? 'var(--color-sage)' : 'var(--color-red)' }}>
                  {r.verdict}
                </span>
                <span style={{ ...mono(10), ...ghost3 }}>{r.dur}</span>
              </div>
            ))}
          </div>
        </div>
      </>
    )
  }
  if (detail.type === 'log') {
    return (
      <div style={{ borderTop: '1px solid var(--line-divider)', padding: '10px 14px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          {heading(detail.heading)}
          {heading(detail.note)}
        </div>
        <pre
          style={{
            margin: '6px 0 0',
            padding: '8px 11px',
            background: 'var(--surface-sunken)',
            border: '1px solid var(--line-color)',
            ...mono(11),
            lineHeight: 1.65,
            color: 'var(--fg-2)',
            whiteSpace: 'pre-wrap',
          }}
        >
          {detail.pre.map((seg, i) => (
            <span
              key={i}
              style={{
                display: 'block',
                ...(seg.tone === 'error' ? { color: 'var(--color-red)', background: 'rgb(163 51 50 / .08)' } : {}),
              }}
            >
              {seg.tone === 'error' ? seg.text : <InlineCode text={seg.text} size={11} />}
            </span>
          ))}
        </pre>
        {detail.spill && (
          <div style={{ marginTop: 9, padding: '8px 10px', border: '1px solid var(--color-gold)', display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ ...sectionLabel, color: 'var(--color-gold)' }}>{detail.spill.label}</span>
            <span style={{ ...mono(10.5), color: 'var(--fg-2)' }}>{detail.spill.text}</span>
          </div>
        )}
      </div>
    )
  }
  // summon
  return (
    <div style={{ borderTop: '1px solid var(--line-divider)', padding: '10px 14px 12px' }}>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'max-content 1fr',
          gap: '4px 16px',
          padding: '9px 11px',
          background: 'color-mix(in srgb, var(--color-terra) 6%, transparent)',
          border: '1px solid color-mix(in srgb, var(--color-terra) 35%, transparent)',
        }}
      >
        {detail.rows.map(([k, v]) => (
          <Fragment key={k}>
            <span style={{ ...sectionLabel, color: 'var(--color-terra)', paddingTop: 3 }}>{k}</span>
            <span style={{ ...mono(11.5), color: 'var(--fg-1)' }}>{v}</span>
          </Fragment>
        ))}
      </div>
    </div>
  )
}

function ToolRow({ item, cardMax, i }: { item: Extract<LedgerItem, { kind: 'tool' }>; cardMax: string; i?: number }) {
  const open = useConsole((s) => s.open[item.uid] ?? item.defaultOpen ?? false)
  const { toggleCardOpen } = useConsole((s) => s.actions)
  const summon = item.icon === 'summon'
  return (
    <div
      id={`pm-${item.uid}`}
      className="lt-entry"
      style={
        {
          '--i': i ?? 0,
          maxWidth: cardMax,
          position: 'relative',
          background: 'var(--card-bg)',
          border: summon ? '1px solid var(--color-terra)' : '1px solid var(--line-divider)',
        } as CSSProperties
      }
    >
      {!summon && <Brackets />}
      <div
        onClick={() => toggleCardOpen(item.uid, item.defaultOpen ?? false)}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 13px', background: 'var(--card-bg)', cursor: 'pointer' }}
      >
        <Disclose open={open} style={{ ...mono(11), ...ghost3, width: 12 }} />
        <Icon name={toolIcon[item.icon] ?? 'search'} style={{ color: summon ? 'var(--color-terra)' : 'var(--fg-1)' }} />
        <span style={{ ...cardKind, color: 'var(--fg-1)' }}>Tool</span>
        <span style={{ ...mono(12, 700), color: 'var(--color-accent)' }}>{item.name}</span>
        {item.badge && (
          <span
            style={{
              ...sectionLabel,
              color: item.badge.tone === 'gold' ? 'var(--color-gold)' : 'var(--color-terra)',
              border: `1px solid ${item.badge.tone === 'gold' ? 'var(--color-gold)' : 'var(--color-terra)'}`,
              padding: '2px 6px',
            }}
          >
            {item.badge.label}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {item.dur && <span style={{ ...mono(8), ...ghost3 }}>{item.dur}</span>}
        {item.status === 'run' ? (
          <>
            <span style={{ width: 5, height: 5, borderRadius: 9999, background: 'var(--color-accent)', animation: 'trkPulse 1.2s infinite' }} />
            <span style={{ ...statusNote, color: 'var(--color-accent)' }}>run</span>
          </>
        ) : (
          <>
            <span style={{ width: 5, height: 5, borderRadius: 9999, background: 'var(--color-sage)' }} />
            <span style={{ ...statusNote, color: 'var(--color-sage)' }}>done</span>
          </>
        )}
      </div>
      <Fold open={open}>
        <div>
          <ToolDetailView detail={item.detail} />
        </div>
      </Fold>
    </div>
  )
}

function FileRow({ item, cardMax }: { item: Extract<LedgerItem, { kind: 'file' }> ; cardMax: string }) {
  const pvOpen = useConsole((s) => s.pvOpen)
  const activeFile = useConsole((s) => s.activeFile)
  const hasDocs = useConsole((s) => !!(s.docs ?? s.detail?.docs))
  const { openFile } = useConsole((s) => s.actions)
  const isOpen = pvOpen && activeFile === item.name
  return (
    <div
      id={`pm-${item.uid}`}
      onClick={() => hasDocs && openFile(item.name)}
      className="lt-fade-in hov-shadow"
      style={{
        maxWidth: cardMax,
        position: 'relative',
        background: 'var(--card-bg)',
        border: `1px solid ${isOpen ? 'var(--color-accent)' : 'var(--line-divider)'}`,
        cursor: hasDocs ? 'pointer' : 'default',
      }}
    >
      <Brackets />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px' }}>
        <Icon name="file" size={15} style={{ color: 'var(--color-accent)', flexShrink: 0 }} />
        <span style={{ ...mono(11.5, 700), color: 'var(--color-accent)', ...ellipsis, minWidth: 0 }}>{item.name}</span>
        <span style={{ ...mono(7, 700), letterSpacing: '.1em', color: 'var(--fg-2)', border: '1px solid var(--line-divider)', padding: '1px 5px', flexShrink: 0 }}>
          {item.rev}
        </span>
        <span style={{ flex: 1 }} />
        {isOpen ? (
          <span style={{ ...microMeta, color: 'var(--color-accent)', flexShrink: 0 }}>● open</span>
        ) : (
          <span style={{ ...mono(9, 700), color: 'var(--color-accent)', flexShrink: 0 }}>open →</span>
        )}
      </div>
      <div style={{ padding: '0 14px 9px', ...mono(8.5), ...ghost3 }}>{item.note}</div>
    </div>
  )
}

function SubRow({ item, cardMax }: { item: Extract<LedgerItem, { kind: 'sub' }>; cardMax: string }) {
  return (
    <div
      id={`pm-${item.uid}`}
      className="lt-fade-in"
      style={{
        maxWidth: cardMax,
        marginLeft: 24,
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '5px 11px',
        boxSizing: 'border-box',
        border: '1px dashed color-mix(in srgb, var(--color-teal) 45%, transparent)',
        background: 'color-mix(in srgb, var(--color-teal) 4%, transparent)',
      }}
    >
      <span style={{ ...mono(9), ...ghost3, flexShrink: 0 }}>↳</span>
      <span style={{ ...mono(7, 700), letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--color-teal)', border: '1px solid var(--color-teal)', padding: '0 3px', flexShrink: 0 }}>
        subagent
      </span>
      <span style={{ ...mono(8.5, 700), color: 'var(--color-accent)', flexShrink: 0 }}>{item.id}</span>
      <span style={{ ...mono(8.5), color: 'var(--fg-2)', whiteSpace: 'nowrap', flexShrink: 0 }}>{item.route}</span>
      <span style={{ flex: 1, minWidth: 0, ...mono(8), ...ghost3, ...ellipsis, textAlign: 'right' }}>{item.note}</span>
    </div>
  )
}

function ApprovalRow({ item, cardMax, i }: { item: Extract<LedgerItem, { kind: 'approval' }>; cardMax: string; i?: number }) {
  const { resolveApproval } = useConsole((s) => s.actions)
  if (item.state !== 'waiting') {
    const approved = item.state === 'approved'
    const color = approved ? 'var(--color-sage)' : 'var(--fg-3)'
    return (
      <div id={`pm-${item.uid}`} className="lt-entry" style={{ '--i': i ?? 0, maxWidth: cardMax } as CSSProperties}>
        <div
          className="lt-fade-in"
          style={{
            border: '1px solid var(--line-divider)',
            borderLeft: `3px solid ${color}`,
            background: 'var(--card-bg)',
            display: 'flex',
            alignItems: 'baseline',
            gap: 9,
            padding: '7px 12px',
          }}
        >
          <span style={{ ...mono(10, 700), color, lineHeight: 1 }}>{approved ? '✓' : '○'}</span>
          <span style={{ ...label(8.5, '.12em'), color: 'var(--fg-1)' }}>
            {approved ? 'approved by kalynnka — gh.create_issue dispatched' : 'dismissed by kalynnka — action dropped, run resumes'}
          </span>
          <span style={{ flex: 1, minWidth: 0, ...serif(12), fontStyle: 'italic', ...ghost3, ...ellipsis }}>
            {item.title.toLowerCase()}
          </span>
          <span style={{ ...mono(8), ...ghost3, flexShrink: 0 }}>{item.resolvedT}</span>
        </div>
      </div>
    )
  }
  return (
    <div id={`pm-${item.uid}`} className="lt-entry" style={{ '--i': i ?? 0, maxWidth: cardMax } as CSSProperties}>
      <div style={{ border: '1px solid var(--line-divider)', borderLeft: '3px solid var(--color-gold)', background: 'var(--card-bg)', padding: '11px 15px 11px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          <div style={{ ...display(14, 600), lineHeight: 1.2, letterSpacing: '-.01em', textTransform: 'uppercase', color: 'var(--fg-1)' }}>{item.title}</div>
          <span style={{ flex: 1 }} />
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginTop: 1, ...microSection, color: 'var(--color-gold)', whiteSpace: 'nowrap' }}>
            <i style={{ width: 5, height: 5, borderRadius: 9999, background: 'var(--color-gold)', animation: 'trkPulse 1.2s infinite' }} />
            Write · waiting · kalynnka
          </span>
        </div>
        <p style={{ margin: '5px 0 0', ...serif(12.5), lineHeight: 1.65, color: 'var(--fg-2)' }}>{item.desc}</p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 10, flexWrap: 'wrap' }}>
          <span
            onClick={() => resolveApproval(item.uid, 'approved')}
            className="hov-panel"
            style={{ ...label(8.5, '.12em'), color: 'var(--trk-on-fill)', background: 'var(--color-accent)', padding: '4px 10px', cursor: 'pointer' }}
          >
            Approve
          </span>
          <span className="hov-accent-fill" style={{ ...label(8.5, '.12em'), color: 'var(--on-panel)', background: 'var(--panel)', padding: '4px 10px', cursor: 'pointer' }}>
            Read
          </span>
          <span
            onClick={() => resolveApproval(item.uid, 'dismissed')}
            className="hov-panel-full"
            style={{ ...label(8.5, '.12em'), color: 'var(--fg-1)', border: '1px solid var(--line-divider)', padding: '3px 9px', cursor: 'pointer' }}
          >
            Dismiss
          </span>
        </div>
        <div style={{ marginTop: 9, ...microMeta, color: 'var(--fg-3)' }}>{item.meta}</div>
      </div>
    </div>
  )
}

function AskRow({ item, cardMax, i }: { item: Extract<LedgerItem, { kind: 'ask' }>; cardMax: string; i?: number }) {
  const { answerAsk } = useConsole((s) => s.actions)
  const [bodyMore, setBodyMore] = useState(false)
  const [rowOpen, setRowOpen] = useState(0)
  const [otherOpen, setOtherOpen] = useState(false)
  const [otherText, setOtherText] = useState('')
  const [ansOpen, setAnsOpen] = useState(false)

  if (item.state === 'answered') {
    return (
      <div id={`pm-${item.uid}`} className="lt-entry" style={{ '--i': i ?? 0, maxWidth: cardMax } as CSSProperties}>
        {!ansOpen ? (
          <div
            onClick={() => setAnsOpen(true)}
            className="lt-fade-in hov-card-hover"
            style={{
              border: '1px solid var(--line-divider)',
              borderLeft: '3px solid var(--color-sage)',
              background: 'var(--card-bg)',
              display: 'flex',
              alignItems: 'baseline',
              gap: 9,
              padding: '8px 12px',
              cursor: 'pointer',
            }}
          >
            <span style={{ ...label(9, '.12em'), color: 'var(--color-sage)', flexShrink: 0 }}>✓ answered · kalynnka</span>
            <span style={{ flex: 1, minWidth: 0, ...serif(12.5), fontStyle: 'italic', color: 'var(--fg-1)', ...ellipsis }}>“{item.answer}”</span>
            <span style={{ ...mono(10), ...ghost3, flexShrink: 0 }}>▸</span>
            <span style={{ ...mono(8), ...ghost3, flexShrink: 0 }}>{item.resolvedT}</span>
          </div>
        ) : (
          <div className="lt-fade-in" style={{ border: '1px solid var(--line-divider)', borderLeft: '3px solid var(--color-sage)', background: 'var(--card-bg)', padding: '9px 12px 10px' }}>
            <div onClick={() => setAnsOpen(false)} style={{ display: 'flex', alignItems: 'baseline', gap: 9, cursor: 'pointer' }}>
              <span style={{ ...label(9, '.12em'), color: 'var(--color-sage)' }}>✓ answered · kalynnka</span>
              <span style={{ flex: 1 }} />
              <span style={{ ...mono(10), ...ghost3 }}>▾</span>
              <span style={{ ...mono(8), ...ghost3 }}>{item.resolvedT}</span>
            </div>
            <p style={{ margin: '6px 0 0', ...serif(13), lineHeight: 1.7, fontStyle: 'italic', color: 'var(--fg-1)' }}>“{item.answer}”</p>
            <div style={{ marginTop: 8, ...microMeta, color: 'var(--fg-3)' }}>{item.via} · RUN_0521 resumed</div>
          </div>
        )}
      </div>
    )
  }

  const chooseOption = (o: AskOption, idx: number) => answerAsk(item.uid, o.label, `picked option ${idx + 1}`)
  const sendOther = () => {
    const t = otherText.trim()
    if (!t) return
    answerAsk(item.uid, t, `free-text answer · ${t.length} chars`)
  }

  return (
    <div id={`pm-${item.uid}`} className="lt-entry" style={{ '--i': i ?? 0, maxWidth: cardMax } as CSSProperties}>
      <div style={{ border: '1px solid var(--line-divider)', borderLeft: '3px solid var(--color-gold)', background: 'var(--card-bg)', paddingTop: 12 }}>
        <div style={{ padding: '0 15px 0 16px' }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
            <div style={{ ...display(16, 600), lineHeight: 1.2, letterSpacing: '-.01em', textTransform: 'uppercase', color: 'var(--fg-1)' }}>{item.title}</div>
            <span style={{ flex: 1 }} />
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginTop: 2, ...sectionLabel, color: 'var(--color-gold)', whiteSpace: 'nowrap' }}>
              <i style={{ width: 6, height: 6, borderRadius: 9999, background: 'var(--color-gold)', animation: 'trkPulse 1.2s infinite' }} />
              Ask · waiting · kalynnka
            </span>
          </div>
          <div style={{ position: 'relative', overflow: 'hidden', maxHeight: bodyMore ? 'none' : 46, marginTop: 6 }}>
            <p style={{ margin: 0, ...serif(13), lineHeight: 1.7, color: 'var(--fg-2)' }}>{item.body}</p>
            {!bodyMore && (
              <span style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: 20, background: 'linear-gradient(transparent, var(--card-bg))', pointerEvents: 'none' }} />
            )}
          </div>
          <div style={{ display: 'flex', marginTop: 2 }}>
            <span style={{ flex: 1 }} />
            <span onClick={() => setBodyMore((v) => !v)} className="hov-accent" style={{ ...label(8, '.14em'), color: 'var(--fg-3)', cursor: 'pointer' }}>
              {bodyMore ? '▾ show less' : '▸ show all'}
            </span>
          </div>
        </div>
        <div style={{ marginTop: 9, borderTop: '1px solid var(--line-divider)' }}>
          {item.options.map((o, idx) => {
            const openRow = rowOpen === idx + 1
            return (
              <div
                key={o.label}
                onClick={() => {
                  setRowOpen(openRow ? 0 : idx + 1)
                  setOtherOpen(false)
                }}
                className="hov-wash"
                style={{ borderBottom: '1px solid var(--line-divider)', background: openRow ? 'var(--hover)' : 'transparent', cursor: 'pointer' }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 15px 8px 16px' }}>
                  <span style={{ ...mono(8, 700), color: 'var(--fg-2)', border: '1px solid var(--line-divider)', padding: '1px 5px', flexShrink: 0 }}>{idx + 1}</span>
                  <span style={{ fontSize: 12.5, color: 'var(--fg-1)', flexShrink: 0 }}>{o.label}</span>
                  <span style={{ flex: 1, minWidth: 0, ...mono(8), ...ghost3, ...ellipsis }}>{o.sum}</span>
                  <span style={{ ...mono(10), ...ghost3, flexShrink: 0 }}>{openRow ? '▾' : '▸'}</span>
                </div>
                {openRow && (
                  <div className="lt-fade-in" style={{ padding: '0 15px 10px 16px' }}>
                    <p style={{ margin: '0 0 0 25px', ...serif(12), lineHeight: 1.65, color: 'var(--fg-2)' }}>{o.desc}</p>
                    <div style={{ display: 'flex', marginTop: 8 }}>
                      <span style={{ width: 25 }} />
                      <span
                        onClick={(e) => {
                          e.stopPropagation()
                          chooseOption(o, idx)
                        }}
                        className="hov-panel"
                        style={{ ...label(8.5, '.12em'), color: 'var(--trk-on-fill)', background: 'var(--color-accent)', padding: '4px 10px', cursor: 'pointer' }}
                      >
                        Choose {idx + 1}
                      </span>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
          <div style={{ borderBottom: '1px solid var(--line-divider)' }}>
            <div
              onClick={() => {
                setOtherOpen((v) => !v)
                setRowOpen(0)
              }}
              className="hov-wash"
              style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 15px 8px 16px', cursor: 'pointer' }}
            >
              <span style={{ ...mono(8, 700), color: 'var(--fg-2)', border: '1px solid var(--line-divider)', padding: '1px 5px', flexShrink: 0 }}>0</span>
              <span style={{ fontSize: 12.5, fontStyle: 'italic', color: 'var(--fg-3)' }}>Other — type instructions…</span>
              <span style={{ flex: 1 }} />
              <span style={{ ...mono(10), ...ghost3, flexShrink: 0 }}>{otherOpen ? '▾' : '▸'}</span>
            </div>
            {otherOpen && (
              <div className="lt-fade-in" style={{ padding: '0 15px 10px 16px' }}>
                <textarea
                  rows={2}
                  placeholder="type your instruction — it sends as the answer"
                  value={otherText}
                  onChange={(e) => setOtherText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      sendOther()
                    }
                    if (e.key === 'Escape') setOtherOpen(false)
                  }}
                  style={{
                    boxSizing: 'border-box',
                    width: '100%',
                    border: '1px solid var(--line-divider)',
                    background: 'var(--surface-sunken)',
                    padding: '8px 10px',
                    ...serif(13),
                    lineHeight: 1.6,
                    color: 'var(--fg-1)',
                    resize: 'none',
                    outline: 'none',
                  }}
                />
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
                  <span style={{ ...microMeta, color: 'var(--fg-3)' }}>↩ send · esc back to options</span>
                  <span style={{ flex: 1 }} />
                  <span onClick={sendOther} className="hov-panel" style={{ ...label(8.5, '.12em'), color: 'var(--trk-on-fill)', background: 'var(--color-accent)', padding: '4px 10px', cursor: 'pointer' }}>
                    Send
                  </span>
                </div>
              </div>
            )}
          </div>
        </div>
        <div style={{ padding: '8px 15px 10px 16px', ...microMeta, color: 'var(--fg-3)' }}>{item.meta}</div>
      </div>
    </div>
  )
}

function OAuthRow({ item, cardMax, i }: { item: Extract<LedgerItem, { kind: 'oauth' }>; cardMax: string; i?: number }) {
  const device = item.code !== undefined
  return (
    <div id={`pm-${item.uid}`} className="lt-entry" style={{ '--i': i ?? 0, maxWidth: cardMax } as CSSProperties}>
      <div style={{ border: '1px solid var(--line-divider)', borderLeft: '3px solid var(--color-gold)', background: 'var(--card-bg)', padding: '11px 15px 12px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          <div style={{ ...display(14, 600), lineHeight: 1.2, letterSpacing: '-.01em', textTransform: 'uppercase', color: 'var(--fg-1)' }}>
            Connect {item.label}
          </div>
          <span style={{ flex: 1 }} />
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, marginTop: 1, ...microSection, color: 'var(--color-gold)', whiteSpace: 'nowrap' }}>
            <i style={{ width: 5, height: 5, borderRadius: 9999, background: 'var(--color-gold)', animation: 'trkPulse 1.2s infinite' }} />
            OAuth · waiting
          </span>
        </div>
        <p style={{ margin: '5px 0 0', ...serif(12.5), lineHeight: 1.65, color: 'var(--fg-2)' }}>
          {device
            ? 'Open the authorization page and enter this code — the run resumes once the provider confirms.'
            : 'Open the authorization page and approve — the provider finishes the connection on its own.'}
        </p>
        {device && (
          <div
            onClick={() => void navigator.clipboard.writeText(item.code ?? '')}
            title="click to copy"
            style={{
              display: 'inline-block',
              marginTop: 10,
              padding: '7px 14px',
              border: '1px solid var(--color-gold)',
              ...mono(16, 700),
              letterSpacing: '.18em',
              color: 'var(--fg-1)',
              cursor: 'copy',
              userSelect: 'all',
            }}
          >
            {item.code}
          </div>
        )}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
          <a
            href={item.uri}
            target="_blank"
            rel="noreferrer"
            className="hov-panel"
            style={{ ...label(8.5, '.12em'), color: 'var(--trk-on-fill)', background: 'var(--color-accent)', padding: '4px 10px', textDecoration: 'none' }}
          >
            Authorize ↗
          </a>
          <span style={{ ...mono(9), color: 'var(--info-strong)', wordBreak: 'break-all', minWidth: 0 }}>{item.uri}</span>
        </div>
        <div style={{ marginTop: 9, ...microMeta, color: 'var(--fg-3)' }}>
          {item.connectorId} · {device ? 'oauth device flow · come back after' : 'oauth authorization'}
        </div>
      </div>
    </div>
  )
}

function Rule({ children }: { children?: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '2px 0' }}>
      <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
      {children}
      <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
    </div>
  )
}

/** Dispatch a ledger item to its card. `i` staggers entry animations. */
export function LedgerRow({ item, cardMax, i }: { item: LedgerItem; cardMax: string; i?: number }) {
  switch (item.kind) {
    case 'divider':
      return (
        <Rule>
          <span style={{ ...statusNote, color: 'var(--fg-3)' }}>{item.label}</span>
        </Rule>
      )
    case 'system':
      return (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <i style={{ width: 6, height: 6, borderRadius: 9999, background: 'var(--fg-3)', flexShrink: 0 }} />
          <CapsLabel>{item.text}</CapsLabel>
          <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
        </div>
      )
    case 'session-open':
      if (item.tone === 'summon') {
        return (
          <div id={item.uid} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '4px 0' }}>
            <span style={{ flex: 1, borderTop: '2px solid var(--color-terra)', opacity: 0.65 }} />
            <span style={{ ...label(9, '.16em'), color: 'var(--color-terra)' }}>{item.text}</span>
            <span style={{ flex: 1, borderTop: '2px solid var(--color-terra)', opacity: 0.65 }} />
          </div>
        )
      }
      return (
        <div id={item.uid} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <i style={{ width: 6, height: 6, borderRadius: 9999, background: 'var(--color-accent)', flexShrink: 0 }} />
          <CapsLabel>{item.text}</CapsLabel>
          <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
        </div>
      )
    case 'user':
      return <UserRow item={item} />
    case 'agent':
      return <AgentRow item={item} cardMax={cardMax} />
    case 'code':
      return <CodeRow item={item} cardMax={cardMax} />
    case 'think':
      return <ThinkRow item={item} cardMax={cardMax} i={i} />
    case 'plan':
      return <PlanRow item={item} cardMax={cardMax} i={i} />
    case 'tool':
      return <ToolRow item={item} cardMax={cardMax} i={i} />
    case 'file':
      return <FileRow item={item} cardMax={cardMax} />
    case 'sub':
      return <SubRow item={item} cardMax={cardMax} />
    case 'approval':
      return <ApprovalRow item={item} cardMax={cardMax} i={i} />
    case 'ask':
      return <AskRow item={item} cardMax={cardMax} i={i} />
    case 'oauth':
      return <OAuthRow item={item} cardMax={cardMax} i={i} />
    case 'dots':
      return (
        <div className="lt-fade-in" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '2px 0' }}>
          <span className="lt-dots">
            <i />
            <i />
            <i />
          </span>
          <CapsLabel>{item.label}</CapsLabel>
        </div>
      )
    case 'stream':
      return (
        <div id={`pm-${item.uid}`} className="lt-fade-in" style={{ maxWidth: cardMax, ...serif(14.5), lineHeight: 1.75, color: 'var(--fg-1)' }}>
          {item.text}
          {item.streaming && <span className="lt-caret" style={{ verticalAlign: 'text-bottom', marginLeft: 3 }} />}
        </div>
      )
    case 'end':
      return (
        <div id={`pm-${item.uid}`} style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 2 }}>
          <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
          <span style={{ flex: '0 1 auto', minWidth: 0, ...ellipsis, ...label(8, '.2em'), color: 'var(--fg-3)' }}>{item.label}</span>
          <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
        </div>
      )
    case 'notice':
      return (
        <div className="lt-fade-in" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '2px 0' }}>
          <span style={{ flex: 1, borderTop: '1px dashed var(--color-teal)', opacity: 0.6 }} />
          <span style={{ ...label(9, '.14em'), color: 'var(--color-teal)' }}>⇄ {item.text}</span>
          <span style={{ flex: 1, borderTop: '1px dashed var(--color-teal)', opacity: 0.6 }} />
        </div>
      )
  }
}
