import { useEffect } from 'react'
import { useConsole } from '@/state/console'
import { followAddress } from '@/state/url'
import { ThreadsSidebar } from '@/features/threads/ThreadsSidebar'
import { ControlRail } from '@/features/control/ControlRail'
import { ReviewPanel } from '@/features/review/ReviewPanel'
import { ChatMain } from '@/features/chat/ChatMain'
import { TimelinePanel } from '@/features/timeline/TimelinePanel'
import { StatusBar } from './StatusBar'

export function ConsoleShell() {
  const { applyViewport, closeOverlays, cyclePermissionMode } = useConsole((s) => s.actions)
  const overlayOpen = useConsole(
    (s) => !s.sbFold || (!s.mgmtSec && (s.mgmtOpen || (s.traceOn === true && !s.pvOpen))),
  )
  // Boot into what the address names, else the newest live thread; an empty or
  // unreachable relay opens the new-thread flow (the status bar carries the
  // offline state). The shell mounts once per sign-in, and the address follows
  // what is open for as long as it stays mounted.
  useEffect(() => followAddress(), [])
  useEffect(() => {
    const measure = () => applyViewport(window.innerWidth)
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [applyViewport])
  // ⇧⇥ steps the approval posture, as it does in the agents' own terminals, and
  // from anywhere: the composer is where it is reached for, and a shortcut that
  // only worked while the caret sat there would be the one that failed at the
  // moment a card is on screen. It costs the console reverse tab-navigation,
  // which is the trade the shortcut is worth.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (useConsole.getState().mgmtSec) return
      if (event.key !== 'Tab' || !event.shiftKey) return
      if (event.ctrlKey || event.metaKey || event.altKey) return
      event.preventDefault()
      void cyclePermissionMode()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cyclePermissionMode])
  return (
    <div
      className="paper-texture"
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: 'calc(100dvh / var(--trk-zoom, 1))',
        overflow: 'hidden',
        position: 'relative',
        background: 'var(--page-bg)',
        color: 'var(--fg-1)',
        fontFamily: 'var(--font-sans)',
      }}
    >
      <div className="trk-shell-row" style={{ display: 'flex', flex: 1, minHeight: 0, position: 'relative' }}>
        <ThreadsSidebar />
        <ControlRail />
        <ReviewPanel />
        <ChatMain />
        <TimelinePanel />
      </div>
      <StatusBar />
      {/* Covers the status bar as well as the row: a touch anywhere outside
          the open overlay dismisses it. */}
      {overlayOpen && <div className="trk-scrim" onClick={closeOverlays} />}
    </div>
  )
}
