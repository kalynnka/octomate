import { useEffect } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { ConsoleShell } from '@/features/shell/ConsoleShell'
import { fetchLiveThreads } from '@/lib/api/client'
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
    // Boot into the newest live thread; an empty live relay opens the
    // new-thread flow, and only an unreachable one falls back to the CI demo.
    void (async () => {
      try {
        const live = await fetchLiveThreads()
        const first = live[0]
        if (first) void selectThread(first.channel, first.id)
        else useConsole.getState().actions.startNewThread()
        return
      } catch {
        void selectThread('trunkline', 'THR-0198')
      }
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
