import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Markdown } from '../../src/components/Markdown'
import { LedgerRow } from '../../src/features/chat/cards'
import { botLabel } from '../../src/lib/api/fold'
import type { LedgerItem } from '../../src/lib/api/types'
import { useConsole } from '../../src/state/console'
import '../../src/styles/lonetrail.css'
import '../../src/styles/console.css'
import '../../src/styles/motion.css'

const code = 'This is `demo_worker.py`.\n\n## Python\n\n```python\nimport json\nfrom pathlib import Path\n\n# A private workspace for this worker.\nworkspace = Path("/work/alice")\ncount = 42\nif count > 0:\n    print("Ready", workspace)\n```'
const longCode = ['message = """First line', 'Second line', 'Third line"""', '', ...Array.from({ length: 100 }, (_, index) => `value_${index} = ${index}`), `print("${'a long line of code '.repeat(20)}")`].join('\n')
const diagram = '```mermaid\nflowchart LR\n    accTitle: Octomate dispatch\n    Core[Octomate core] --> Worker[Alice worker]\n    Worker --> CLI[CLI tools]\n```'
const second = '> ```mermaid\n> flowchart TD\n>     accTitle: Thread lifecycle\n>     Start --> Done\n> ```'
const tall = '```mermaid\nflowchart TD\n    Start([Start]) --> Setup[Set a = 2 and b = 3]\n    Setup --> Calculate[Calculate sum = a + b]\n    Calculate --> Decision{Is sum greater than 0?}\n    Decision -->|Yes| Positive[Print sum]\n    Decision -->|No| Other[Print zero]\n    Positive --> End([End])\n    Other --> End\n```'
useConsole.getState().actions.setTheme('light')

export function Harness() {
  const [message, setMessage] = useState({ text: code, cursor: false })
  const [reasoning, setReasoning] = useState(false)
  const [thinking, setThinking] = useState(true)
  const reasoningItems: LedgerItem[] = [
    { kind: 'user', uid: 'copy-user', who: 'Owner', t: '10:00', text: '**Keep** the `workspace`.\n\nSecond line.', reply: { who: 'Octomate', text: 'Earlier context, not part of this message.' } },
    { kind: 'agent', uid: 'progress', label: botLabel('codex'), blocks: [{ type: 'p', text: '**Checking macOS setup**\nI am checking the admin permissions before continuing.' }] },
    { kind: 'agent', uid: 'claude-reply', label: botLabel('claude'), blocks: [{ type: 'p', text: 'Workspace permissions checked.' }] },
    { kind: 'agent', uid: 'dsh-reply', label: botLabel('deepseek'), blocks: [{ type: 'p', text: 'Ready to continue.' }] },
    { kind: 'think', uid: 'reasoning', text: '**Tracing the permission boundary**\n\nCheck the current user and `workspace` access.\n\n- Inspect the owner.\n- Verify the permitted path.', dur: thinking ? '…' : '2s', thinking },
    { kind: 'think', uid: 'codex-summary', format: 'summary', text: '**Awaiting macOS admin setup**\n\nThe account needs access to the shared workspace.', dur: thinking ? '…' : '1s', thinking },
    { kind: 'think', uid: 'claude-summary', format: 'summary', text: '**Checking the configuration**\n\nThe permission settings need to agree with the workspace owner.', dur: '1s', thinking: false },
    { kind: 'think', uid: 'unavailable', format: 'summary', text: '', dur: '', thinking: false },
    { kind: 'agent', uid: 'copy-agent', label: botLabel('codex'), blocks: [
      { type: 'p', text: '**Ready** to continue.' },
      { type: 'ul', items: ['First item', 'Second `item`'] },
      { type: 'diff', diff: { file: 'demo.py', adds: 1, dels: 0, lines: [{ tone: 'add', text: '+ ready = True' }] } },
    ] },
    { kind: 'stream', uid: 'copy-stream', text: 'A **native** reply with `code`.', streaming: false },
  ]
  const { setTheme } = useConsole.getState().actions
  return <main style={{ maxWidth: 900, margin: '24px auto', padding: 16, fontFamily: 'var(--font-cn)', fontSize: 16 }}>
    <nav style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 24 }}>
      <button onClick={() => setMessage({ text: `${code}\n\n${diagram}\n\n${second}`, cursor: false })}>Example</button>
      <button onClick={() => setMessage({ text: `\`\`\`python\n${longCode}\n\`\`\``, cursor: false })}>Long code</button>
      <button onClick={() => setMessage({ text: tall, cursor: false })}>Tall</button>
      <button onClick={() => setMessage({ text: '```mermaid\nsequenceDiagram\nAlice->>Worker: Run task\nWorker-->>Alice: Done\n```', cursor: false })}>Sequence</button>
      <button onClick={() => setMessage({ text: '```mermaid\nflowchart LR\n    Core -->', cursor: true })}>Stream</button>
      <button onClick={() => setMessage({ text: diagram, cursor: false })}>Finish</button>
      <button onClick={() => setMessage({ text: '```mermaid\nflowchart LR\nA --> [\n```', cursor: false })}>Invalid</button>
      <button onClick={() => setMessage({ text: '```mermaid\nflowchart LR\nA["<img src=x onerror=alert(1)>"] --> B[Safe]\nclick A "javascript:alert(1)"\n```', cursor: false })}>Unsafe</button>
      <button onClick={() => setTheme('light')}>Light</button>
      <button onClick={() => setTheme('dark')}>Dark</button>
      <button onClick={() => setReasoning(true)}>Reasoning</button>
      {reasoning && <button onClick={() => setThinking(false)}>Finish reasoning</button>}
    </nav>
    {reasoning ? <section aria-label="Reasoning cards" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {reasoningItems.map(item => <LedgerRow key={item.uid} item={item} cardMax="100%" />)}
    </section> : <Markdown text={message.text} cursor={message.cursor} />}
  </main>
}

createRoot(document.getElementById('root')!).render(<StrictMode><Harness /></StrictMode>)
