import { lazy, Suspense } from 'react'
import { AssistantRuntimeProvider } from '@assistant-ui/react'
import { useConsole } from '@/state/console'
import { useTrunklineRuntime } from './runtime'
import { ChatHeader } from './ChatHeader'
import { ProjectStrip } from './ProjectStrip'
import { NewThreadStrip } from './NewThreadStrip'
import { ChatLog } from './ChatLog'
import { Composer } from './Composer'
import { SessionBar } from './SessionBar'

const ControlPage = lazy(() => import('@/features/control/ControlPage').then(module => ({ default: module.ControlPage })))

export function ChatMain() {
  const runtime = useTrunklineRuntime()
  const mgmtSec = useConsole((s) => s.mgmtSec)
  const ntOn = useConsole((s) => s.ntOn)
  // A thread with a directory but no project still gets the strip — it is where
  // the work is, and where the VS Code hand-off goes.
  const hasWorkspace = useConsole((s) => !!s.detail?.project || !!s.detail?.vscode)
  const theme = useConsole((s) => s.theme)
  const sysDark = useConsole((s) => s.sysDark)

  const isDark = theme === 'dark' || (theme === 'auto' && sysDark)
  const grainA = isDark ? 1.6 : 3.2
  const grainB = Math.round(grainA * 7.5) / 10

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <main
        style={{
          flex: 1,
          minWidth: 'min(340px,100%)',
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          backgroundColor: 'var(--page-bg)',
          backgroundImage: `radial-gradient(color-mix(in srgb, var(--color-ink) ${grainA}%, transparent) 0.5px, transparent 0.6px), radial-gradient(color-mix(in srgb, var(--color-ink) ${grainB}%, transparent) 0.5px, transparent 0.6px)`,
          backgroundSize: '4px 4px, 7px 7px',
          backgroundPosition: '0 0, 2px 3px',
        }}
      >
        {mgmtSec ? (
          <Suspense fallback={<div role="status" style={{ padding: 24 }}>Loading Control…</div>}><ControlPage /></Suspense>
        ) : (
          <>
            <ChatHeader />
            {!ntOn && hasWorkspace && <ProjectStrip />}
            {ntOn && <NewThreadStrip />}
            <ChatLog />
            <Composer />
            <SessionBar />
          </>
        )}
      </main>
    </AssistantRuntimeProvider>
  )
}
