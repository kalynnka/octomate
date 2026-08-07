/**
 * assistant-ui runtime for the console chat. The ledger is the source of
 * truth: chat turns feed the external-store runtime (composer state,
 * running/canSend), while the ledger DOM itself is rendered by ChatLog. A
 * real backend swaps `onNew` for the POST /api/chat data-stream without
 * touching the UI.
 */
import { useMemo } from 'react'
import {
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from '@assistant-ui/react'
import { useConsole } from '@/state/console'
import type { LedgerItem } from '@/lib/api/types'

const toMessage = (item: LedgerItem): ThreadMessageLike => {
  switch (item.kind) {
    case 'user':
      return { id: item.uid, role: 'user', content: item.text }
    case 'agent':
      return {
        id: item.uid,
        role: 'assistant',
        content: item.blocks
          .map((b) => (b.type === 'p' ? b.text : b.type === 'ul' ? b.items.join('\n') : ''))
          .join('\n'),
      }
    case 'stream':
      return {
        id: item.uid,
        role: 'assistant',
        content: item.text,
        status: item.streaming ? { type: 'running' } : { type: 'complete', reason: 'stop' },
      }
    default:
      return { id: item.uid, role: 'system', content: ' ' }
  }
}

const isTurn = (item: LedgerItem) =>
  item.kind === 'user' || item.kind === 'agent' || item.kind === 'stream'

export function useTrunklineRuntime() {
  const detail = useConsole((s) => s.detail)
  const live = useConsole((s) => s.live)
  const running = useConsole((s) => s.running)
  const { sendDirective } = useConsole((s) => s.actions)

  const messages = useMemo(
    () => [...(detail?.ledger ?? []), ...live].filter(isTurn),
    [detail, live],
  )

  return useExternalStoreRuntime<LedgerItem>({
    messages,
    isRunning: running,
    convertMessage: toMessage,
    async onNew(message: AppendMessage) {
      const text = message.content
        .filter((p): p is { type: 'text'; text: string } => p.type === 'text')
        .map((p) => p.text)
        .join('\n')
      sendDirective(text)
    },
  })
}
