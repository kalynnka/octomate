/**
 * The door: restores the session the cookies hold, and until someone is in,
 * shows the login or registration page in the console's place. When a session
 * ends — signed out, or lapsed under a refused request — the shell unmounts
 * and the next operator boots into their own threads, not the last one's.
 */
import { useEffect, type ReactNode } from 'react'
import { statusNote } from '@/components/text'
import { queryClient } from '@/lib/queryClient'
import { useAuth } from '@/state/auth'
import { useConsole } from '@/state/console'
import { LoginPage } from './LoginPage'
import { RegisterPage } from './RegisterPage'

function Booting() {
  return (
    <div
      className="paper-texture"
      style={{
        minHeight: 'calc(100vh / var(--trk-zoom, 1))',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 12,
        background: 'var(--page-bg)',
      }}
    >
      <span className="lt-dots">
        <i />
        <i />
        <i />
      </span>
      <span style={{ ...statusNote, color: 'var(--fg-3)' }}>restoring session</span>
    </div>
  )
}

export function AuthGate({ children }: { children: ReactNode }) {
  const status = useAuth((s) => s.status)
  const page = useAuth((s) => s.page)
  const { boot, takeInvitation } = useAuth((s) => s.actions)
  const { signedOut } = useConsole((s) => s.actions)
  useEffect(() => {
    void boot()
    window.addEventListener('hashchange', takeInvitation)
    return () => window.removeEventListener('hashchange', takeInvitation)
  }, [boot, takeInvitation])
  // After the shell has unmounted, not before: a query still mounted would
  // refetch into a cache just cleared, and hand the next operator its rows.
  useEffect(() => {
    if (status !== 'signed-out') return
    queryClient.clear()
    signedOut()
  }, [status, signedOut])
  if (status === 'booting') return <Booting />
  if (status === 'signed-out') return page === 'register' ? <RegisterPage /> : <LoginPage />
  return children
}
