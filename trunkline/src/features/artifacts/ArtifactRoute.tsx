import { lazy, Suspense, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { statusNote } from '@/components/text'
import { ConsoleShell } from '@/features/shell/ConsoleShell'
import { fetchArtifactConfig } from './api'

const ArtifactViewer = lazy(() => import('./ArtifactViewer').then(module => ({ default: module.ArtifactViewer })))

const subscribe = (changed: () => void) => {
  window.addEventListener('popstate', changed)
  return () => window.removeEventListener('popstate', changed)
}

/** The configured viewer prefix is resolved before mounting the console router. */
export function ArtifactRoute() {
  const path = useSyncExternalStore(subscribe, () => window.location.pathname)
  const config = useQuery({ queryKey: ['artifact-config'], queryFn: fetchArtifactConfig, staleTime: Infinity, retry: false })
  if (config.error) return <p role="alert" style={{ ...statusNote, color: 'var(--color-terra)' }}>{config.error.message}</p>
  if (!config.data) return <p role="status" style={{ ...statusNote, color: 'var(--fg-3)' }}>Loading…</p>
  if (!path.startsWith(`${config.data.path}/`)) return <ConsoleShell />
  const id = path.slice(config.data.path.length + 1)
  return <main className="trk-artifact-page">
    <a href="/">← Trunkline</a>
    <Suspense fallback={<p role="status" style={{ ...statusNote, color: 'var(--fg-3)' }}>Loading artifact…</p>}><ArtifactViewer key={id} artifactId={id} /></Suspense>
  </main>
}
