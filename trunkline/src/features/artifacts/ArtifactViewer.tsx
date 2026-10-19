import { useEffect, useId, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Brackets } from '@/components/Brackets'
import { Disclose, Fold } from '@/components/Fold'
import { Icon } from '@/components/Icon'
import { cardKind } from '@/components/text'
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
  const [collapsed, setCollapsed] = useState(false)
  const folded = Boolean(threadId) && collapsed
  const header = useRef<HTMLElement>(null)
  const contentId = useId()
  const view = useQuery({
    queryKey: ['artifact-view', url, revision], queryFn: ({ signal }) => fetchArtifact(url, signal),
    retry: false, gcTime: 0, staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false,
  })
  const wide = view.data?.artifact.kind === 'html' && view.data.artifact.mode === 'wide'
  const title = view.data?.artifact.title ?? 'Artifact'
  return <section className="trk-artifact" data-wide={wide ? '' : undefined} data-folded={folded ? '' : undefined} aria-label="Artifact">
    <Brackets />
    <header ref={header} className="trk-artifact-header">
      {threadId ? <button className="trk-artifact-toggle" type="button" aria-label={folded ? 'Unfold' : 'Fold'} aria-expanded={!folded} aria-controls={contentId} onClick={() => {
        setCollapsed(!folded)
        if (folded) requestAnimationFrame(() => header.current?.scrollIntoView({ block: 'start', behavior: 'instant' }))
      }}>
        <Disclose open={!folded} />
      </button> : <span className="trk-artifact-disclosure" aria-hidden="true"><Disclose open /></span>}
      <span className="trk-artifact-heading" title={title}>
        <Icon name="file" />
        <span style={cardKind}>Artifact</span>
        <strong>{title}</strong>
      </span>
      <span className="trk-artifact-controls">
        {threadId && view.data && <a href={view.data.viewer_path} target="_blank" rel="noopener noreferrer"><span className="trk-artifact-icon" aria-hidden="true">↗</span>Open</a>}
        <button type="button" onClick={() => setRevision((value) => value + 1)}>
          <span className="trk-artifact-icon" aria-hidden="true">↻</span>Reload
        </button>
      </span>
    </header>
    <div id={contentId} inert={folded} aria-hidden={folded || undefined} onTransitionEnd={(event) => {
      if (!folded && event.target === event.currentTarget.firstElementChild && event.propertyName === 'grid-template-rows') {
        header.current?.scrollIntoView({ block: 'start', behavior: 'instant' })
      }
    }}>
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
