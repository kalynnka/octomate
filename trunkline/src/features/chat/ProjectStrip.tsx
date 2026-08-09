import { useConsole } from '@/state/console'
import { Icon } from '@/components/Icon'
import { ellipsis, label, mono } from '@/components/text'

export function ProjectStrip() {
  const project = useConsole((s) => s.detail?.project)
  const vsLaunch = useConsole((s) => s.vsLaunch)
  const { vsOpen } = useConsole((s) => s.actions)
  if (!project) return null
  return (
    <div
      id="trk-projstrip"
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
      <span style={{ ...mono(11, 700), color: 'var(--color-accent)', lineHeight: 1 }}>⌗</span>
      <span
        title="project directory — fixed when the thread was opened, from where its session ran"
        style={{ ...mono(9, 700), letterSpacing: '.08em', color: 'var(--fg-1)', whiteSpace: 'nowrap', flexShrink: 0 }}
      >
        {project.name}
      </span>
      <span style={{ ...mono(8.5), color: 'var(--fg-3)', ...ellipsis, minWidth: 0, flexShrink: 1 }}>{project.path}</span>
      {project.cwd && (
        <span
          title="where the last run ran — inside this tree, below its root"
          style={{ ...mono(8.5), color: 'var(--fg-2)', whiteSpace: 'nowrap', flexShrink: 0 }}
        >
          ↳ {project.cwd}
        </span>
      )}
      {/* Unset git state is unknown, not absent: no endpoint reports it yet, and
          "no git" would be a claim about the tree nothing here has checked. */}
      {project.git !== undefined && (
        <>
          <span style={{ width: 1, height: 14, background: 'var(--trk-vline)', flexShrink: 0 }} />
          {project.git ? (
            <span title="git state of the shared tree" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, flexShrink: 0 }}>
              <Icon name="gitBranch" size={11} style={{ color: 'var(--fg-2)' }} />
              <span style={{ ...mono(8.5, 700), color: 'var(--fg-1)', whiteSpace: 'nowrap' }}>{project.branch}</span>
              <span style={{ ...mono(8.5, 700), color: project.dirty ? 'var(--color-gold)' : 'var(--color-sage)', whiteSpace: 'nowrap' }}>
                {project.stat}
              </span>
            </span>
          ) : (
            <span style={{ ...mono(8), color: 'var(--fg-3)', flexShrink: 0 }}>no git</span>
          )}
        </>
      )}
      <span style={{ flex: 1, minWidth: 8 }} />
      {project.also && (
        <span title="threads sharing this working tree" style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0, flexShrink: 1 }}>
          {project.also}
        </span>
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
    </div>
  )
}
