import type { JSONRPCMessage, Transport } from '@modelcontextprotocol/client'
import { JSONRPCMessageSchema } from '@modelcontextprotocol/core'
import { AppBridge } from '@modelcontextprotocol/ext-apps/app-bridge'
import type { ArtifactView } from './api'

/** The SDK transport checks source only; this one also pins both message origins. */
export class ArtifactTransport implements Transport {
  readonly target: Window
  readonly origin: string
  onmessage?: Transport['onmessage']
  onclose?: Transport['onclose']
  onerror?: Transport['onerror']

  constructor(target: Window, origin: string) {
    this.target = target
    this.origin = origin
  }

  receive = (event: MessageEvent<unknown>) => {
    if (event.source !== this.target || event.origin !== this.origin) return
    const message = JSONRPCMessageSchema.safeParse(event.data)
    if (message.success) this.onmessage?.(message.data)
  }

  async start() { window.addEventListener('message', this.receive) }
  async send(message: JSONRPCMessage) { this.target.postMessage(message, this.origin) }
  async close() {
    window.removeEventListener('message', this.receive)
    this.onclose?.()
  }
}

/** Own one iframe/bridge lifetime, replaying saved data only after initialization. */
export function mountArtifact(
  container: HTMLElement,
  view: ArtifactView,
  onReady: () => void,
  onError: (message: string) => void,
): () => void {
  const origin = new URL(view.sandbox_origin).origin
  if (origin === window.location.origin || new URL(view.sandbox_url).origin !== origin) {
    onError('The artifact sandbox must use a separate origin.')
    return () => {}
  }
  const iframe = document.createElement('iframe')
  iframe.title = view.artifact.title
  iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin')
  iframe.setAttribute('allow', "camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'; fullscreen 'none'")
  iframe.referrerPolicy = 'no-referrer'
  iframe.style.cssText = 'width:100%;height:480px;border:0;display:block'
  container.append(iframe)
  const target = iframe.contentWindow
  if (!target) {
    iframe.remove()
    onError('The artifact frame could not be created.')
    return () => {}
  }
  const theme = () => document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
  const dimensions = () => ({ width: container.clientWidth, maxHeight: 1000 })
  const bridge = new AppBridge(null, { name: 'Octomate', version: '1' }, {
    sandbox: { csp: view.resource.ui.csp ?? {}, permissions: {} },
  }, {
    hostContext: {
      theme: theme(), displayMode: 'inline', availableDisplayModes: ['inline'],
      containerDimensions: dimensions(), platform: 'web', locale: navigator.language,
      toolInfo: { tool: view.artifact.invocation.tool },
    },
  })
  let disposed = false
  let initialized = false
  let resourceSent = false
  const fail = (message: string) => {
    if (disposed) return
    onError(message)
    dispose()
  }
  const timeout = window.setTimeout(() => fail('The artifact did not finish loading.'), 15000)
  const resize = new ResizeObserver(() => {
    if (initialized && !disposed) bridge.setHostContext({ containerDimensions: dimensions() })
  })
  const themes = new MutationObserver(() => {
    if (initialized && !disposed) bridge.setHostContext({ theme: theme() })
  })
  resize.observe(container)
  themes.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  bridge.addEventListener('sandboxready', () => {
    if (resourceSent || disposed) return
    resourceSent = true
    void bridge.sendSandboxResourceReady({ html: view.resource.html }).catch(() => fail('The artifact could not be sent to its sandbox.'))
  })
  bridge.addEventListener('initialized', () => {
    if (initialized || disposed) return
    initialized = true
    void (async () => {
      await bridge.sendToolInput({ arguments: view.artifact.invocation.arguments })
      if (disposed) return
      await bridge.sendToolResult(view.artifact.invocation.result)
      if (disposed) return
      window.clearTimeout(timeout)
      onReady()
    })().catch(() => fail('The saved artifact could not be replayed.'))
  })
  bridge.addEventListener('sizechange', ({ height }) => {
    if (!initialized || disposed || height == null || !Number.isFinite(height)) return
    iframe.style.height = `${Math.max(100, Math.min(1000, Math.ceil(height)))}px`
  })
  bridge.onrequestdisplaymode = async () => ({ mode: 'inline' })
  bridge.onerror = () => fail('The artifact connection failed.')
  iframe.onerror = () => fail('The artifact sandbox could not be loaded.')
  void bridge.connect(new ArtifactTransport(target, origin)).then(() => {
    if (!disposed) iframe.src = view.sandbox_url
  }).catch(() => fail('The artifact connection could not be opened.'))

  function dispose() {
    if (disposed) return
    disposed = true
    window.clearTimeout(timeout)
    window.removeEventListener('pagehide', dispose)
    resize.disconnect()
    themes.disconnect()
    const closing = initialized
      ? bridge.teardownResource({}, { timeout: 200 }).catch(() => {})
      : Promise.resolve()
    void closing.finally(() => { void bridge.close(); iframe.remove() })
  }
  window.addEventListener('pagehide', dispose)
  return dispose
}
