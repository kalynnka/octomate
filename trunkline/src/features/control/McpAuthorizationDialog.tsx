import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/Button'
import { Field, Refusal } from '@/features/auth/parts'
import { refusalText } from '@/lib/api/auth'
import { connectMcp, confirmMcp } from '@/lib/api/client'
import type { ApiMcp, ApiMcpAuthorization, OAuthFlowKind } from '@/lib/api/events'
import { useProfile } from '@/lib/api/hooks'
import { queryClient } from '@/lib/queryClient'

export function McpAuthorizationDialog({ mcp, onClose }: { mcp: ApiMcp; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const profile = useProfile()
  const oauth = profile.data?.mcps.find((item) => item.id === mcp.id)?.oauth
  const [flow, setFlow] = useState<OAuthFlowKind | ''>('')
  const [authorization, setAuthorization] = useState<ApiMcpAuthorization | null>(null)
  const [busy, setBusy] = useState(false)
  const [retryAt, setRetryAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const selectedFlow = flow || oauth?.flows[0]
  const ready = oauth?.status === 'active'
  const pending = oauth?.status === 'pending_device' || oauth?.status === 'pending_browser'

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
    if (busy || !selectedFlow) return
    setBusy(true)
    setError(null)
    try {
      const result = await connectMcp(mcp.id, selectedFlow)
      setAuthorization(result)
      setRetryAt('interval_seconds' in result ? Date.now() + result.interval_seconds * 1000 : null)
      await queryClient.invalidateQueries({ queryKey: ['profile'] })
    } catch (caught) {
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
        setAuthorization(null)
        setError('Authorization is not complete. Start again to get a new link.')
      }
      await queryClient.invalidateQueries({ queryKey: ['profile'] })
      if (result.status === 'active') onClose()
    } catch (caught) {
      setError(refusalText(caught) ?? 'Authorization could not be checked.')
    } finally { setBusy(false) }
  }

  return (
    <dialog ref={dialog} className="trk-dialog" aria-labelledby="mcp-authorization-title" onCancel={(event) => {
      if (busy) event.preventDefault()
      else onClose()
    }}>
      <div className="trk-dialog-layout">
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
        <div className="trk-dialog-main">
          <header className="trk-dialog-header">
            <span>Authorization</span>
            <button type="button" className="trk-dialog-close hov-wash" aria-label="Close MCP authorization" disabled={busy} onClick={onClose}>×</button>
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
                  <p className="trk-dialog-hint">{pending ? 'Authorization is pending. Check its status, or get another link to continue.' : 'Continue to authorize this MCP with your provider.'}</p>
                  <div className="trk-dialog-actions">
                    <Button variant="accent" disabled={busy || !selectedFlow} onClick={() => void start()}>{busy ? 'Starting…' : pending ? 'Get authorization link' : 'Continue'}</Button>
                  </div>
                </>
              )}
              {authorization && (
                <>
                  {'user_code' in authorization && (
                    <Field name="Device code">
                      <input className="trk-input" readOnly value={authorization.user_code} onFocus={(event) => event.currentTarget.select()} />
                    </Field>
                  )}
                  <p className="trk-dialog-hint">
                    <a href={'authorization_uri' in authorization ? authorization.authorization_uri : authorization.verification_uri_complete ?? authorization.verification_uri} target="_blank" rel="noopener noreferrer">Open authorization ↗</a>
                  </p>
                  <p className="trk-dialog-hint">Approve access in the opened tab, then check the status here. This link expires at {new Date(authorization.expires_at).toLocaleTimeString()}.</p>
                </>
              )}
              {(authorization || pending) && (
                <div className="trk-dialog-actions">
                  <Button disabled={busy || retryAt !== null} onClick={() => void confirm()}>{busy ? 'Checking…' : retryAt !== null ? 'Wait to check' : 'Check status'}</Button>
                </div>
              )}
            </>
          )}
          {error && <div role="alert"><Refusal>{error}</Refusal></div>}
          <div className="trk-dialog-actions"><Button variant="ghost" disabled={busy} onClick={onClose}>{ready ? 'Done' : 'Close'}</Button></div>
        </div>
      </div>
    </dialog>
  )
}
