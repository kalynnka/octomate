import { useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchApiKeys, fetchProfileAuthorizations } from './auth'
import { fetchAddresses, fetchCommandCatalog, threadConversations, fetchThreadOperations, modelMessagePage } from './client'
import type { CommandContextBody } from './events'
import { api } from './index'
import { groupLiveThreads, nestedSessions, resolveSpawnNames, type UnnamedSpawn } from './live'

export const useChannels = () =>
  useQuery({ queryKey: ['channels'], queryFn: api.listChannels, staleTime: Infinity })

export const useThreads = () =>
  useQuery({ queryKey: ['threads'], queryFn: api.listThreads, select: groupLiveThreads, staleTime: Infinity })

/** The sessions nested under one thread's sidebar row. A running thread spawns
 *  and settles them as it goes, so it is asked again while it runs. */
export function useNestedSessions(threadId: string, enabled: boolean, running: boolean, showNames = false) {
  const client = useQueryClient()
  const metadata = useQuery({
    ...threadConversations(threadId),
    enabled,
    retry: false,
    refetchInterval: running ? 5_000 : false,
  })
  const targets = useMemo(() => {
    const rows = metadata.data ?? []
    const children = rows.filter(row => row.subagent_id)
    const childIds = new Set(children.map(child => child.id))
    const missing: UnnamedSpawn[] = []
    for (const child of children) {
      if (child.name) continue
      const parent = child.parent_conversation_id
      const scope = parent && childIds.has(parent) ? parent : undefined
      for (const run of child.runs) {
        if (run.parent_run_id && run.parent_tool_call_id) {
          missing.push({ id: child.id, scope, run: run.parent_run_id, call: run.parent_tool_call_id })
        }
      }
    }
    return missing
  }, [metadata.data])
  const names = useQuery({
    queryKey: ['subagent-titles', threadId, targets],
    enabled: enabled && showNames && targets.length > 0,
    staleTime: running ? 5_000 : Infinity,
    refetchInterval: query => running && showNames && targets.some(target => !query.state.data?.[target.id]) ? 5_000 : false,
    queryFn: async () => {
      const titles: Record<string, string> = {}
      for (const scope of new Set(targets.map(target => target.scope))) {
        let pending = targets.filter(target => target.scope === scope)
        let cursor: string | undefined
        while (pending.length) {
          const query = modelMessagePage(threadId, cursor, scope)
          const page = await client.fetchQuery({ ...query, staleTime: running && !cursor ? 5_000 : Infinity })
          if (!page) break
          pending = resolveSpawnNames(page.items, pending, titles)
          if (!page.has_more) break
          cursor = page.next_cursor
        }
      }
      return titles
    },
  })
  const data = useMemo(() => nestedSessions(metadata.data ?? [], names.data), [metadata.data, names.data])
  return { data, hasData: metadata.data !== undefined, loading: metadata.isLoading, refetch: metadata.refetch }
}

export function useThreadSummary(threadId: string) {
  const { data } = useThreads()
  return Object.values(data ?? {}).flat().find(thread => thread.id === threadId)
}

export const useHealth = () =>
  useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    // Chase recovery while the gateway is down or still booting; relax once healthy.
    refetchInterval: (query) => (query.state.data?.ok ? 15_000 : 2_000),
  })

export const useRoutes = () =>
  useQuery({ queryKey: ['routes'], queryFn: api.routes, staleTime: 60_000 })

export const useProjects = () =>
  useQuery({ queryKey: ['projects'], queryFn: api.projects, staleTime: 60_000 })

export const usePermissionModes = () =>
  useQuery({
    queryKey: ['permission-modes'],
    queryFn: api.permissionModes,
    staleTime: 60_000,
  })

/** The signed-in account's API keys, revoked ones included. */
export const useApiKeys = () => useQuery({ queryKey: ['api-keys'], queryFn: fetchApiKeys })

export const useProfileAuthorizations = () =>
  useQuery({ queryKey: ['profile-authorizations'], queryFn: fetchProfileAuthorizations })

// The control page polls live counts; composers reuse its cached model capabilities.
export const useAgents = (enabled = true, live = true) =>
  useQuery({ queryKey: ['agents'], queryFn: api.agents, staleTime: 60_000, refetchInterval: live ? 15_000 : false, enabled })

export const useProfile = () =>
  useQuery({ queryKey: ['profile'], queryFn: api.profile, refetchInterval: 15_000 })

// The agent's own management tools install, enable
// and disable them mid-session, so this page can go stale behind its own console.
export const useMcpServers = () =>
  useQuery({ queryKey: ['mcp-servers'], queryFn: api.mcpServers, refetchInterval: 15_000 })

export const useMcpTentacles = () =>
  useQuery({ queryKey: ['mcp-tentacles'], queryFn: api.mcpTentacles, staleTime: 60_000 })

/** What Teleport and Summon can do from one thread. The header keeps it fresh;
 *  the composer reads the same entry with `enabled` off. */
export const useThreadOperations = (threadId: string, enabled: boolean) =>
  useQuery({
    queryKey: ['thread-operations', threadId],
    queryFn: () => fetchThreadOperations(threadId),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

/** The commands the selected agent offers in the current context. Discovery that
 *  was cut short answers `loading`, so that answer is asked again. */
export const useCommandCatalog = (context: CommandContextBody | null) =>
  useQuery({
    queryKey: ['command-catalog', context?.agent_id, context],
    queryFn: () => fetchCommandCatalog(context!),
    enabled: context !== null,
    retry: false,
    staleTime: 60_000,
    refetchInterval: (query) => (query.state.data?.status === 'loading' ? 2_000 : false),
  })

/** One level of a channel's destinations, fetched when that level is opened. */
export const useAddresses = (threadId: string, channelId: string | undefined, inside: string | undefined) =>
  useQuery({
    queryKey: ['thread-addresses', threadId, channelId, inside ?? null],
    queryFn: () => fetchAddresses(threadId, channelId!, inside),
    enabled: channelId !== undefined,
    retry: false,
    staleTime: 60_000,
  })
