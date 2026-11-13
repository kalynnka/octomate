import { useEffect, useState } from 'react'
import { Button } from '@/components/Button'
import { TextSwap } from '@/components/TextSwap'
import { useConsole } from '@/state/console'
import { followAddress } from '@/state/url'
import { watchThreads } from '@/state/threads'
import { resumeActivity } from '@/state/activity'
import { ThreadsSidebar } from '@/features/threads/ThreadsSidebar'
import { ControlRail } from '@/features/control/ControlRail'
import { ReviewPanel } from '@/features/review/ReviewPanel'
import { ChatMain } from '@/features/chat/ChatMain'
import { TimelinePanel } from '@/features/timeline/TimelinePanel'
import { StatusBar } from './StatusBar'

export function ConsoleShell() {
  const [threadListError, setThreadListError] = useState<string | null>(null)
  const [reconnecting, setReconnecting] = useState(false)
  const [connectionAttempt, setConnectionAttempt] = useState(0)
  const actions = useConsole((s) => s.actions)
  const { applyViewport, closeOverlays, cyclePermissionMode, resumeLiveUpdates } = actions
  useEffect(() => {
    let disconnected = true
    let stop: (() => void) | undefined
    const resume = () => {
      if (document.visibilityState !== 'visible') return
      if (disconnected) {
        stop?.()
        disconnected = false
        setReconnecting(true)
        stop = watchThreads(message => {
          disconnected = true
          setReconnecting(false)
          setThreadListError(message)
        }, () => {
          setReconnecting(false)
          setThreadListError(null)
        })
      }
      resumeActivity()
      void resumeLiveUpdates()
    }
    resume()
    document.addEventListener('visibilitychange', resume)
    window.addEventListener('focus', resume)
    return () => {
      document.removeEventListener('visibilitychange', resume)
      window.removeEventListener('focus', resume)
      stop?.()
    }
  }, [resumeLiveUpdates, connectionAttempt])
  const overlayOpen = useConsole(
    (s) => !s.sbFold || (!s.mgmtSec && (s.mgmtOpen || (s.traceOn === true && !s.pvOpen))),
  )
  // Boot into what the address names, else the newest live thread; an empty or
  // unreachable relay opens the new-thread flow (the status bar carries the
  // offline state). Reattach when hot reload replaces the store's actions.
  useEffect(() => followAddress(), [actions])
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
      if (event.key === 'Escape' && !event.defaultPrevented && overlayOpen && window.matchMedia('(max-width: 599px), (max-height: 519px) and (max-width: 999px)').matches) {
        const state = useConsole.getState()
        const target = state.traceOn ? '[aria-controls="trk-trace-panel"]' : !state.sbFold ? '[aria-controls="trk-channel-list"]' : '[aria-label="Control"]'
        closeOverlays()
        requestAnimationFrame(() => document.querySelector<HTMLElement>(target)?.focus())
        return
      }
      if (useConsole.getState().mgmtSec) return
      if (event.key !== 'Tab' || !event.shiftKey) return
      if (event.ctrlKey || event.metaKey || event.altKey) return
      event.preventDefault()
      void cyclePermissionMode()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cyclePermissionMode, closeOverlays, overlayOpen])
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
      {threadListError && (
        <div className="trk-thread-list-alert lt-fade-in" role={reconnecting ? 'status' : 'alert'}>
          <svg className={reconnecting ? 'lt-spin' : undefined} aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {reconnecting ? <path d="M20 12a8 8 0 1 1-8-8" /> : <>
              <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
              <path d="M12 9v4M12 17h.01" />
            </>}
          </svg>
          <div className="trk-thread-list-alert-copy">
            <strong><TextSwap>{reconnecting ? 'Reconnecting…' : threadListError}</TextSwap></strong>
            <span>{reconnecting ? 'Restoring thread-list updates.' : 'Reconnect to resume live updates.'}</span>
          </div>
          <Button disabled={reconnecting} onClick={() => setConnectionAttempt(attempt => attempt + 1)} style={{ flexShrink: 0 }}>
            {reconnecting ? 'Connecting…' : 'Reconnect'}
          </Button>
        </div>
      )}
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
