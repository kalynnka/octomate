import { useQuery } from '@tanstack/react-query'
import { api } from './index'

export const useChannels = () =>
  useQuery({ queryKey: ['channels'], queryFn: api.listChannels, staleTime: Infinity })

export const useThreads = () =>
  useQuery({ queryKey: ['threads'], queryFn: api.listThreads, staleTime: Infinity })

export const useProjects = () =>
  useQuery({ queryKey: ['projects'], queryFn: api.listProjects, staleTime: Infinity })

export const useSurfaces = () =>
  useQuery({ queryKey: ['surfaces'], queryFn: api.getSurfaces, staleTime: Infinity })

export const useControlData = () =>
  useQuery({ queryKey: ['control'], queryFn: api.getControlData, staleTime: Infinity })

export const useStatusData = () =>
  useQuery({ queryKey: ['status'], queryFn: api.getStatus, staleTime: Infinity })

export const useHealth = () =>
  useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15_000 })

export const useConfigure = () =>
  useQuery({ queryKey: ['configure'], queryFn: api.configure, staleTime: 60_000 })

export const useRoutes = () =>
  useQuery({ queryKey: ['routes'], queryFn: api.routes, staleTime: 60_000 })
