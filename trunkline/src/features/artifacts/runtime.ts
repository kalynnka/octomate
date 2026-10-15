import type { JSONRPCMessage, Transport } from '@modelcontextprotocol/client'
import { JSONRPCMessageSchema } from '@modelcontextprotocol/core'
import { AppBridge, McpUiHostContextSchema, type McpUiHostContext, type McpUiStyles } from '@modelcontextprotocol/ext-apps/app-bridge'
import type { ArtifactView } from './api'

/** Resolve host tokens before crossing into a document with its own stylesheet. */
function artifactAppearance() {
  const root = document.documentElement
  const styles = getComputedStyle(root)
  const token = (name: string) => styles.getPropertyValue(name).trim()
  return McpUiHostContextSchema.parse({
    theme: root.dataset.theme === 'dark' ? 'dark' : 'light',
    styles: { variables: {
      '--color-background-primary': token('--card-bg'),
      '--color-background-secondary': token('--surface-sunken'),
      '--color-background-tertiary': token('--surface-raised'),
      '--color-background-inverse': token('--fg-1'),
      '--color-background-ghost': 'transparent',
      '--color-background-disabled': token('--surface-sunken'),
      '--color-text-primary': token('--fg-1'),
      '--color-text-secondary': token('--fg-2'),
      '--color-text-tertiary': token('--fg-3'),
      '--color-text-inverse': token('--page-bg'),
      '--color-text-ghost': token('--fg-3'),
      '--color-text-disabled': token('--fg-3'),
      '--color-border-primary': token('--line-divider'),
      '--color-border-secondary': token('--line-color'),
      '--color-border-tertiary': token('--line-color'),
      '--color-border-inverse': token('--page-bg'),
      '--color-border-ghost': 'transparent',
      '--color-border-disabled': token('--line-color'),
      '--color-ring-primary': token('--color-accent'),
      '--color-ring-secondary': token('--fg-2'),
      '--color-ring-inverse': token('--page-bg'),
      '--font-sans': token('--font-sans'),
      '--font-mono': token('--font-mono'),
      '--border-radius-xs': token('--radius-small'),
      '--border-radius-sm': token('--radius-small'),
      '--border-radius-md': token('--radius-medium'),
      '--border-radius-lg': token('--radius-large'),
      '--border-radius-xl': token('--radius-large'),
      '--border-radius-full': token('--radius-dot'),
      '--shadow-sm': token('--shadow-soft'),
      '--shadow-md': token('--shadow-soft'),
      '--shadow-lg': token('--shadow-card'),
    } satisfies Partial<McpUiStyles> },
  })
}

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
  const dimensions = () => ({ width: container.clientWidth, maxHeight: Math.max(240, Math.min(640, Math.floor(window.innerHeight * 0.65))) })
  let requestedHeight = 480
  iframe.style.cssText = `width:100%;height:${Math.min(requestedHeight, dimensions().maxHeight)}px;border:0;display:block`
  container.append(iframe)
  const target = iframe.contentWindow
  if (!target) {
    iframe.remove()
    onError('The artifact frame could not be created.')
    return () => {}
  }
  let hostContext: McpUiHostContext = {
    ...artifactAppearance(), displayMode: 'inline', availableDisplayModes: ['inline'],
    containerDimensions: dimensions(), platform: 'web', locale: navigator.language,
    toolInfo: { tool: view.artifact.invocation.tool },
  }
  const bridge = new AppBridge(null, { name: 'Octomate', version: '1' }, {
    sandbox: { csp: view.resource.ui.csp ?? {}, permissions: {} },
  }, { hostContext })
  let disposed = false
  let initialized = false
  let resourceSent = false
  const fail = (message: string) => {
    if (disposed) return
    onError(message)
    dispose()
  }
  const timeout = window.setTimeout(() => fail('The artifact did not finish loading.'), 15000)
  const updateDimensions = () => {
    if (disposed) return
    const containerDimensions = dimensions()
    iframe.style.height = `${Math.min(requestedHeight, containerDimensions.maxHeight)}px`
    if (initialized) {
      hostContext = { ...hostContext, containerDimensions }
      bridge.setHostContext(hostContext)
    }
  }
  const resize = new ResizeObserver(updateDimensions)
  const updateAppearance = () => {
    if (!initialized || disposed) return
    hostContext = { ...hostContext, ...artifactAppearance() }
    bridge.setHostContext(hostContext)
  }
  const themes = new MutationObserver(updateAppearance)
  void document.fonts.ready.then(updateAppearance)
  resize.observe(container)
  window.addEventListener('resize', updateDimensions)
  themes.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  bridge.addEventListener('sandboxready', () => {
    if (resourceSent || disposed) return
    resourceSent = true
    void bridge.sendSandboxResourceReady({ html: view.resource.html }).catch(() => fail('The artifact could not be sent to its sandbox.'))
  })
  bridge.addEventListener('initialized', () => {
    if (initialized || disposed) return
    initialized = true
    updateAppearance()
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
    requestedHeight = Math.max(100, Math.ceil(height))
    updateDimensions()
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
    window.removeEventListener('resize', updateDimensions)
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
