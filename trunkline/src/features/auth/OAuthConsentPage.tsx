import { useEffect, useState } from 'react'
import { Button } from '@/components/Button'
import { label, mono, serif } from '@/components/text'
import {
  ApiError,
  decideOAuthConsent,
  inspectOAuthConsent,
  refusalText,
  type ApiKeyScope,
  type ApiOAuthConsent,
} from '@/lib/api/auth'
import { AuthPage, Refusal } from './parts'

const scopeNames: Record<ApiKeyScope, string> = {
  mcp: 'Use tools and conversations through MCP',
  hooks: 'Upload agent sessions and send hooks',
  acp: 'Connect through ACP',
}

export function OAuthConsentPage({ ticket }: { ticket: string }) {
  const [pending, setPending] = useState<ApiOAuthConsent | null>(null)
  const [decision, setDecision] = useState<'approve' | 'deny' | null>(null)
  const [error, setError] = useState<string | null>(ticket ? null : 'This request is missing. Start again from your application.')

  useEffect(() => {
    if (!ticket) return
    let active = true
    void inspectOAuthConsent(ticket).then(
      details => { if (active) setPending(details) },
      caught => { if (active) setError(refusalText(caught) ?? 'The server did not answer. Try again from your application.') },
    )
    return () => { active = false }
  }, [ticket])

  const decide = async (approve: boolean) => {
    if (!pending || decision) return
    setDecision(approve ? 'approve' : 'deny')
    setError(null)
    try {
      window.location.assign(await decideOAuthConsent(ticket, pending.user_id, approve))
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        setPending(null)
        try {
          setPending(await inspectOAuthConsent(ticket))
          setError('Your account changed. Review the request again.')
        } catch (refreshError) {
          setError(refusalText(refreshError) ?? 'The server did not answer. Try again from your application.')
        }
      } else {
        setError(refusalText(caught) ?? 'The server did not answer. Try again.')
      }
      setDecision(null)
    }
  }

  return (
    <AuthPage title="Authorize access" sub="Review this request before continuing." width={480}>
      {!pending && !error && <p role="status" style={{ ...serif(13), color: 'var(--fg-2)' }}>Loading request…</p>}
      {pending && <>
        <dl className="trk-profile-info" style={{ marginTop: 16 }}>
          <div>
            <dt style={{ ...label(9), color: 'var(--fg-3)' }}>Application</dt>
            <dd style={{ ...serif(14), color: 'var(--fg-1)', overflowWrap: 'anywhere' }}>{pending.client_name}</dd>
          </div>
          <div>
            <dt style={{ ...label(9), color: 'var(--fg-3)' }}>Signed in as</dt>
            <dd style={{ ...mono(12), color: 'var(--fg-1)' }}>{pending.username}</dd>
          </div>
          <div>
            <dt style={{ ...label(9), color: 'var(--fg-3)' }}>Access requested</dt>
            <dd><ul style={{ ...serif(14), margin: 0, paddingLeft: 18 }}>
              {pending.scopes.map(scope => <li key={scope}>{scopeNames[scope]}</li>)}
            </ul></dd>
          </div>
          <div>
            <dt style={{ ...label(9), color: 'var(--fg-3)' }}>Server</dt>
            <dd style={{ ...mono(12), overflowWrap: 'anywhere' }}>{pending.resource}</dd>
          </div>
          <div>
            <dt style={{ ...label(9), color: 'var(--fg-3)' }}>Return address</dt>
            <dd style={{ ...mono(12), overflowWrap: 'anywhere' }}>{pending.redirect_uri}</dd>
          </div>
        </dl>
        <p style={{ ...serif(12.5), color: 'var(--fg-2)', lineHeight: 1.6 }}>
          The application supplied its name. Only approve if you started this connection and recognize it.
        </p>
        <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 8, marginTop: 20 }}>
          <Button disabled={decision !== null} onClick={() => { void decide(false) }}>
            {decision === 'deny' ? 'Denying…' : 'Deny'}
          </Button>
          <Button variant="accent" disabled={decision !== null} onClick={() => { void decide(true) }}>
            {decision === 'approve' ? 'Authorizing…' : 'Allow access'}
          </Button>
        </div>
      </>}
      {error && <div role="alert"><Refusal>{error}</Refusal></div>}
    </AuthPage>
  )
}
