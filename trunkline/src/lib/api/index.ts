/**
 * The console's data source — live against /api/trunkline, no mock
 * stand-ins. An unreachable relay surfaces as an error state (empty ledger,
 * `relay offline` in the status bar). Surfaces whose endpoints do not exist
 * yet (control, status, teleport targets) render their empty states; see
 * README.md's gap list.
 */
import {
  fetchChannels,
  fetchHealth,
  fetchPermissionModes,
  fetchProjects,
  fetchRoutes,
  fetchThread,
  fetchThreadBatches,
  fetchThreadConversations,
  fetchThreadMessages,
  fetchThreadProject,
  fetchThreads,
  patchPermissionMode,
} from './client'
import type { HealthState } from './client'
import type {
  ApiConversation,
  ApiPermissionModes,
  ApiProject,
  ApiRoute,
} from './events'
import { channelMeta, groupLiveThreads, liveThreadDetail } from './live'
import type { ChannelMeta, ThreadDetail, ThreadSummary } from './types'

export interface RoutesResult {
  routes: ApiRoute[]
}

/**
 * A surface whose relay endpoint does not exist yet (README gap list): typed
 * as absent data so the consuming feature keeps its empty state honest, and
 * lights up by swapping this for a fetch — never by editing the feature.
 */
export function awaitingEndpoint<T>(): T | undefined {
  return undefined
}

export const api = {
  health(): Promise<HealthState> {
    return fetchHealth()
  },

  /** The channels this instance actually connected — drives the sidebar rail. */
  async listChannels(): Promise<ChannelMeta[]> {
    return (await fetchChannels()).map((channel) => channelMeta(channel.id))
  },

  /** The agent-model routes the composer picker offers. */
  async routes(): Promise<RoutesResult> {
    return { routes: await fetchRoutes() }
  },

  /** The projects a new thread can be filed under; empty is a normal answer. */
  projects(): Promise<ApiProject[]> {
    return fetchProjects()
  },

  /** Each agent's approval postures, in the order the switcher steps through. */
  permissionModes(): Promise<ApiPermissionModes> {
    return fetchPermissionModes()
  },

  /** Switch one conversation's posture; the answer is the row as it now stands. */
  setPermissionMode(conversationId: string, mode: string | null): Promise<ApiConversation> {
    return patchPermissionMode(conversationId, mode)
  },

  async listThreads(): Promise<Record<string, ThreadSummary[]>> {
    return groupLiveThreads(await fetchThreads())
  },

  /**
   * One thread, read as the relay shapes it: the row, its ledger, its
   * conversations, its project and its waiting feelers. Five requests in
   * parallel rather than one fat payload — the ledger is the only large one,
   * and keeping it separate is what lets a listing stay small.
   */
  async getThreadDetail(id: string): Promise<ThreadDetail> {
    const [thread, messages, conversations, project, batches] = await Promise.all([
      fetchThread(id),
      fetchThreadMessages(id),
      fetchThreadConversations(id),
      fetchThreadProject(id),
      fetchThreadBatches(id),
    ])
    if (thread === null) throw new Error(`thread ${id} not found on the relay`)
    return liveThreadDetail({
      thread,
      messages: messages ?? [],
      conversations: conversations ?? [],
      project: project ?? null,
      batches: batches ?? [],
    })
  },
}

export { resolveBatch, streamDirective } from './client'
export type { HealthState }
