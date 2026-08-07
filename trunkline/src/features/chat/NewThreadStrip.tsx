import { useState } from 'react'
import { useConsole } from '@/state/console'
import { useProjects } from '@/lib/api/hooks'
import { Icon } from '@/components/Icon'
import { ellipsis, fieldLabel, label, mono, sectionLabel } from '@/components/text'

/** Project-picker strip shown while composing a new web thread. */
export function NewThreadStrip() {
  const ntMenu = useConsole((s) => s.ntMenu)
  const ntMenuPos = useConsole((s) => s.ntMenuPos)
  const ntProjSel = useConsole((s) => s.ntProjSel)
  const ntBranch = useConsole((s) => s.ntBranch)
  const ntPure = useConsole((s) => s.ntPure)
  const vsLaunch = useConsole((s) => s.vsLaunch)
  const { setNtMenu, closeNtMenu, pickNtProject, togglePure, registerProject, pickBranch, forkBranch, vsOpen } =
    useConsole((s) => s.actions)
  const { data: projects } = useProjects()
  const [newPath, setNewPath] = useState('')
  const [newBr, setNewBr] = useState('')

  const openMenu = (menu: 'proj' | 'br') => (e: React.MouseEvent) => {
    const r = e.currentTarget.getBoundingClientRect()
    setNtMenu(menu, { top: r.bottom + 7, right: Math.max(12, window.innerWidth - r.right) })
  }
  const curBr = ntProjSel?.git ? ntProjSel.branches.find((b) => b.n === ntBranch) : undefined

  return (
    <div
      id="trk-ntstrip"
      className="lt-fade-in"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 9,
        padding: '0 16px',
        height: 30,
        boxSizing: 'border-box',
        borderBottom: '1px solid var(--line-divider)',
        flexShrink: 0,
        background: 'var(--trk-wash)',
        position: 'relative',
        zIndex: 70,
      }}
    >
      {/* Click-away for this strip's own menus only — the composer's route
          menu lives in another stacking context and brings its own. */}
      {(ntMenu === 'proj' || ntMenu === 'br') && (
        <span onClick={closeNtMenu} style={{ position: 'fixed', inset: 0, zIndex: 75 }} />
      )}
      <span style={{ flex: 1, minWidth: 8 }} />
      {!ntProjSel && (
        <span style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0, flexShrink: 1 }}>
          {ntPure
            ? 'chat only — no session dir, nothing written to disk'
            : 'optional — session boots in a scratch dir until one is filed'}
        </span>
      )}
      {ntProjSel?.note && (
        <span title="threads already sharing this working tree" style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0, flexShrink: 1 }}>
          {ntProjSel.note}
        </span>
      )}
      <span style={{ ...mono(11, 700), color: ntProjSel ? 'var(--color-accent)' : 'var(--fg-3)', lineHeight: 1 }}>⌗</span>
      <span style={{ position: 'relative', display: 'inline-flex', flexShrink: 0, zIndex: 76 }}>
        {!ntProjSel ? (
          <span
            onClick={openMenu('proj')}
            title="file this thread to a working tree — siblings filed there share it"
            className="hov-accent-wash"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              ...label(8, '.12em'),
              color: 'var(--color-accent)',
              border: `1px dashed ${ntMenu === 'proj' ? 'var(--color-accent)' : 'var(--line-divider)'}`,
              padding: '0 8px',
              height: 20,
              boxSizing: 'border-box',
              cursor: 'pointer',
            }}
          >
            attach project <span style={{ fontSize: 7, lineHeight: 1, marginTop: 1 }}>▾</span>
          </span>
        ) : (
          <span
            onClick={openMenu('proj')}
            title="working tree of this thread — click to switch"
            className="hov-border"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              ...mono(9, 700),
              letterSpacing: '.08em',
              color: 'var(--fg-1)',
              border: `1px solid ${ntMenu === 'proj' ? 'var(--color-accent)' : 'var(--line-divider)'}`,
              padding: '0 8px',
              height: 20,
              boxSizing: 'border-box',
              cursor: 'pointer',
              whiteSpace: 'nowrap',
            }}
          >
            {ntProjSel.name} <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1, marginTop: 1 }}>▾</span>
          </span>
        )}
        {ntMenu === 'proj' && (
          <span
            style={{
              position: 'fixed',
              top: ntMenuPos.top,
              right: ntMenuPos.right,
              width: 'min(344px, calc(100vw - 24px))',
              zIndex: 80,
              background: 'var(--surface-raised)',
              border: '1px solid var(--line-color)',
              boxShadow: 'var(--shadow-soft)',
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <span style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '8px 12px 7px', borderBottom: '1px solid var(--line-divider)' }}>
              <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>Project</span>
              <span style={{ flex: 1 }} />
              <span style={{ ...mono(7.5), color: 'var(--fg-3)', letterSpacing: '.06em' }}>files this thread to a working tree</span>
            </span>
            {(projects ?? []).map((p) => {
              const on = ntProjSel?.path === p.path
              const first = p.branches[0]
              return (
                <span
                  key={p.path}
                  onClick={() => pickNtProject(p)}
                  className="hov-wash"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 9,
                    padding: '7px 12px',
                    cursor: 'pointer',
                    background: on ? 'color-mix(in srgb, var(--color-accent) 6%, transparent)' : 'transparent',
                    borderBottom: '1px solid var(--line-color)',
                  }}
                >
                  <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <span style={{ display: 'flex', alignItems: 'baseline', gap: 7 }}>
                      <span style={{ ...mono(10, 700), color: 'var(--fg-1)' }}>{p.name}</span>
                      <span style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0 }}>{p.path}</span>
                    </span>
                    <span style={{ ...mono(7.5), color: 'var(--fg-3)', letterSpacing: '.02em' }}>{p.sub}</span>
                  </span>
                  <span
                    style={{
                      ...mono(8),
                      color: !p.git ? 'var(--fg-3)' : first?.dirty ? 'var(--color-gold)' : 'var(--color-sage)',
                      whiteSpace: 'nowrap',
                      flexShrink: 0,
                    }}
                  >
                    {!p.git ? 'no git' : first ? `${first.n} · ${first.stat}` : ''}
                  </span>
                  <span style={{ width: 10, flexShrink: 0, ...mono(10, 700), color: 'var(--color-accent)', textAlign: 'right' }}>
                    {on ? '✓' : ''}
                  </span>
                </span>
              )
            })}
            <span onClick={() => pickNtProject(null)} className="hov-wash" style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '7px 12px', cursor: 'pointer', borderBottom: '1px solid var(--line-color)' }}>
              <span style={{ ...mono(9, 700), color: 'var(--fg-2)' }}>pure chat</span>
              <span style={{ ...mono(7.5), color: 'var(--fg-3)' }}>plain thread — chat only, nothing shared, nothing written</span>
              <span style={{ flex: 1 }} />
              <span style={{ width: 10, ...mono(10, 700), color: 'var(--color-accent)', textAlign: 'right' }}>{ntPure ? '✓' : ''}</span>
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', background: 'var(--surface-sunken)' }}>
              <span style={{ ...sectionLabel, color: 'var(--fg-2)', flexShrink: 0 }}>Register</span>
              <input
                value={newPath}
                onChange={(e) => setNewPath(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    registerProject(newPath)
                    setNewPath('')
                  }
                }}
                placeholder="~/path/to/tree — ⏎ files it"
                style={{
                  flex: 1,
                  minWidth: 0,
                  ...mono(9),
                  color: 'var(--fg-1)',
                  background: 'transparent',
                  border: 'none',
                  outline: 'none',
                  borderBottom: '1px solid var(--line-divider)',
                  padding: '2px 0',
                }}
              />
              <span
                onClick={() => {
                  registerProject(newPath)
                  setNewPath('')
                }}
                className="hov-accent-fill"
                style={{
                  ...label(8, '.1em'),
                  color: 'var(--color-accent)',
                  border: '1px solid var(--color-accent)',
                  padding: '0 7px',
                  height: 18,
                  boxSizing: 'border-box',
                  display: 'inline-flex',
                  alignItems: 'center',
                  cursor: 'pointer',
                  flexShrink: 0,
                }}
              >
                ⏎ new
              </span>
            </span>
          </span>
        )}
      </span>
      {!ntProjSel && (
        <span
          onClick={togglePure}
          title="plain thread — chat only, no working tree or session dir"
          className="hov-border"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            ...label(8, '.12em'),
            color: ntPure ? 'var(--trk-on-fill)' : 'var(--fg-2)',
            background: ntPure ? 'var(--color-accent)' : 'transparent',
            border: `1px ${ntPure ? 'solid var(--color-accent)' : 'dashed var(--line-divider)'}`,
            padding: '0 8px',
            height: 20,
            boxSizing: 'border-box',
            cursor: 'pointer',
            flexShrink: 0,
          }}
        >
          {ntPure ? '✓ pure chat' : 'pure chat'}
        </span>
      )}
      {ntProjSel && (
        <>
          <span style={{ ...mono(8.5), color: 'var(--fg-3)', ...ellipsis, minWidth: 0, flexShrink: 1 }}>{ntProjSel.path}</span>
          {ntProjSel.git ? (
            <>
              <span style={{ width: 1, height: 14, background: 'var(--trk-vline)', flexShrink: 0 }} />
              <span style={{ position: 'relative', display: 'inline-flex', flexShrink: 0, zIndex: 76 }}>
                <span
                  onClick={openMenu('br')}
                  title="branch the session checks out — click to switch or fork"
                  className="hov-border"
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 5,
                    ...mono(8.5, 700),
                    color: 'var(--fg-1)',
                    border: `1px solid ${ntMenu === 'br' ? 'var(--color-accent)' : 'var(--line-divider)'}`,
                    padding: '0 7px',
                    height: 20,
                    boxSizing: 'border-box',
                    cursor: 'pointer',
                    whiteSpace: 'nowrap',
                  }}
                >
                  <Icon name="gitBranch" size={11} style={{ color: 'var(--fg-2)' }} />
                  {ntBranch}{' '}
                  <span style={{ color: curBr?.dirty ? 'var(--color-gold)' : 'var(--color-sage)' }}>{curBr?.stat}</span>{' '}
                  <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1, marginTop: 1 }}>▾</span>
                </span>
                {ntMenu === 'br' && (
                  <span
                    style={{
                      position: 'fixed',
                      top: ntMenuPos.top,
                      right: ntMenuPos.right,
                      width: 'min(296px, calc(100vw - 24px))',
                      zIndex: 80,
                      background: 'var(--surface-raised)',
                      border: '1px solid var(--line-color)',
                      boxShadow: 'var(--shadow-soft)',
                      display: 'flex',
                      flexDirection: 'column',
                    }}
                  >
                    <span style={{ display: 'flex', alignItems: 'baseline', gap: 8, padding: '8px 12px 7px', borderBottom: '1px solid var(--line-divider)' }}>
                      <span style={{ ...fieldLabel, color: 'var(--fg-2)' }}>Branch</span>
                      <span style={{ flex: 1 }} />
                      <span style={{ ...mono(7.5), color: 'var(--fg-3)', letterSpacing: '.06em' }}>{ntProjSel.name}</span>
                    </span>
                    {ntProjSel.branches.map((b) => {
                      const on = b.n === ntBranch
                      return (
                        <span
                          key={b.n}
                          onClick={() => pickBranch(b.n)}
                          className="hov-wash"
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                            padding: '7px 12px',
                            cursor: 'pointer',
                            background: on ? 'color-mix(in srgb, var(--color-accent) 6%, transparent)' : 'transparent',
                            borderBottom: '1px solid var(--line-color)',
                          }}
                        >
                          <span style={{ ...mono(9.5, 700), color: 'var(--fg-1)', ...ellipsis, minWidth: 0 }}>{b.n}</span>
                          <span style={{ flex: 1 }} />
                          <span style={{ ...mono(8), color: b.dirty ? 'var(--color-gold)' : 'var(--color-sage)', whiteSpace: 'nowrap', flexShrink: 0 }}>
                            {b.stat}
                          </span>
                          <span style={{ width: 10, flexShrink: 0, ...mono(10, 700), color: 'var(--color-accent)', textAlign: 'right' }}>
                            {on ? '✓' : ''}
                          </span>
                        </span>
                      )
                    })}
                    <span style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', background: 'var(--surface-sunken)' }}>
                      <span style={{ ...sectionLabel, color: 'var(--fg-2)', flexShrink: 0 }}>Fork</span>
                      <input
                        value={newBr}
                        onChange={(e) => setNewBr(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            forkBranch(newBr)
                            setNewBr('')
                          }
                        }}
                        placeholder={`new branch off ${ntBranch ?? 'main'} — ⏎ forks it`}
                        style={{
                          flex: 1,
                          minWidth: 0,
                          ...mono(9),
                          color: 'var(--fg-1)',
                          background: 'transparent',
                          border: 'none',
                          outline: 'none',
                          borderBottom: '1px solid var(--line-divider)',
                          padding: '2px 0',
                        }}
                      />
                      <span
                        onClick={() => {
                          forkBranch(newBr)
                          setNewBr('')
                        }}
                        className="hov-accent-fill"
                        style={{
                          ...label(8, '.1em'),
                          color: 'var(--color-accent)',
                          border: '1px solid var(--color-accent)',
                          padding: '0 7px',
                          height: 18,
                          boxSizing: 'border-box',
                          display: 'inline-flex',
                          alignItems: 'center',
                          cursor: 'pointer',
                          flexShrink: 0,
                        }}
                      >
                        ⏎ fork
                      </span>
                    </span>
                  </span>
                )}
              </span>
            </>
          ) : (
            <>
              <span style={{ width: 1, height: 14, background: 'var(--trk-vline)', flexShrink: 0 }} />
              <span style={{ ...mono(8), color: 'var(--fg-3)', flexShrink: 0 }}>no git</span>
            </>
          )}
          {vsLaunch && (
            <span style={{ ...label(8, '.1em'), color: 'var(--info-strong)', whiteSpace: 'nowrap', flexShrink: 0, animation: 'trkPulse 1s infinite' }}>
              ⌁ vscode:// — window restored
            </span>
          )}
          <span
            onClick={vsOpen}
            title="Open this project in VS Code — jumps to the window if it's already open"
            className="hov-info-fill"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              ...label(8, '.12em'),
              color: 'var(--info-strong)',
              border: '1px solid var(--info)',
              padding: '0 8px',
              height: 20,
              boxSizing: 'border-box',
              cursor: 'pointer',
              flexShrink: 0,
            }}
          >
            <Icon name="code" size={11} strokeWidth={2.4} />
            VS Code
          </span>
        </>
      )}
    </div>
  )
}
