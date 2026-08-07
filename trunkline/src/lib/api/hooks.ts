import { useQuery } from '@tanstack/react-query'
import { api } from './index'

export const useChannels = () =>
  useQuery({ queryKey: ['channels'], queryFn: api.listChannels, staleTime: Infinity })

export const useThreads = () =>
  useQuery({ queryKey: ['threads'], queryFn: api.listThreads, staleTime: Infinity })

export const useHealth = () =>
  useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15_000 })

export const useRoutes = () =>
  useQuery({ queryKey: ['routes'], queryFn: api.routes, staleTime: 60_000 })
