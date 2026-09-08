/**
 * Session state — who is signed in, and which of the signed-out pages is
 * showing when nobody is. The cookies themselves are the relay's; this store
 * only knows what /api/auth/me last said, and turns a refused request into the
 * login page with a word on why.
 */
import { create } from 'zustand'
import {
  ApiError,
  fetchMe,
  login,
  logout,
  onSessionLost,
  register,
  type ApiUser,
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
  /** read an invitation off the address bar, on load and on a hash change — a
   *  link pasted into the console's own tab reloads nothing */
  takeInvitation(): void
  /** throws the ApiError for the form to word; the relay state is kept here */
  signIn(username: string, password: string): Promise<void>
  register(body: RegistrationBody): Promise<void>
  signOut(): Promise<void>
  goRegister(): void
  goLogin(): void
}

interface AuthState {
  status: AuthStatus
  user: ApiUser | null
  page: AuthPage
  /** the invitation a registration link carried in its fragment; '' when none did */
  invitation: string
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
    // The token is struck from the address bar as it is read, so it outlives
    // neither the form nor a screenshot of it.
    takeInvitation() {
      const token = new URLSearchParams(window.location.hash.slice(1)).get('invitation')
      if (!token) return
      history.replaceState(null, '', window.location.pathname + window.location.search)
      set({ invitation: token, page: 'register' })
    },

    async boot() {
      actions.takeInvitation()
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
      // Leaving is the outcome asked for, whatever the relay answered: a
      // sign-out the relay never heard leaves a session that lapses on its own.
      try {
        await logout()
      } finally {
        set({ status: 'signed-out', user: null, page: 'login', notice: null })
      }
    },

    goRegister: () => set({ page: 'register' }),
    goLogin: () => set({ page: 'login' }),
  }

  return {
    status: 'booting',
    user: null,
    page: 'login',
    invitation: '',
    notice: null,
    relay: 'ok',
    actions,
  }
})
