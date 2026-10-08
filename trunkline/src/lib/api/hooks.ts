import { useQuery } from '@tanstack/react-query'
import { fetchApiKeys, fetchProfileAuthorizations } from './auth'
import { fetchAddresses, fetchCommandCatalog, fetchThreadOperations } from './client'
import type { CommandContextBody } from './events'
import { api } from './index'

export const useChannels = () =>
  useQuery({ queryKey: ['channels'], queryFn: api.listChannels, staleTime: Infinity })

// Threads arrive without the console asking: a native session is tailed in
// through the hooks, and an IM turn lands on its own channel. Until a standing
// stream exists (README's gap list) the listing is re-read on a timer, so a
// session that started after this page did still shows up in the rail.
export const useThreads = () =>
  useQuery({ queryKey: ['threads'], queryFn: api.listThreads, refetchInterval: 10_000 })

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
