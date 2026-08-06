import { useEffect } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConsoleShell } from '@/features/shell/ConsoleShell'
import { applyThemeAttr, useConsole } from '@/state/console'

const queryClient = new QueryClient()

export default function App() {
  const { setSysDark, selectThread } = useConsole((s) => s.actions)

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setSysDark(mq.matches)
    setSysDark(mq.matches)
    mq.addEventListener('change', onChange)
    const s = useConsole.getState()
    applyThemeAttr(s.theme === 'dark' || (s.theme === 'auto' && mq.matches))
    void selectThread('dev_ui', 'THR-0198')
    return () => mq.removeEventListener('change', onChange)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <QueryClientProvider client={queryClient}>
      <ConsoleShell />
    </QueryClientProvider>
  )
}
