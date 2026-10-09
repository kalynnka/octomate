/**
 * The console's data source — live against /api/trunkline, no mock
 * stand-ins. An unreachable relay surfaces as an error state (empty ledger,
 * `relay offline` in the status bar). Surfaces whose endpoints do not exist
 * yet (control, status) render their empty states; see
 * README.md's gap list.
 */
import {
  fetchAgents,
  fetchChannels,
  fetchHealth,
  fetchMcpServers,
  fetchMcpTentacles,
  fetchPermissionModes,
  fetchProfile,
  fetchProjects,
  fetchRoutes,
  fetchThread,
  fetchThreadBatches,
  fetchThreadConversations,
  fetchThreadModelMessages,
  fetchThreadProject,
  fetchThreadUnbound,
  fetchThreadUsage,
  fetchThreads,
  patchEffort,
  patchPermissionMode,
} from './client'
import type { HealthState } from './client'
import type {
  ApiConversation,
  ApiPermissionModes,
  ApiProject,
  ApiRoute,
} from './events'
import { channelMeta, groupLiveThreads, liveThreadDetail, type OlderPage, type ThreadRead } from './live'
import type { ChannelMeta, EffortStep, ThreadDetail, ThreadSummary } from './types'

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

  agents: fetchAgents,
  profile: fetchProfile,
  mcpServers: fetchMcpServers,
  mcpTentacles: fetchMcpTentacles,

  /** Switch one conversation's posture; the answer is the row as it now stands. */
  setPermissionMode(conversationId: string, mode: string | null): Promise<ApiConversation> {
    return patchPermissionMode(conversationId, mode)
  },

  /** Set the level one conversation's runs ask for; null hands it back to the runtime. */
  setEffort(conversationId: string, effort: EffortStep | null): Promise<ApiConversation> {
    return patchEffort(conversationId, effort)
  },

  async listThreads(): Promise<Record<string, ThreadSummary[]>> {
    return groupLiveThreads(await fetchThreads())
  },

  /**
   * One thread, read as the relay shapes it: the row, the latest page of its own
   * model messages and of the chat rows none of them carries, its conversations,
   * its project, its waiting feelers and what it cost. Seven requests in parallel
   * rather than one fat payload — the two pages are the only large ones, and they
   * come a page at a time.
   */
  async getThreadDetail(id: string): Promise<ThreadDetail> {
    const [thread, model, unbound, conversations, project, batches, usage] = await Promise.all([
      fetchThread(id),
      fetchThreadModelMessages(id),
      fetchThreadUnbound(id),
      fetchThreadConversations(id),
      fetchThreadProject(id),
      fetchThreadBatches(id),
      fetchThreadUsage(id),
    ])
    if (thread === null || model === null || unbound === null) {
      throw new Error(`thread ${id} not found on the relay`)
    }
    return liveThreadDetail({
      thread,
      modelMessages: [...model.items].reverse(),
      unbound: [...unbound.items].reverse(),
      cursors: {
        model: model.has_more ? model.next_cursor : null,
        unbound: unbound.has_more ? unbound.next_cursor : null,
      },
      totals: { model: model.total, unbound: unbound.total },
      conversations: conversations ?? [],
      project: project ?? null,
      batches: batches ?? [],
      usage: usage ?? {},
    })
  },

  /** The next page of one of a thread's reads; `withOlderPage` folds it on. */
  async olderPage(id: string, read: ThreadRead, cursor: string): Promise<OlderPage> {
    if (read === 'model') {
      const page = await fetchThreadModelMessages(id, cursor)
      if (page !== null) return { read, page }
    } else {
      const page = await fetchThreadUnbound(id, cursor)
      if (page !== null) return { read, page }
    }
    throw new Error(`thread ${id} not found on the relay`)
  },
}

export { resolveBatch, streamDirective } from './client'
export type { HealthState }
