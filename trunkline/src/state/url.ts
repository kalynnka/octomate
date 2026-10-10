/**
 * The console's address. It names what is open — a thread, a new thread being
 * composed, or a control page — so a reload, a shared link, or signing back in
 * after a session lapsed opens where it points, and Back and Forward step
 * through what was opened.
 *
 * The store stays the source of truth: the address follows it, and is read back
 * into it only when the console mounts and when the browser moves through its
 * history.
 */
import { fetchThread, fetchThreads } from '@/lib/api/client'
import { controlHints } from '@/features/control/sections'
import { useConsole, type ControlSection } from '@/state/console'

type ConsoleState = ReturnType<typeof useConsole.getState>

const isSection = (name: string): name is Exclude<ControlSection, ''> =>
  Object.hasOwn(controlHints, name)

/** The address of what the store has open; null while nothing is — signed out,
 *  or still opening — and the address is then left as it stands. */
export function pathOf(state: ConsoleState): string | null {
  if (state.mgmtSec) return `/control/${state.mgmtSec}`
  if (state.ntOn) return '/threads/new'
  return state.selThreadId ? `/threads/${state.selThreadId}` : null
}

/** The newest thread, or the new-thread flow on an empty or unreachable relay. */
async function openNewest() {
  const { selectThread, startNewThread } = useConsole.getState().actions
  const first = (await fetchThreads().catch(() => []))[0]
  if (first) await selectThread(first.channel_tentacle_id, first.id)
  else startNewThread()
}

/**
 * Open what `path` names. A control page opens over the thread already open, or
 * the newest one; the root, or a thread this account cannot open, opens the
 * newest thread.
 */
export async function openPath(path: string) {
  const { selectThread, startNewThread, openControl } = useConsole.getState().actions
  const [, head = '', tail = ''] = path.split('/')
  if (head === 'threads' && tail === 'new') return startNewThread()
  if (head === 'control' && isSection(tail)) {
    if (!useConsole.getState().selThreadId) await openNewest()
    return openControl(tail)
  }
  const thread = head === 'threads' && tail ? await fetchThread(tail).catch(() => null) : null
  if (thread) return selectThread(thread.channel_tentacle_id, thread.id)
  await openNewest()
}

/**
 * Open what the address names, then keep the address on what the store has open
 * until the returned function stops it. A step into somewhere new is a history
 * entry of its own; an address read back from history, or one that was only on
 * its way somewhere — the root, or a new thread now saved — is replaced instead.
 */
export function followAddress(): () => void {
  let reading = false
  const read = () => {
    reading = true
    void openPath(window.location.pathname).finally(() => {
      reading = false
    })
  }
  const unsubscribe = useConsole.subscribe((state, previous) => {
    const path = pathOf(state)
    if (path === null || path === window.location.pathname) return
    const before = pathOf(previous)
    const replace = reading || before === null || before === '/threads/new'
    if (replace) window.history.replaceState(null, '', path)
    else window.history.pushState(null, '', path)
  })
  window.addEventListener('popstate', read)
  read()
  return () => {
    unsubscribe()
    window.removeEventListener('popstate', read)
  }
}
