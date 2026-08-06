import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { useConsole } from '@/state/console'
import { useRailDrag } from '@/lib/useRailDrag'
import { display, ellipsis, label, mono, serif } from '@/components/text'
import { Disclose, Fold } from '@/components/Fold'
import { TriStripe } from '@/components/TriStripe'
import type { DocLine, ReviewDoc } from '@/lib/api/types'

/** Per-kind typography for one dossier line — a faithful port of the comp. */
function docLineBody(l: DocLine, codeFirst: boolean, codeLast: boolean, runLn: number): ReactNode {
  switch (l.k) {
    case 'h1':
      return (
        <span
          style={{
            display: 'block',
            ...display(19),
            lineHeight: 1.25,
            textTransform: 'uppercase',
            letterSpacing: '-.01em',
            color: 'var(--fg-1)',
            padding: '12px 0 4px',
          }}
        >
          {l.t}
        </span>
      )
    case 'h2':
      return (
        <span
          style={{
            display: 'block',
            ...display(13.5, 600),
            lineHeight: 1.3,
            textTransform: 'uppercase',
            letterSpacing: '.02em',
            color: 'var(--fg-1)',
            padding: '11px 0 3px',
          }}
        >
          {l.t}
        </span>
      )
    case 'p':
      return (
        <span
          style={{
            display: 'block',
            ...serif(13),
            lineHeight: 1.7,
            color: 'var(--fg-2)',
            padding: '2px 0',
            textWrap: 'pretty',
          }}
        >
          {l.t}
        </span>
      )
    case 'li':
      return (
        <span
          style={{
            display: 'block',
            position: 'relative',
            ...serif(13),
            lineHeight: 1.65,
            color: 'var(--fg-2)',
            padding: '1.5px 0 1.5px 15px',
          }}
        >
          <i
            style={{
              position: 'absolute',
              left: 1,
              top: 5,
              fontStyle: 'normal',
              ...mono(8),
              color: 'var(--fg-3)',
            }}
          >
            ▸
          </i>
          {l.t}
        </span>
      )
    case 'num':
      return (
        <span
          style={{
            display: 'block',
            ...serif(13),
            lineHeight: 1.65,
            color: 'var(--fg-2)',
            padding: '1.5px 0 1.5px 15px',
          }}
        >
          {l.t}
        </span>
      )
    case 'code':
      return (
        <span
          style={{
            display: 'block',
            margin: `${codeFirst ? '5px' : '0'} 0 ${codeLast ? '5px' : '0'}`,
            borderLeft: '1px solid var(--line-divider)',
            borderRight: '1px solid var(--line-divider)',
            borderTop: codeFirst ? '1px solid var(--line-divider)' : 'none',
            borderBottom: codeLast ? '1px solid var(--line-divider)' : 'none',
          }}
        >
          {codeFirst && (
            <span
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '4px 11px',
                background: 'color-mix(in srgb, var(--color-ink) 6%, transparent)',
                borderBottom: '1px solid var(--line-divider)',
              }}
            >
              <span style={{ ...label(8), color: 'var(--fg-2)' }}>{l.lang ?? 'code'}</span>
              <span style={{ flex: 1 }} />
              <span style={{ ...label(8), color: 'var(--fg-3)' }}>{runLn} ln</span>
            </span>
          )}
          <span
            style={{
              display: 'block',
              ...mono(11),
              lineHeight: 1.7,
              color: 'var(--fg-1)',
              background: 'color-mix(in srgb, var(--color-ink) 3%, transparent)',
              padding: '2px 13px',
            }}
          >
            {l.t}
          </span>
        </span>
      )
    case 'quote':
      return (
        <span
          style={{
            display: 'block',
            ...serif(12.5),
            fontStyle: 'italic',
            lineHeight: 1.65,
            color: 'var(--fg-2)',
            borderLeft: '2px solid var(--color-gold)',
            padding: '2px 0 2px 11px',
            margin: '2px 0',
          }}
        >
          {l.t}
        </span>
      )
    case 'meta':
      return (
        <span
          style={{
            display: 'block',
            ...mono(8.5),
            letterSpacing: '.08em',
            color: 'var(--fg-3)',
            padding: '2px 0',
          }}
        >
          {l.t}
        </span>
      )
    case 'blank':
      return <span style={{ display: 'block', height: 9 }} />
    case 'hr':
      return <span style={{ display: 'block', borderTop: '1px solid var(--line-color)', margin: '10px 0' }} />
  }
}

