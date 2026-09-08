import { useEffect } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { AuthGate } from '@/features/auth/AuthGate'
import { ConsoleShell } from '@/features/shell/ConsoleShell'
import { queryClient } from '@/lib/queryClient'
import { applyThemeAttr, useConsole } from '@/state/console'

export default function App() {
  const { setSysDark } = useConsole((s) => s.actions)

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setSysDark(mq.matches)
    setSysDark(mq.matches)
    mq.addEventListener('change', onChange)
    const s = useConsole.getState()
    applyThemeAttr(s.theme === 'dark' || (s.theme === 'auto' && mq.matches))
    return () => mq.removeEventListener('change', onChange)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <QueryClientProvider client={queryClient}>
      <AuthGate>
        <ConsoleShell />
      </AuthGate>
    </QueryClientProvider>
  )
}
