import { useEffect, useState } from 'react'
import { Button } from '@/components/Button'
import { label, mono, serif } from '@/components/text'
import {
  confirmLinkProfile,
  inspectLinkProfile,
  refusalText,
  type ApiLinkProfile,
} from '@/lib/api/auth'
import { channelMeta } from '@/lib/api/live'
import { queryClient } from '@/lib/queryClient'
import { useAuth } from '@/state/auth'
import { AuthPage, Notice, Refusal } from './parts'

export function LinkProfilePage({ token }: { token: string }) {
  const user = useAuth((s) => s.user)
  const { finishLinkProfile } = useAuth((s) => s.actions)
  const [pending, setPending] = useState<ApiLinkProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [linked, setLinked] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void inspectLinkProfile(token)
      .then((found) => {
        if (active) setPending(found)
      })
      .catch((caught) => {
        if (active) {
          setError(refusalText(caught) ?? 'The relay did not answer. Try again.')
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [token])

  const confirm = async () => {
    if (busy || loading || pending === null || user === null) return
    setBusy(true)
    setError(null)
    try {
      await confirmLinkProfile(token, user.id)
      void queryClient.invalidateQueries({ queryKey: ['profile'] })
      setLinked(true)
    } catch (caught) {
      setError(refusalText(caught) ?? 'The relay did not answer. Try again.')
      setBusy(false)
    }
  }

  const profile = pending?.profile
  const channel = profile ? channelMeta(profile.channel_tentacle_id) : null
  return (
    <AuthPage
      title={linked ? 'Profile linked' : 'Link profile'}
      sub="channel identity · local account"
      width={440}
    >
      {linked ? (
        <>
          <Notice tone="sage">
            {channel?.label ?? 'Channel'} profile {profile?.name || profile?.channel_user_id}{' '}
            now belongs to {user?.name || user?.username}.
          </Notice>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20 }}>
            <Button variant="accent" onClick={finishLinkProfile}>Continue →</Button>
          </div>
        </>
      ) : (
        <>
          {loading && <p style={{ ...serif(13), color: 'var(--fg-3)' }}>Checking the private link…</p>}
          {profile && channel && (
            <>
              <p style={{ ...serif(13), lineHeight: 1.65, color: 'var(--fg-2)' }}>
                Confirm that this channel identity should belong to your signed-in
                Octomate account.
              </p>
              <dl className="trk-profile-info" style={{ marginTop: 16 }}>
                {[
                  ['Channel', channel.label],
                  ['Profile', profile.name || profile.nickname || profile.channel_user_id],
                  ['Channel ID', profile.channel_user_id],
                  ['Octomate account', user?.username ?? ''],
                ].map(([name, value]) => (
                  <div key={name}>
                    <dt style={{ ...label(9), color: 'var(--fg-3)' }}>{name}</dt>
                    <dd style={{ ...mono(12), color: 'var(--fg-1)' }}>{value}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
          {error && <Refusal>{error}</Refusal>}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 20 }}>
            <Button variant="ghost" onClick={finishLinkProfile}>Cancel</Button>
            <Button
              variant="accent"
              disabled={busy || loading || pending === null || user === null || error !== null}
              onClick={confirm}
            >
              {busy ? 'Linking…' : 'Link profile →'}
            </Button>
          </div>
        </>
      )}
    </AuthPage>
  )
}
