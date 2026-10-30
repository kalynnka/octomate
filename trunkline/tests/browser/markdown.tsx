import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Markdown } from '../../src/components/Markdown'
import { useConsole } from '../../src/state/console'
import '../../src/styles/lonetrail.css'
import '../../src/styles/console.css'
import '../../src/styles/motion.css'

const code = 'This is `demo_worker.py`.\n\n```python\nimport json\nfrom pathlib import Path\n\n# A private workspace for this worker.\nworkspace = Path("/work/alice")\ncount = 42\nif count > 0:\n    print("Ready", workspace)\n```'
const diagram = '```mermaid\nflowchart LR\n    accTitle: Octomate dispatch\n    Core[Octomate core] --> Worker[Alice worker]\n    Worker --> CLI[CLI tools]\n```'
const second = '> ```mermaid\n> flowchart TD\n>     accTitle: Thread lifecycle\n>     Start --> Done\n> ```'
const tall = '```mermaid\nflowchart TD\n    Start([Start]) --> Setup[Set a = 2 and b = 3]\n    Setup --> Calculate[Calculate sum = a + b]\n    Calculate --> Decision{Is sum greater than 0?}\n    Decision -->|Yes| Positive[Print sum]\n    Decision -->|No| Other[Print zero]\n    Positive --> End([End])\n    Other --> End\n```'
useConsole.getState().actions.setTheme('light')

export function Harness() {
  const [message, setMessage] = useState({ text: code, cursor: false })
  const { setTheme } = useConsole.getState().actions
  return <main style={{ maxWidth: 900, margin: '24px auto', padding: 16, fontFamily: 'var(--font-cn)', fontSize: 16 }}>
    <nav style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 24 }}>
      <button onClick={() => setMessage({ text: `${code}\n\n${diagram}\n\n${second}`, cursor: false })}>Example</button>
      <button onClick={() => setMessage({ text: tall, cursor: false })}>Tall</button>
      <button onClick={() => setMessage({ text: '```mermaid\nsequenceDiagram\nAlice->>Worker: Run task\nWorker-->>Alice: Done\n```', cursor: false })}>Sequence</button>
      <button onClick={() => setMessage({ text: '```mermaid\nflowchart LR\n    Core -->', cursor: true })}>Stream</button>
      <button onClick={() => setMessage({ text: diagram, cursor: false })}>Finish</button>
      <button onClick={() => setMessage({ text: '```mermaid\nflowchart LR\nA --> [\n```', cursor: false })}>Invalid</button>
      <button onClick={() => setMessage({ text: '```mermaid\nflowchart LR\nA["<img src=x onerror=alert(1)>"] --> B[Safe]\nclick A "javascript:alert(1)"\n```', cursor: false })}>Unsafe</button>
      <button onClick={() => setTheme('light')}>Light</button>
      <button onClick={() => setTheme('dark')}>Dark</button>
    </nav>
    <Markdown text={message.text} cursor={message.cursor} />
  </main>
}

createRoot(document.getElementById('root')!).render(<StrictMode><Harness /></StrictMode>)
