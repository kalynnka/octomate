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
  fetchLiveThreadDetail,
  fetchLiveThreads,
  fetchRoutes,
} from './client'
import type { HealthState } from './client'
import type { ApiRoute } from './events'
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

  async listThreads(): Promise<Record<string, ThreadSummary[]>> {
    return groupLiveThreads(await fetchLiveThreads())
  },

  async getThreadDetail(id: string): Promise<ThreadDetail> {
    const live = await fetchLiveThreadDetail(id)
    if (live === null) throw new Error(`thread ${id} not found on the relay`)
    return liveThreadDetail(live)
  },
}

export { resolveBatch, streamDirective } from './client'
export type { HealthState }
