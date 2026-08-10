import { useEffect } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { ConsoleShell } from '@/features/shell/ConsoleShell'
import { fetchThreads } from '@/lib/api/client'
import { queryClient } from '@/lib/queryClient'
import { applyThemeAttr, useConsole } from '@/state/console'

export default function App() {
  const { setSysDark, selectThread } = useConsole((s) => s.actions)

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setSysDark(mq.matches)
    setSysDark(mq.matches)
    mq.addEventListener('change', onChange)
    const s = useConsole.getState()
    applyThemeAttr(s.theme === 'dark' || (s.theme === 'auto' && mq.matches))
    // Boot into the newest live thread; an empty or unreachable relay opens
    // the new-thread flow (the status bar carries the offline state).
    void (async () => {
      let first: { channel_tentacle_id: string; id: string } | undefined
      try {
        first = (await fetchThreads())[0]
      } catch {
        first = undefined
      }
      if (first) void selectThread(first.channel_tentacle_id, first.id)
      else useConsole.getState().actions.startNewThread()
    })()
    return () => mq.removeEventListener('change', onChange)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <QueryClientProvider client={queryClient}>
      <ConsoleShell />
    </QueryClientProvider>
  )
}
