import { useEffect } from 'react'
import { useConsole } from '@/state/console'
import { ThreadsSidebar } from '@/features/threads/ThreadsSidebar'
import { ControlRail } from '@/features/control/ControlRail'
import { ReviewPanel } from '@/features/review/ReviewPanel'
import { ChatMain } from '@/features/chat/ChatMain'
import { TimelinePanel } from '@/features/timeline/TimelinePanel'
import { StatusBar } from './StatusBar'

export function ConsoleShell() {
  const { applyViewport } = useConsole((s) => s.actions)
  useEffect(() => {
    const measure = () => applyViewport(window.innerWidth)
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [applyViewport])
  return (
    <div
      className="paper-texture"
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: 'calc(100vh / var(--trk-zoom, 1))',
        overflow: 'hidden',
        background: 'var(--page-bg)',
        color: 'var(--fg-1)',
        fontFamily: 'var(--font-sans)',
      }}
    >
      <div style={{ display: 'flex', flex: 1, minHeight: 0, position: 'relative' }}>
        <ThreadsSidebar />
        <ControlRail />
        <ReviewPanel />
        <ChatMain />
        <TimelinePanel />
      </div>
      <StatusBar />
    </div>
  )
}
