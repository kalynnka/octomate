import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button } from '@/components/Button'
import { Field, Refusal } from '@/features/auth/parts'
import { refusalText } from '@/lib/api/auth'
import { cancelMcpAuthorization, connectMcp, confirmMcp, fetchMcpAuthorization } from '@/lib/api/client'
import type { ApiMcp, OAuthFlowKind } from '@/lib/api/events'
import { useProfile } from '@/lib/api/hooks'
import { closeDialog } from '@/lib/dialog'
import { queryClient } from '@/lib/queryClient'
import { useDialogDrag } from '@/lib/useDialogDrag'

export function McpAuthorizationDialog({ mcp, onClose }: { mcp: ApiMcp; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const drag = useDialogDrag(dialog)
  const profile = useProfile()
  const oauth = profile.data?.mcps.find((item) => item.id === mcp.id)?.oauth
  const [flow, setFlow] = useState<OAuthFlowKind | ''>('')
  const authorizationQuery = useQuery({
    queryKey: ['mcp-authorization', mcp.id],
    queryFn: () => fetchMcpAuthorization(mcp.id),
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  })
  const authorization = authorizationQuery.data
  const [copied, setCopied] = useState<'copied' | 'failed' | null>(null)
  const [busy, setBusy] = useState(false)
  const [retryAt, setRetryAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const selectedFlow = flow || oauth?.flows[0]
  const ready = oauth?.status === 'active'
  const pending = oauth?.status === 'pending_device' || oauth?.status === 'pending_browser'
  const close = () => void closeDialog(dialog.current, onClose)

  useEffect(() => {
    setRetryAt(authorization && 'interval_seconds' in authorization
      ? Date.now() + authorization.interval_seconds * 1000 : null)
  }, [authorization])

  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    return () => element.close()
  }, [])

  useEffect(() => {
    if (retryAt === null) return
    const timer = setTimeout(() => setRetryAt(null), Math.max(0, retryAt - Date.now()))
    return () => clearTimeout(timer)
  }, [retryAt])

  const start = async () => {
    if (busy || authorizationQuery.isPending || authorizationQuery.isError || !selectedFlow) return
    // Open browser OAuth during the click to avoid asynchronous popup blocking.
    const authorizationTab = selectedFlow === 'authorization_code' ? window.open('about:blank', '_blank') : null
    if (authorizationTab) authorizationTab.opener = null
    setBusy(true)
    setError(null)
    setCopied(null)
    try {
      const result = await connectMcp(mcp.id, selectedFlow)
      queryClient.setQueryData(['mcp-authorization', mcp.id], result)
      if (authorizationTab && !authorizationTab.closed && 'authorization_uri' in result) {
        authorizationTab.location.replace(result.authorization_uri)
      }
      await queryClient.invalidateQueries({ queryKey: ['profile'] })
    } catch (caught) {
      authorizationTab?.close()
      setError(refusalText(caught) ?? 'Authorization could not be started.')
    } finally { setBusy(false) }
  }

  const confirm = async () => {
    if (busy || retryAt !== null) return
    setBusy(true)
    setError(null)
    try {
      const result = await confirmMcp(mcp.id)
      if (result.status === 'pending_device') setRetryAt(Date.now() + result.retry_after_seconds * 1000)
      if (result.status === null || result.status === 'invalid') {
        queryClient.setQueryData(['mcp-authorization', mcp.id], null)
        setError('Authorization is not complete. Start again to get a new link.')
      }
      await queryClient.invalidateQueries({ queryKey: ['profile'] })
      if (result.status === 'active') await closeDialog(dialog.current, onClose)
    } catch (caught) {
      setError(refusalText(caught) ?? 'Authorization could not be checked.')
    } finally { setBusy(false) }
  }

  const cancel = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await cancelMcpAuthorization(mcp.id)
      queryClient.setQueryData(['mcp-authorization', mcp.id], null)
      await queryClient.invalidateQueries({ queryKey: ['profile'] })
      await closeDialog(dialog.current, onClose)
    } catch (caught) {
      setError(refusalText(caught) ?? 'Authorization could not be cancelled.')
    } finally { setBusy(false) }
  }

  return (
    <dialog ref={dialog} className="trk-dialog" aria-labelledby="mcp-authorization-title" onCancel={(event) => {
      event.preventDefault()
      if (!busy) close()
    }}>
      <div className="trk-dialog-layout" style={{ minHeight: 'min(440px, calc(100dvh / var(--trk-zoom) - 34px))' }} {...drag}>
        <aside className="trk-dialog-panel">
          <span className="trk-dialog-index" aria-hidden="true">03</span>
          <span className="trk-dialog-eyebrow">Account authorization</span>
          <h2 id="mcp-authorization-title">Connect {mcp.name}</h2>
          <dl className="trk-dialog-summary">
            <div><dt>Namespace</dt><dd>{mcp.namespace}</dd></div>
            <div><dt>Status</dt><dd>{ready ? 'Ready' : authorization || pending ? 'Pending' : 'Not connected'}</dd></div>
          </dl>
          <p className="trk-dialog-caption">This authorization belongs to your account and this MCP only.</p>
        </aside>
        <div className="trk-dialog-main" style={{ display: 'flex', flexDirection: 'column' }}>
          <header className="trk-dialog-header">
            <span>Authorization</span>
            <button type="button" className="trk-dialog-close hov-wash" aria-label="Close MCP authorization" disabled={busy} onClick={close}>×</button>
          </header>
          {ready ? <p role="status" className="trk-dialog-hint">Connected and ready to use.</p> : (
            <>
              {!authorization && (
                <>
                  {oauth?.flows.length ? (
                    <Field name="Method">
                      <select className="trk-input" autoFocus disabled={busy} value={selectedFlow} onChange={(event) => {
                        const value = event.target.value
                        if (value === 'device' || value === 'authorization_code') setFlow(value)
                      }}>
                        {oauth.flows.map((kind) => <option key={kind} value={kind}>{kind === 'device' ? 'Device code' : 'Browser'}</option>)}
                      </select>
                    </Field>
                  ) : <p className="trk-dialog-hint">{profile.isFetching ? 'Loading authorization methods…' : 'No authorization method is available.'}</p>}
                  <p className="trk-dialog-hint">{authorizationQuery.isPending ? 'Loading authorization…' : 'Continue to authorize this MCP with your provider.'}</p>
                </>
              )}
              {authorization && (
                <>
                  {'user_code' in authorization && (
                    <>
                      <div className="trk-device-code" style={{ display: 'flex', alignItems: 'flex-end', gap: 8 }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <Field name="Device code">
                            <input className="trk-input" readOnly value={authorization.user_code} onFocus={(event) => event.currentTarget.select()} />
                          </Field>
                        </div>
                        <Button
                          title="Copy device code"
                          style={{ padding: '8px 10px', fontSize: 9, flexShrink: 0 }}
                          onClick={async () => {
                            try {
                              await navigator.clipboard.writeText(authorization.user_code)
                              setCopied('copied')
                            } catch { setCopied('failed') }
                          }}
                        >
                          <span role="status">{copied === 'copied' ? 'Copied ✓' : 'Copy'}</span>
                        </Button>
                      </div>
                      {copied === 'failed' && <p role="alert" className="trk-dialog-hint">Could not copy the code. Select it and copy it manually.</p>}
                    </>
                  )}
                  <p className="trk-dialog-hint">
                    <a href={'authorization_uri' in authorization ? authorization.authorization_uri : authorization.verification_uri_complete ?? authorization.verification_uri} target="_blank" rel="noopener noreferrer">Open authorization ↗</a>
                  </p>
                  <p className="trk-dialog-hint">
                    {'user_code' in authorization
                      ? 'Copy the code and open authorization to approve access, then check the status here.'
                      : 'Approve access in the opened tab, then check the status here.'}
                    {' '}This link expires at {new Date(authorization.expires_at).toLocaleTimeString()}.
                  </p>
                </>
              )}
            </>
          )}
          {error && <div role="alert"><Refusal>{error}</Refusal></div>}
          {authorizationQuery.isError && (
            <div role="alert">
              <Refusal>Pending authorization could not be loaded.</Refusal>
              <Button onClick={() => void authorizationQuery.refetch()}>Try again</Button>
            </div>
          )}
          <div className="trk-dialog-actions" style={{ marginTop: 'auto', paddingTop: 22, flexShrink: 0 }}>
            {!ready && (authorization || pending) && (
              <Button variant="ghost" disabled={busy} onClick={() => void cancel()} style={{ color: 'var(--color-red)' }}>Cancel authorization</Button>
            )}
            <Button variant="ghost" disabled={busy} onClick={close}>{ready ? 'Done' : 'Close'}</Button>
            {!ready && !authorization && (
              <Button variant="accent" disabled={busy || authorizationQuery.isPending || authorizationQuery.isError || !selectedFlow} onClick={() => void start()}>{busy ? 'Starting…' : 'Continue'}</Button>
            )}
            {!ready && (authorization || pending) && (
              <Button disabled={busy || retryAt !== null} onClick={() => void confirm()}>{busy ? 'Checking…' : retryAt !== null ? 'Wait to check' : 'Check status'}</Button>
            )}
          </div>
        </div>
      </div>
    </dialog>
  )
}
