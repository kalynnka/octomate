import { QueryClient } from '@tanstack/react-query'

/** Shared so console actions can invalidate queries after live runs. */
export const queryClient = new QueryClient()
