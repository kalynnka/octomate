import type { ArtifactAvailableEvent } from '@/lib/api/events'
import type { LedgerItem } from '@/lib/api/types'

/** A persisted ledger row is the identity in both live events and history pages. */
export function withArtifact(live: LedgerItem[], event: ArtifactAvailableEvent, history: LedgerItem[] = []): LedgerItem[] {
  if (live.some((item) => item.uid === event.message_id) || history.some((item) => item.uid === event.message_id)) return live
  return [...live, { kind: 'artifact', uid: event.message_id, artifactId: event.artifact_id }]
}
