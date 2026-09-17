import { useEffect, useState } from 'react'
import { Button } from '@/components/Button'
import { label, mono, serif } from '@/components/text'
import {
  confirmLinkProfile,
  inspectLinkProfile,
  refusalText,
  type ApiLinkProfile,
  type ApiUser,
} from '@/lib/api/auth'
import type { ApiUserProfile } from '@/lib/api/events'
import { channelMeta } from '@/lib/api/live'
import { queryClient } from '@/lib/queryClient'
import { useAuth } from '@/state/auth'
import { AuthPage, Notice, Refusal } from './parts'

export function LinkProfileDetails({ profile, user }: { profile: ApiUserProfile; user: ApiUser | null }) {
  const channel = channelMeta(profile.channel_tentacle_id)
  const displayName = profile.name || profile.nickname
  const fields = [
    ['Requested through', channel.label],
    ['Display name', displayName || 'Name not provided by the channel'],
    ['Nickname', profile.nickname !== displayName ? profile.nickname : null],
    ['Title', profile.title],
    ['Gender', profile.gender],
    ['Age', profile.age],
    ['Octomate account', user ? `${user.name || user.nickname || user.username} (@${user.username})` : null],
  ] as const

  return (
    <>
      <dl className="trk-profile-info" style={{ marginTop: 16 }}>
        {fields.filter(([, value]) => value !== null && value !== '').map(([name, value]) => (
          <div key={name}>
            <dt style={{ ...label(9), color: 'var(--fg-3)' }}>{name}</dt>
            <dd style={{ ...serif(14), color: 'var(--fg-1)' }}>{value}</dd>
          </div>
        ))}
      </dl>
      <details style={{ marginBottom: 24 }}>
        <summary style={{ ...label(9), color: 'var(--fg-3)', cursor: 'pointer' }}>Technical details</summary>
        <dl className="trk-profile-info" style={{ marginBottom: 8 }}>
          <div>
            <dt style={{ ...label(9), color: 'var(--fg-3)' }}>{channel.label} user ID</dt>
            <dd style={{ ...mono(12), color: 'var(--fg-1)' }}>{profile.channel_user_id}</dd>
          </div>
        </dl>
        <p style={{ ...serif(12), color: 'var(--fg-3)' }}>
          Assigned by {channel.label}, not an Octomate internal profile ID.
        </p>
      </details>
    </>
  )
}

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
      title={linked ? 'Profile linked' : 'Authorize profile'}
      sub={`${channel?.label ?? 'Channel'} → Octomate · link profile`}
      width={440}
    >
      {linked ? (
        <>
          <Notice tone="sage">
            {channel?.label ?? 'Channel'} profile {profile?.name || profile?.nickname || ''}{' '}
            now belongs to {user?.name || user?.nickname || user?.username}.
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
                This channel is requesting authorization from Octomate. Review
                the profile and the signed-in account before linking them.
              </p>
              <LinkProfileDetails profile={profile} user={user} />
              <Notice tone="gold">
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  <li>No access token is sent to the channel.</li>
                  <li>Nothing is linked until you approve.</li>
                </ul>
              </Notice>
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
              {busy ? 'Linking…' : 'Authorize and link →'}
            </Button>
          </div>
        </>
      )}
    </AuthPage>
  )
}