/** The file-review dossier rail — tabs, diff meta, marked-up doc, note cards. */
export function ReviewPanel() {
  const pvOpen = useConsole((s) => s.pvOpen)
  const tabs = useConsole((s) => s.tabs)
  const activeFile = useConsole((s) => s.activeFile)
  const view = useConsole((s) => s.view)
  const storeDocs = useConsole((s) => s.docs)
  const storeCmts = useConsole((s) => s.cmts)
  const cmtOpen = useConsole((s) => s.cmtOpen)
  const sel = useConsole((s) => s.sel)
  const selecting = useConsole((s) => s.selecting)
  const draft = useConsole((s) => s.draft)
  const detail = useConsole((s) => s.detail)
  const pvW = useConsole((s) => s.widths.pv)
  const railDrag = useConsole((s) => s.railDrag)
  const {
    togglePv,
    openFile,
    closeTab,
    setView,
    lineDown,
    lineOver,
    endSelection,
    plusLine,
    clearSelection,
    docNums,
    docRange,
    setDraftText,
    cancelDraft,
    queueComment,
    removeQueued,
    toggleComment,
    nextCommentId,
  } = useConsole((s) => s.actions)
  const dragPv = useRailDrag('pv', 'mdv-panel', 390, 940)

  useEffect(() => {
    const onUp = () => endSelection()
    window.addEventListener('mouseup', onUp)
    return () => window.removeEventListener('mouseup', onUp)
  }, [endSelection])

  useEffect(() => {
    if (!sel && !draft) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') clearSelection()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sel, draft, clearSelection])

  const widthCss = pvW ? `${pvW}px` : 'clamp(440px,40vw,720px)'
  const asideStyle = {
    width: widthCss,
    flexShrink: 0,
    background: 'var(--card-bg)',
    boxShadow: 'inset -1px 0 0 var(--line-divider)',
    position: 'relative',
  } as const

  const docs: Record<string, ReviewDoc | undefined> = storeDocs ?? detail?.docs ?? {}
  const cmts = storeCmts ?? detail?.comments ?? []
  const tabList = tabs ?? detail?.artifacts?.map((a) => a.name) ?? []
  const doc = docs[activeFile] ?? docs['REGISTRY_CUTOVER.md']

  if (!doc) {
    return <aside id="mdv-panel" className="trk-rail" data-folded="" style={asideStyle} />
  }

  const clean = view === 'clean'
  const selRange = sel ? { lo: Math.min(sel.a, sel.b), hi: Math.max(sel.a, sel.b) } : null
  const ns = docNums(doc)
  let adds = 0
  let dels = 0
  for (const l of doc.lines) {
    if (l.mark === 'add') adds++
    if (l.mark === 'del') dels++
  }
  const fileCmts = cmts.filter((c) => c.file === activeFile)
  const outd = fileCmts.filter((c) => c.status === 'outdated').length
  const docLn = doc.lines.filter((l) => l.mark !== 'del').length

  const rows: ReactNode[] = []
  doc.lines.forEach((l, i) => {
    const isDel = l.mark === 'del'
    const isAdd = l.mark === 'add'
    if (!(isDel && clean)) {
      const inSel = selRange !== null && i >= selRange.lo && i <= selRange.hi
      const isCode = l.k === 'code'
      const prevCode = isCode && i > 0 && doc.lines[i - 1].k === 'code'
      const nextCode = isCode && i + 1 < doc.lines.length && doc.lines[i + 1].k === 'code'
      let runLn = 0
      if (isCode && !prevCode) {
        let j = i
        while (j < doc.lines.length && doc.lines[j].k === 'code') {
          runLn++
          j++
        }
      }
      rows.push(
        <div
          key={l.id}
          className="mdv-line"
          id={`ln-${l.id}`}
          onMouseDown={(e) => {
            e.preventDefault()
            lineDown(i)
          }}
          onMouseEnter={() => lineOver(i)}
          style={{
            position: 'relative',
            display: 'flex',
            alignItems: 'baseline',
            background: inSel
              ? 'color-mix(in srgb, var(--info) 13%, transparent)'
              : isDel
                ? 'color-mix(in srgb, var(--color-red) 7%, transparent)'
                : isAdd && !clean
                  ? 'color-mix(in srgb, var(--color-teal) 9%, transparent)'
                  : 'transparent',
            boxShadow: `inset 2px 0 0 ${
              inSel
                ? 'var(--info)'
                : isDel
                  ? 'color-mix(in srgb, var(--color-red) 50%, transparent)'
                  : isAdd && !clean
                    ? 'color-mix(in srgb, var(--color-teal) 60%, transparent)'
                    : 'transparent'
            }`,
            paddingRight: 8,
          }}
        >
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              width: 52,
              flexShrink: 0,
              justifyContent: 'flex-end',
              paddingRight: 7,
              boxSizing: 'border-box',
            }}
          >
            <span
              className="mdv-plus"
              onMouseDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation()
                plusLine(i)
              }}
              title="Comment on this line"
              style={{
                width: 13,
                height: 13,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: 'var(--color-accent)',
                color: '#F6F1E5',
                ...mono(10, 700),
                lineHeight: 1,
                cursor: 'pointer',
              }}
            >
              +
            </span>
            <span style={{ ...mono(8.5), color: inSel ? 'var(--info-strong)' : 'var(--fg-3)' }}>
              {isDel ? '·' : ns[i]}
            </span>
          </span>
          <span
            style={{
              width: 12,
              flexShrink: 0,
              ...mono(9, 700),
              color: isDel ? 'var(--color-red)' : 'var(--color-teal)',
            }}
          >
            {isDel ? '−' : isAdd && !clean ? '+' : ''}
          </span>
          <span
            style={{
              flex: 1,
              minWidth: 0,
              opacity: isDel ? 0.55 : 1,
              textDecorationLine: isDel ? 'line-through' : 'none',
              textDecorationColor: 'color-mix(in srgb, var(--color-red) 55%, transparent)',
              textDecorationThickness: 1,
            }}
          >
            {docLineBody(l, isCode && !prevCode, isCode && !nextCode, runLn)}
          </span>
        </div>,
      )
    }
    if (selRange && i === selRange.hi && !selecting && draft) {
      rows.push(
        <div
          key="cmt-draft"
          className="lt-fade-in"
          style={{
            margin: '6px 0 10px 52px',
            maxWidth: 500,
            border: '1px solid var(--color-ink)',
            background: 'var(--card-bg)',
            boxShadow: 'var(--shadow-card)',
            position: 'relative',
            zIndex: 6,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '6px 10px',
              borderBottom: '1px solid var(--line-divider)',
            }}
          >
            <span style={{ ...mono(8.5, 700), letterSpacing: '.1em', color: 'var(--color-accent)' }}>
              {nextCommentId()}
            </span>
            <span style={{ ...mono(8.5, 700), color: 'var(--info-strong)' }}>
              {docRange(doc, selRange.lo, selRange.hi)}
            </span>
            <span style={{ flex: 1 }} />
            <span
              style={{
                ...mono(7.5),
                letterSpacing: '.14em',
                textTransform: 'uppercase',
                color: 'var(--fg-3)',
              }}
            >
              rides with next send
            </span>
          </div>
          <textarea
            autoFocus
            value={draft.text}
            onChange={(e) => setDraftText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                queueComment()
              }
              if (e.key === 'Escape') cancelDraft()
            }}
            rows={2}
            placeholder="what should change here…"
            style={{
              display: 'block',
              width: '100%',
              boxSizing: 'border-box',
              border: 'none',
              outline: 'none',
              resize: 'none',
              background: 'var(--surface-sunken)',
              padding: '8px 10px',
              ...mono(11),
              lineHeight: 1.6,
              color: 'var(--fg-1)',
            }}
          />
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '6px 10px',
              borderTop: '1px solid var(--line-divider)',
            }}
          >
            <span
              onClick={queueComment}
              className="hov-accent-ghost"
              style={{
                ...label(8, '.12em'),
                color: '#F6F1E5',
                background: 'var(--color-accent)',
                border: '1px solid var(--color-accent)',
                padding: '3px 9px',
                cursor: 'pointer',
              }}
            >
              Queue ↵
            </span>
            <span
              onClick={cancelDraft}
              className="hov-ink-wash"
              style={{
                ...label(8, '.12em'),
                color: 'var(--fg-2)',
                border: '1px solid var(--line-divider)',
                padding: '3px 9px',
                cursor: 'pointer',
              }}
            >
              Cancel
            </span>
            <span style={{ flex: 1 }} />
            <span style={{ ...mono(7.5), color: 'var(--fg-3)' }}>queued notes land in the composer</span>
          </div>
        </div>,
      )
    }
    for (const c of fileCmts.filter((x) => x.anchor === l.id)) {
      const queued = c.status === 'queued'
      const outdated = c.status === 'outdated'
      const open = cmtOpen[c.id] ?? !outdated
      const idColor = queued ? 'var(--color-accent)' : outdated ? 'var(--color-gold)' : 'var(--fg-1)'
      const chipColor = queued ? 'var(--color-accent)' : outdated ? 'var(--color-gold)' : 'var(--fg-2)'
      rows.push(
        <div
          key={c.id}
          className="lt-fade-in"
          style={{
            margin: '4px 0 6px 52px',
            maxWidth: 500,
            border: `1px ${outdated ? 'dashed' : 'solid'} ${
              outdated ? 'var(--line-color)' : queued ? 'var(--color-accent)' : 'var(--line-divider)'
            }`,
            background: outdated ? 'color-mix(in srgb, var(--color-gold) 5%, transparent)' : 'var(--card-bg)',
            boxShadow: `inset 2px 0 0 ${
              queued ? 'var(--color-accent)' : outdated ? 'var(--color-gold)' : 'var(--fg-3)'
            }`,
          }}
        >
          <div
            onClick={() => toggleComment(c.id)}
            className="hov-wash"
            style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px', cursor: 'pointer' }}
          >
            <Disclose open={open} style={{ ...mono(9), color: 'var(--fg-3)', width: 10 }} />
            <span style={{ ...mono(8.5, 700), letterSpacing: '.1em', color: idColor }}>{c.id}</span>
            <span style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>
              {c.range} · {c.who} · {c.t}
            </span>
            <span style={{ flex: 1 }} />
            <span
              style={{
                ...mono(7, 700),
                letterSpacing: '.14em',
                textTransform: 'uppercase',
                color: chipColor,
                border: `1px solid ${chipColor}`,
                padding: '1px 5px',
                whiteSpace: 'nowrap',
              }}
            >
              {queued ? 'queued' : outdated ? 'outdated' : 'sent'}
            </span>
            {queued && (
              <span
                onClick={(e) => {
                  e.stopPropagation()
                  removeQueued(c.id)
                }}
                title="Discard note"
                className="hov-red"
                style={{ ...mono(10), color: 'var(--fg-3)', cursor: 'pointer', padding: '0 3px' }}
              >
                ×
              </span>
            )}
          </div>
          <Fold open={open}>
            <div style={{ borderTop: '1px solid var(--line-color)', padding: '7px 12px 8px' }}>
              <p
                style={{
                  margin: 0,
                  ...serif(12.5),
                  fontStyle: 'italic',
                  lineHeight: 1.6,
                  color: outdated ? 'var(--fg-3)' : 'var(--fg-2)',
                }}
              >
                {c.text}
              </p>
              {c.appliedIn && (
                <div
                  style={{
                    marginTop: 5,
                    ...mono(7.5),
                    letterSpacing: '.1em',
                    textTransform: 'uppercase',
                    color: 'var(--fg-3)',
                  }}
                >
                  applied in {c.appliedIn} — lines above superseded
                </div>
              )}
            </div>
          </Fold>
        </div>,
      )
    }
  })

  return (
    <aside
      id="mdv-panel"
      className="trk-rail"
      data-folded={pvOpen ? undefined : ''}
      data-dragging={railDrag === 'pv' ? 'true' : undefined}
      style={asideStyle}
    >
      <div
        style={{
          width: widthCss,
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          boxSizing: 'border-box',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'stretch',
            height: 34,
            boxSizing: 'border-box',
            borderBottom: '1px solid var(--line-divider)',
            flexShrink: 0,
          }}
        >
          <span style={{ flex: 1, display: 'flex', alignItems: 'stretch', overflow: 'hidden', minWidth: 0 }}>
            {tabList.map((name) => {
              const on = name === activeFile
              const d2 = docs[name]
              const revColor = on ? 'var(--color-accent)' : 'var(--fg-3)'
              return (
                <span
                  key={name}
                  onClick={() => openFile(name)}
                  className="hov-wash"
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                    padding: '0 9px',
                    borderRight: '1px solid var(--line-divider)',
                    cursor: 'pointer',
                    background: on ? 'var(--page-bg)' : 'transparent',
                    boxShadow: `inset 0 2px 0 ${on ? 'var(--color-accent)' : 'transparent'}`,
                    maxWidth: 190,
                    minWidth: 0,
                  }}
                >
                  <span
                    style={{
                      ...mono(8.5, 700),
                      color: on ? 'var(--fg-1)' : 'var(--fg-3)',
                      ...ellipsis,
                      minWidth: 0,
                    }}
                  >
                    {name}
                  </span>
                  <span
                    style={{
                      ...mono(6.5, 700),
                      letterSpacing: '.1em',
                      color: revColor,
                      border: `1px solid ${revColor}`,
                      padding: '0 3px',
                      flexShrink: 0,
                    }}
                  >
                    {d2?.rev ?? '—'}
                  </span>
                  <span
                    onClick={(e) => {
                      e.stopPropagation()
                      closeTab(name)
                    }}
                    title="Close file"
                    className="hov-red"
                    style={{ ...mono(10), color: 'var(--fg-3)', cursor: 'pointer', flexShrink: 0, padding: '1px 2px' }}
                  >
                    ×
                  </span>
                </span>
              )
            })}
          </span>
          <span
            onClick={togglePv}
            title="Hide review panel"
            className="hov-ink-wash"
            style={{
              width: 28,
              flexShrink: 0,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
              color: 'var(--fg-2)',
              fontFamily: 'var(--font-display)',
              fontSize: 12,
              borderLeft: '1px solid var(--line-divider)',
            }}
          >
            ←
          </span>
        </div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 9,
            padding: '0 14px',
            height: 30,
            boxSizing: 'border-box',
            borderBottom: '1px solid var(--line-divider)',
            flexShrink: 0,
          }}
        >
          <span
            style={{
              ...label(8, '.12em'),
              color: 'var(--color-accent)',
              border: '1px solid var(--color-accent)',
              padding: '1px 6px',
            }}
          >
            {doc.rev}
          </span>
          <span style={{ ...mono(8.5, 700), color: 'var(--color-teal)' }}>+{adds}</span>
          <span style={{ ...mono(8.5, 700), color: 'var(--color-red)' }}>−{dels}</span>
          <span style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>
            {adds || dels ? 'last turn' : 'no changes this turn'}
          </span>
          <span style={{ width: 1, height: 14, background: 'var(--trk-vline)' }} />
          <span style={{ ...mono(8), color: 'var(--fg-3)', whiteSpace: 'nowrap' }}>
            {fileCmts.length
              ? `${fileCmts.length} note${fileCmts.length > 1 ? 's' : ''}${outd ? ` · ${outd} outdated` : ''}`
              : 'no notes yet'}
          </span>
          <span style={{ flex: 1 }} />
          <span style={{ display: 'inline-flex', border: '1px solid var(--line-divider)' }}>
            <span
              onClick={() => setView('diff')}
              style={{
                ...label(8, '.14em'),
                padding: '3px 8px',
                cursor: 'pointer',
                color: clean ? 'var(--fg-3)' : '#F6F1E5',
                background: clean ? 'transparent' : 'var(--color-ink)',
              }}
            >
              Diff
            </span>
            <span
              onClick={() => setView('clean')}
              style={{
                ...label(8, '.14em'),
                padding: '3px 8px',
                cursor: 'pointer',
                color: clean ? '#F6F1E5' : 'var(--fg-3)',
                background: clean ? 'var(--color-ink)' : 'transparent',
                borderLeft: '1px solid var(--line-divider)',
              }}
            >
              Clean
            </span>
          </span>
        </div>
        <div
          id="mdv-doc"
          className="trk-draft"
          style={{ flex: 1, overflowY: 'auto', minHeight: 0, padding: '20px 20px 46px', userSelect: 'none' }}
        >
          <div
            style={{
              maxWidth: 640,
              margin: '0 auto',
              background:
                'linear-gradient(90deg, transparent 51px, color-mix(in srgb, var(--color-red) 20%, transparent) 51px, color-mix(in srgb, var(--color-red) 20%, transparent) 52px, transparent 52px)',
            }}
          >
            {rows}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 16 }}>
              <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
              <span style={{ ...label(7.5, '.18em'), color: 'var(--fg-3)' }}>
                // end of file · sweep lines to quote · + to comment
              </span>
              <span style={{ flex: 1, borderTop: '1px solid var(--line-color)' }} />
            </div>
          </div>
        </div>
        <div
          style={{
            flexShrink: 0,
            borderTop: '1px solid var(--line-divider)',
            height: 30,
            boxSizing: 'border-box',
            padding: '0 14px 3px',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span style={{ ...mono(8, 700), letterSpacing: '.1em', color: 'var(--fg-1)', whiteSpace: 'nowrap' }}>
            {doc.rev} · {docLn} ln
          </span>
          <span style={{ flex: 1 }} />
          <span style={{ ...mono(7.5), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>
            struck lines = last turn · outdated notes fold at their anchor
          </span>
        </div>
        <TriStripe />
      </div>
      <span
        onMouseDown={dragPv}
        title="Drag to resize"
        className="hov-resize"
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, width: 6, cursor: 'col-resize', zIndex: 40 }}
      />
    </aside>
  )
}
