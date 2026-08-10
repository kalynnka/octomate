import { useProjects } from '@/lib/api/hooks'
import { useConsole } from '@/state/console'
import { ellipsis, label, mono } from '@/components/text'

/**
 * The strip above a new trunkline thread: where its work will live.
 *
 * A console thread has no directory of its own to be filed from — nothing ran
 * anywhere yet — so unlike `ProjectStrip`, which reports what a thread already
 * is, this one asks. The pick rides the first directive and freezes with the
 * row; no project is a real answer, and leaves the thread a chat.
 *
 * Once that directive has gone (`ntStarted`) the strip stops asking and reports,
 * because the row is written and nothing later carries a project. A dropdown that
 * still opened would be a control that silently does nothing.
 */
export function NewThreadStrip() {
  const ntProject = useConsole((s) => s.ntProject)
  const ntStarted = useConsole((s) => s.ntStarted)
  const ntMenu = useConsole((s) => s.ntMenu)
  const { closeNtMenu, setNtMenu, setNtProject } = useConsole((s) => s.actions)
  // One pick is the whole interaction, so the menu closes on it rather than
  // waiting for a click-away that reads as "did that register?".
  const pick = (name: string | null) => {
    setNtProject(name)
    closeNtMenu()
  }
  const { data: projects } = useProjects()
  const chosen = projects?.find((project) => project.name === ntProject)
  const open = ntMenu === 'proj' && !ntStarted
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
      <span
        style={{
          ...mono(11, 700),
          color: chosen ? 'var(--color-accent)' : 'var(--fg-3)',
          lineHeight: 1,
        }}
      >
        ⌗
      </span>
      <span style={{ position: 'relative', display: 'inline-flex', flexShrink: 0 }}>
        {open && (
          <span onClick={() => setNtMenu('proj')} style={{ position: 'fixed', inset: 0, zIndex: 75 }} />
        )}
        <span
          onClick={ntStarted ? undefined : () => setNtMenu('proj')}
          title={
            ntStarted
              ? 'the project this thread was filed under — fixed when the first directive created it'
              : 'the project this thread will be filed under — fixed once the first directive creates it'
          }
          className={ntStarted ? undefined : 'hov-border'}
          style={{
            ...label(8, '.06em'),
            background: 'var(--surface-raised)',
            border: `1px solid ${open ? 'var(--color-accent)' : 'var(--line-divider)'}`,
            borderRadius: 2,
            height: 20,
            boxSizing: 'border-box',
            padding: '0 7px',
            display: 'inline-flex',
            alignItems: 'center',
            gap: 5,
            cursor: ntStarted ? 'default' : 'pointer',
            whiteSpace: 'nowrap',
            zIndex: 76,
          }}
        >
          <span style={{ color: chosen ? 'var(--color-accent)' : 'var(--fg-2)' }}>
            {chosen ? chosen.name : 'no project'}
          </span>
          {!ntStarted && (
            <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1, marginTop: 1 }}>▾</span>
          )}
        </span>
        {open && (
          <span
            style={{
              position: 'absolute',
              top: 'calc(100% + 6px)',
              left: 0,
              width: 302,
              zIndex: 80,
              background: 'var(--surface-raised)',
              border: '1px solid var(--line-color)',
              boxShadow: 'var(--shadow-soft)',
              display: 'flex',
              flexDirection: 'column',
              maxHeight: 260,
              overflowY: 'auto',
            }}
          >
            <ProjectOption
              name="no project"
              root="a chat — the agent works nowhere in particular"
              on={!chosen}
              onPick={() => pick(null)}
            />
            {(projects ?? []).map((project) => (
              <ProjectOption
                key={project.name}
                name={project.name}
                root={project.description ?? project.root}
                on={project.name === ntProject}
                onPick={() => pick(project.name)}
              />
            ))}
          </span>
        )}
      </span>
      <span style={{ ...mono(8.5), color: 'var(--fg-3)', ...ellipsis, minWidth: 0, flexShrink: 1 }}>
        {chosen ? chosen.root : 'nothing is registered until a project claims it'}
      </span>
      <span style={{ flex: 1, minWidth: 8 }} />
      <span style={{ ...mono(8), color: 'var(--fg-3)', whiteSpace: 'nowrap', flexShrink: 0 }}>
        {ntStarted ? 'filed — a thread keeps its project' : 'filed with the first directive'}
      </span>
    </div>
  )
}

function ProjectOption({
  name,
  root,
  on,
  onPick,
}: {
  name: string
  root: string
  on: boolean
  onPick: () => void
}) {
  return (
    <span
      onClick={(e) => {
        e.stopPropagation()
        onPick()
      }}
      className="hov-wash"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 3,
        padding: '8px 12px',
        cursor: 'pointer',
        background: on ? 'color-mix(in srgb, var(--color-accent) 7%, transparent)' : 'transparent',
        borderBottom: '1px solid var(--line-color)',
      }}
    >
      <span style={{ ...mono(10, 700), color: on ? 'var(--color-accent)' : 'var(--fg-1)' }}>{name}</span>
      <span style={{ ...mono(8), color: 'var(--fg-3)', lineHeight: 1.55, ...ellipsis }}>{root}</span>
    </span>
  )
}
