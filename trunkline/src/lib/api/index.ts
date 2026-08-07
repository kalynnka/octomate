/**
 * The console's data source. The trunkline channel is live: its threads,
 * ledgers, routes, and runs come from /api/trunkline (falling back to the mock
 * dataset when the relay is unreachable). Every other channel is served by the
 * mock adapter until its endpoint exists — see the API-gap list in README.md.
 */
import {
  fetchHealth,
  fetchLiveThreadDetail,
  fetchLiveThreads,
  fetchRoutes,
} from './client'
import type { HealthState } from './client'
import type { ApiRoute } from './events'
import { groupLiveThreads, liveThreadDetail } from './live'
import { mockApi } from './mock/api'
import type { ConfigureModel, ThreadDetail, ThreadSummary } from './types'

export interface ConfigureResult {
  models: ConfigureModel[]
  source: 'live' | 'mock'
}

export interface RoutesResult {
  routes: ApiRoute[]
  source: 'live' | 'mock'
}

export const api = {
  ...mockApi,

  health(): Promise<HealthState> {
    return fetchHealth()
  },

  /** The agent-model routes the composer picker offers. */
  async routes(): Promise<RoutesResult> {
    try {
      return { routes: await fetchRoutes(), source: 'live' }
    } catch {
      return {
        routes: mockApi.mockModels().map((model) => {
          const [agent = 'inkling', ...rest] = model.id.split(':')
          return { id: model.id, agent, model: rest.join(':') || model.id }
        }),
        source: 'mock',
      }
    }
  },

  async configure(): Promise<ConfigureResult> {
    const { routes, source } = await api.routes()
    return {
      models: routes.map((route) => ({
        id: route.id,
        name: `${route.agent} · ${route.model}`,
      })),
      source,
    }
  },

  async listThreads(): Promise<Record<string, ThreadSummary[]>> {
    // The console reads every channel's real threads; the mock dataset only
    // stands in when the relay is unreachable.
    try {
      return groupLiveThreads(await fetchLiveThreads())
    } catch {
      return mockApi.listThreads()
    }
  },

  async getThreadDetail(id: string): Promise<ThreadDetail> {
    try {
      const live = await fetchLiveThreadDetail(id)
      if (live !== null) return liveThreadDetail(live)
    } catch {
      // Relay unreachable — fall through to the mock ledger.
    }
    return mockApi.getThreadDetail(id)
  },
}

export { resolveBatch, streamDirective } from './client'
export type { HealthState }
