import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { LedgerRow } from '../../src/features/chat/cards'
import { queryClient } from '../../src/lib/queryClient'
import { useConsole } from '../../src/state/console'
import { useState } from 'react'
import '../../src/styles/lonetrail.css'
import '../../src/styles/motion.css'
import '../../src/features/artifacts/artifacts.css'

useConsole.setState({ selThreadId: 'thread' })
export function Harness() {
  const [shown, setShown] = useState(true)
  return <QueryClientProvider client={queryClient}>
    <button onClick={() => setShown(false)}>Remove cards</button>
    <button onClick={() => { document.documentElement.dataset.theme = 'dark' }}>Dark</button>
    {shown && <div id="trk-chatlog">
      <LedgerRow item={{ kind: 'artifact', uid: 'row-a', artifactId: 'app-a' }} cardMax="100%" />
      <LedgerRow item={{ kind: 'artifact', uid: 'row-b', artifactId: 'app-b' }} cardMax="100%" />
    </div>}
  </QueryClientProvider>
}
createRoot(document.getElementById('root')!).render(<Harness />)
