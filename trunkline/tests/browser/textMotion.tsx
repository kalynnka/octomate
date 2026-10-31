import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { TextSwap } from '../../src/components/TextSwap'
import { Button } from '../../src/components/Button'
import '../../src/styles/lonetrail.css'
import '../../src/styles/motion.css'

export function Harness() {
  const [index, setIndex] = useState(0)
  const labels = ['Ready', 'A longer status label', 'Done', '']
  return <main style={{ padding: 24 }}>
    <Button onClick={() => setIndex(value => (value + 1) % labels.length)}>{labels[index]}</Button>
    <div style={{ width: 90, marginTop: 16, whiteSpace: 'nowrap' }}>
      <TextSwap role="status" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{labels[index]}</TextSwap>
    </div>
    <TextSwap data-testid="counter">{index}</TextSwap>
  </main>
}

createRoot(document.getElementById('root')!).render(<StrictMode><Harness /></StrictMode>)
