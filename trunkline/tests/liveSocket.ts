/**
 * A stand-in for the thread's live socket, for tests that drive runs: opens on
 * the next microtask, carries what the test delivers, and records its URL.
 */
export class LiveSocket {
  static opened: LiveSocket[] = []
  readonly url: string
  onopen: (() => void) | null = null
  onmessage: ((message: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  closed = false

  constructor(url: string) {
    this.url = url
    LiveSocket.opened.push(this)
    queueMicrotask(() => this.onopen?.())
  }

  static last(): LiveSocket {
    const socket = LiveSocket.opened.at(-1)
    if (!socket) throw new Error('no live socket was opened')
    return socket
  }

  deliver(...events: unknown[]) {
    for (const event of events) this.onmessage?.({ data: JSON.stringify(event) })
  }

  close() {
    this.closed = true
    this.onclose?.()
  }
}

/** Put the stand-in where the client looks for the browser's socket and origin. */
export function installLiveSocket() {
  LiveSocket.opened = []
  Object.defineProperty(globalThis, 'WebSocket', { configurable: true, value: LiveSocket })
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { protocol: 'http:', host: 'console.test' } })
}
