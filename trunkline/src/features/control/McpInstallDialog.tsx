import { useEffect, useRef, useState, type SubmitEvent } from 'react'
import { Button } from '@/components/Button'
import { Field, Refusal } from '@/features/auth/parts'
import { refusalText } from '@/lib/api/auth'
import { installMcp } from '@/lib/api/client'
import type { ApiMcp, McpAuthKind, McpInstallBody } from '@/lib/api/events'
import { useMcpTentacles } from '@/lib/api/hooks'
import { queryClient } from '@/lib/queryClient'

export function McpInstallDialog({ onClose, onInstalled }: {
  onClose: () => void
  onInstalled: (mcp: ApiMcp) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const tentaclesQuery = useMcpTentacles()
  const [source, setSource] = useState('')
  const [name, setName] = useState('')
  const [namespace, setNamespace] = useState('')
  const [url, setUrl] = useState('')
  const [auth, setAuth] = useState<McpAuthKind>('oauth')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const tentacle = tentaclesQuery.data?.find((item) => item.id === source)
  const endpoint = tentacle?.url ?? url.trim()
  const authKind = tentacle?.auth_kind ?? auth

  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    return () => element.close()
  }, [])

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || (source && !tentacle)) return
    setBusy(true)
    setError(null)
    const body: McpInstallBody = tentacle
      ? { name: name.trim(), namespace, url: tentacle.url, tentacle_id: tentacle.id }
      : {
          name: name.trim(), namespace, url: endpoint,
          auth: auth === 'bearer' ? { kind: auth, token } : { kind: auth },
        }
    try {
      const mcp = await installMcp(body)
      setToken('')
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      void queryClient.invalidateQueries({ queryKey: ['profile'] })
      onInstalled(mcp)
    } catch (caught) {
      setError(refusalText(caught) ?? 'The MCP could not be installed. Try again.')
      setBusy(false)
    }
  }

  return (
    <dialog
      ref={dialog}
      className="trk-dialog"
      aria-labelledby="mcp-install-title"
      aria-describedby="mcp-install-scope"
      onCancel={(event) => {
        if (busy) event.preventDefault()
        else onClose()
      }}
    >
      <div className="trk-dialog-layout">
        <aside className="trk-dialog-panel">
          <span className="trk-dialog-index" aria-hidden="true">03</span>
          <span className="trk-dialog-eyebrow">New connection</span>
          <h2 id="mcp-install-title">Install MCP</h2>
          <p id="mcp-install-scope">Private to your account. Use a distinct namespace for each workspace.</p>
          <dl className="trk-dialog-summary">
            <div><dt>Source</dt><dd>{tentacle?.name ?? 'Custom'}</dd></div>
            <div><dt>Auth</dt><dd>{authKind === 'oauth' ? 'OAuth' : authKind === 'bearer' ? 'Bearer token' : 'None'}</dd></div>
            <div><dt>Namespace</dt><dd>{namespace ? `personal/${namespace}` : '—'}</dd></div>
          </dl>
          <div className="trk-dialog-caption">
            <span>Endpoint</span>
            <p>{endpoint || 'Your MCP server’s HTTPS URL'}</p>
          </div>
        </aside>
        <div className="trk-dialog-main">
          <header className="trk-dialog-header">
            <span>Connection details</span>
            <button type="button" className="trk-dialog-close hov-wash" aria-label="Close MCP installation" disabled={busy} onClick={onClose}>×</button>
          </header>
          <form onSubmit={submit} aria-busy={busy}>
            <fieldset className="trk-dialog-fields" disabled={busy}>
              <Field name="Source">
                <select
                  className="trk-input"
                  autoFocus
                  value={source}
                  onChange={(event) => {
                    const id = event.target.value
                    const selected = tentaclesQuery.data?.find((item) => item.id === id)
                    setSource(id)
                    setName(selected?.name ?? '')
                    setNamespace(selected?.id ?? '')
                    setToken('')
                    setError(null)
                  }}
                >
                  <option value="">Custom Endpoint</option>
                  {tentaclesQuery.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select>
              </Field>
              {tentaclesQuery.isPending && <p className="trk-dialog-hint" role="status">Loading configured tentacles…</p>}
              {tentaclesQuery.isError && <Refusal>Configured tentacles could not be loaded. You can still install a custom endpoint.</Refusal>}
              <Field name="Name">
                <input className="trk-input" name="name" required maxLength={100} value={name} placeholder="Linear · Work" onChange={(event) => setName(event.target.value)} />
              </Field>
              <Field name="Namespace">
                <input className="trk-input" name="namespace" required maxLength={64} pattern="[a-z][a-z0-9_\-]{0,63}" title="Start with a lowercase letter; use lowercase letters, digits, underscores or hyphens." autoCapitalize="none" spellCheck={false} value={namespace} placeholder="linear_work" onChange={(event) => setNamespace(event.target.value)} />
              </Field>
              <p className="trk-dialog-hint">A unique name for your tools. It cannot be changed after installation.</p>
              <Field name="Endpoint">
                <input className="trk-input" type="url" name="url" required maxLength={2083} pattern="https://.*" title="Use the MCP server’s HTTPS URL." autoCapitalize="none" spellCheck={false} readOnly={!!tentacle} value={tentacle?.url ?? url} placeholder="https://mcp.example.com/mcp" onChange={(event) => setUrl(event.target.value)} />
              </Field>
              {!tentacle && <Field name="Authentication">
                <select className="trk-input" value={auth} onChange={(event) => {
                  const kind = event.target.value
                  if (kind === 'none' || kind === 'bearer' || kind === 'oauth') setAuth(kind)
                  setToken('')
                }}>
                  <option value="none">None</option>
                  <option value="bearer">Bearer token</option>
                  <option value="oauth">OAuth</option>
                </select>
              </Field>}
              {!tentacle && auth === 'bearer' && <Field name="Bearer token">
                <input className="trk-input" type="password" name="token" required maxLength={16384} autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} />
              </Field>}
              {tentacle && <p className="trk-dialog-hint">The selected tentacle supplies its endpoint and authentication settings.</p>}
              {authKind === 'oauth' && <p className="trk-dialog-hint">You’ll authorize your account in the next step.</p>}
            </fieldset>
            {error && <div role="alert"><Refusal>{error}</Refusal></div>}
            <div className="trk-dialog-actions">
              <Button variant="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
              <Button type="submit" variant="accent" disabled={busy || !name.trim() || !namespace || !endpoint || (!!source && !tentacle)}>
                {busy ? 'Installing…' : 'Install MCP'}
              </Button>
            </div>
          </form>
        </div>
      </div>
    </dialog>
  )
}
