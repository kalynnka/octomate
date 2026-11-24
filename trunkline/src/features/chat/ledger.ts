import type { LedgerItem } from '@/lib/api/types'

export type LocalNotice = Extract<LedgerItem, { kind: 'notice' }> & {
  /** Number of preceding message rows, including history not currently visible. */
  position: number
}

/** Merge the saved and live rows, keeping local notices at their arrival positions. */
export function chatRows(ledger: LedgerItem[], live: LedgerItem[], notices: LocalNotice[] = []): LedgerItem[] {
  const saved = new Set(ledger.map(item => item.uid))
  const rows = [...ledger, ...live.filter(item => !saved.has(item.uid))].filter(item => item.kind !== 'dots')
  for (const [index, notice] of notices.entries()) rows.splice(notice.position + index, 0, notice)
  return rows
}
