import { useQuery } from '@tanstack/react-query'
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
