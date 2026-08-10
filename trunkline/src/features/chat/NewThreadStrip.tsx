import { ellipsis, label, mono } from '@/components/text'

/**
 * The strip above a new trunkline thread.
 *
 * Frozen, and empty by design: a thread's project is written with the thread,
 * from the directory the session ran in, and no relay endpoint takes one on
 * create. A picker here would move local state and nothing else — a promise the
 * backend never keeps. It lights up when thread-create carries a workspace
 * (README gap list, item 4), and that is a change in `lib/api`, not here.
 */
export function NewThreadStrip() {
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
      <span style={{ flex: 1, minWidth: 8 }} />
      <span style={{ ...mono(11, 700), color: 'var(--fg-3)', lineHeight: 1 }}>⌗</span>
      <span
        style={{ ...label(8, '.12em'), color: 'var(--fg-2)', whiteSpace: 'nowrap', flexShrink: 0 }}
      >
        no project
      </span>
      <span style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis, minWidth: 0, flexShrink: 1 }}>
        a thread is filed where its session runs — never from here
      </span>
    </div>
  )
}
