import { useEffect, useId, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Disclose, Fold } from '@/components/Fold'
import { fetchArtifact, type ArtifactView } from './api'

function ArtifactSkeleton() {
  return <div className="trk-artifact-loading lt-skeleton" role="status" aria-label="Loading artifact" />
}

function ArtifactFrame({ view }: { view: ArtifactView }) {
  const container = useRef<HTMLDialogElement>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error' | 'closed'>('loading')
  const [error, setError] = useState('')
  useEffect(() => {
    const element = container.current
    if (!element) return
    let cancelled = false
    let dispose: (() => void) | undefined
    // Load the host SDK only when an artifact is displayed.
    void import('./runtime').then(({ mountArtifact }) => {
      if (cancelled) return
      dispose = mountArtifact(element, view, () => setStatus('ready'), (message) => {
        setError(message)
        setStatus('error')
      }, () => setStatus('closed'))
    }).catch(() => { if (!cancelled) { setError('The artifact viewer could not be loaded.'); setStatus('error') } })
    return () => { cancelled = true; dispose?.() }
  }, [view])
  const loading = status === 'loading'
  return <div className="trk-artifact-stage" data-loading={loading ? '' : undefined} aria-busy={loading}>
    {loading && <ArtifactSkeleton />}
    {status === 'error' && <p role="alert">{error}</p>}
    {status === 'closed' && <p role="status">Artifact closed. Reload to reopen.</p>}
    <dialog ref={container} open className="trk-artifact-frame" aria-label={view.artifact.title} inert={loading} aria-hidden={loading || undefined} />
  </div>
}

/** Shared by standalone pages and ledger references; reload never executes a tool. */
export function ArtifactViewer({ artifactId, threadId, messageId }: {
  artifactId: string
  threadId?: string
  messageId?: string
}) {
  const url = threadId && messageId
    ? `/api/trunkline/threads/${encodeURIComponent(threadId)}/artifacts/${encodeURIComponent(messageId)}`
    : `/api/artifacts/${encodeURIComponent(artifactId)}`
  const [revision, setRevision] = useState(0)
  const [folded, setFolded] = useState(false)
  const contentId = useId()
  const view = useQuery({
    queryKey: ['artifact-view', url, revision], queryFn: ({ signal }) => fetchArtifact(url, signal),
    retry: false, gcTime: 0, staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false,
  })
  const inlineHtml = threadId && view.data?.artifact.kind === 'html'
  const wide = view.data?.artifact.kind === 'html' && view.data.artifact.mode === 'wide'
  return <section className={`trk-artifact${inlineHtml ? ' trk-artifact-inline-html' : ''}`} data-wide={wide ? '' : undefined} data-folded={folded ? '' : undefined} aria-label="Artifact">
    <header className="trk-artifact-header">
      <strong>{view.data?.artifact.title ?? 'Artifact'}</strong>
      <span className="trk-artifact-controls">
        {threadId && view.data && <a href={view.data.viewer_path} target="_blank" rel="noopener noreferrer"><span className="trk-artifact-icon" aria-hidden="true">↗</span>Open</a>}
        <button type="button" onClick={() => setRevision((value) => value + 1)}>
          <span className="trk-artifact-icon" aria-hidden="true">↻</span>Reload
        </button>
        <button type="button" aria-expanded={!folded} aria-controls={contentId} onClick={() => setFolded((value) => !value)}>
          {folded ? 'Unfold' : 'Fold'}
          <span className="trk-artifact-icon" aria-hidden="true"><Disclose open={!folded} /></span>
        </button>
      </span>
    </header>
    <div id={contentId} inert={folded} aria-hidden={folded || undefined}>
      <Fold open={!folded}>
        <div className="trk-artifact-sheet">
          {view.isPending && <ArtifactSkeleton />}
          {view.error && <p role="alert">{view.error.message}</p>}
          {view.data && <ArtifactFrame key={`${url}:${revision}`} view={view.data} />}
        </div>
      </Fold>
    </div>
  </section>
}
