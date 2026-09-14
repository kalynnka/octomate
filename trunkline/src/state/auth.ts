/**
 * Session state — who is signed in, and which of the signed-out pages is
 * showing when nobody is. The cookies themselves are the relay's; this store
 * only knows what /api/auth/me last said, and turns a refused request into the
 * login page with a word on why.
 */
import { create } from 'zustand'
import {
  ApiError,
  changePassword,
  fetchMe,
  login,
  logout,
  onSessionLost,
  register,
  type ApiUser,
  type PasswordBody,
  type RegistrationBody,
} from '@/lib/api/auth'

export type AuthStatus = 'booting' | 'signed-out' | 'signed-in'
export type AuthPage = 'login' | 'register'
/** What the relay said when the console last knocked: taking accounts, refusing
 *  them because auth.yaml is missing, or not answering at all. */
export type RelayState = 'ok' | 'unconfigured' | 'offline'

export interface AuthActions {
  /** restore the session the cookies hold, or land on a signed-out page */
  boot(): Promise<void>
  /** read a one-time entry link off the address bar, on load and hash changes */
  takeEntryLink(): void
  /** throws the ApiError for the form to word; the relay state is kept here */
  signIn(username: string, password: string): Promise<void>
  register(body: RegistrationBody): Promise<void>
  signOut(): Promise<void>
  changePassword(body: PasswordBody): Promise<void>
  goRegister(): void
  goLogin(): void
  finishLinkProfile(): void
}

interface AuthState {
  status: AuthStatus
  user: ApiUser | null
  page: AuthPage
  /** the invitation a registration link carried in its fragment; '' when none did */
  invitation: string
  /** the private profile-linking ticket being carried through sign-in */
  linkProfile: string
  /** why the login page is back — a session that lapsed — or null on a first visit */
  notice: string | null
  relay: RelayState
  actions: AuthActions
}

/** What a refusal says about the relay: a 503 is auth left unconfigured, any
 *  other 5xx or a transport failure is a relay that did not answer. */
function relayOf(error: unknown): RelayState {
  if (!(error instanceof ApiError)) return 'offline'
  if (error.status === 503) return 'unconfigured'
  return error.status >= 500 ? 'offline' : 'ok'
}

export const useAuth = create<AuthState>()((set, get) => {
  // A refusal that survived a refresh is only news while someone is signed in;
  // the first visit's 401 is the login page being the right answer.
  onSessionLost(() => {
    if (get().status !== 'signed-in') return
    set({
      status: 'signed-out',
      user: null,
      page: 'login',
      notice: 'Your session expired — sign in again.',
    })
  })

  const actions: AuthActions = {
    // A secret is struck from the address bar as it is read, so it outlives
    // neither the in-memory ceremony nor a screenshot of the URL.
    takeEntryLink() {
      const params = new URLSearchParams(window.location.hash.slice(1))
      const invitation = params.get('invitation')
      const linkProfile = params.get('link-profile')
      if (!invitation && !linkProfile) return
      history.replaceState(null, '', window.location.pathname + window.location.search)
      if (linkProfile) {
        set({
          linkProfile,
          page: 'login',
          notice: 'Sign in to choose the account for this channel profile.',
        })
      } else if (invitation) {
        set({ invitation, page: 'register' })
      }
    },

    async boot() {
      actions.takeEntryLink()
      try {
        const user = await fetchMe()
        set({ status: 'signed-in', user, relay: 'ok' })
      } catch (error) {
        set({ status: 'signed-out', user: null, relay: relayOf(error) })
      }
    },

    async signIn(username, password) {
      try {
        await login({ username, password })
        const user = await fetchMe()
        set({ status: 'signed-in', user, notice: null, relay: 'ok' })
      } catch (error) {
        set({ relay: relayOf(error) })
        throw error
      }
    },

    async register(body) {
      try {
        const user = await register(body)
        set({ status: 'signed-in', user, notice: null, relay: 'ok', invitation: '' })
      } catch (error) {
        set({ relay: relayOf(error) })
        throw error
      }
    },

    async signOut() {
      await logout()
      set({ status: 'signed-out', user: null, page: 'login', notice: null })
    },

    async changePassword(body) {
      await changePassword(body)
      set({
        status: 'signed-out', user: null, page: 'login',
        notice: 'Password changed. Sign in with your new password.',
      })
    },

    goRegister: () => set({ page: 'register' }),
    goLogin: () => set({ page: 'login' }),
    finishLinkProfile: () => set({ linkProfile: '', notice: null }),
  }

  return {
    status: 'booting',
    user: null,
    page: 'login',
    invitation: '',
    linkProfile: '',
    notice: null,
    relay: 'ok',
    actions,
  }
})
