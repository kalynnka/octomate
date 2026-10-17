import { useEffect, useId, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Disclose, Fold } from '@/components/Fold'
import { fetchArtifact, type ArtifactView } from './api'

function ArtifactFrame({ view }: { view: ArtifactView }) {
  const container = useRef<HTMLDialogElement>(null)
  const [status, setStatus] = useState('Loading artifact…')
  const [error, setError] = useState(false)
  useEffect(() => {
    const element = container.current
    if (!element) return
    let cancelled = false
    let dispose: (() => void) | undefined
    // Load the host SDK only when an artifact is displayed.
    void import('./runtime').then(({ mountArtifact }) => {
      if (cancelled) return
      dispose = mountArtifact(element, view, () => setStatus(''), (message) => {
        setError(true)
        setStatus(message)
      }, () => setStatus('Artifact closed. Reload to reopen.'))
    }).catch(() => { if (!cancelled) { setError(true); setStatus('The artifact viewer could not be loaded.') } })
    return () => { cancelled = true; dispose?.() }
  }, [view])
  return <>
    {status && <p role={error ? 'alert' : 'status'}>{status}</p>}
    <dialog ref={container} open className="trk-artifact-frame" aria-label={view.artifact.title} />
  </>
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
  return <section className="trk-artifact" aria-label="Artifact">
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
          {view.isPending && <p role="status">Loading artifact…</p>}
          {view.error && <p role="alert">{view.error.message}</p>}
          {view.data && <ArtifactFrame key={`${url}:${revision}`} view={view.data} />}
        </div>
      </Fold>
    </div>
  </section>
}
