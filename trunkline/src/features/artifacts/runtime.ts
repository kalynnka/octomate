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
  container: HTMLDialogElement,
  view: ArtifactView,
  onReady: () => void,
  onError: (message: string) => void,
  onClosed: () => void,
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
  let displayMode: 'inline' | 'fullscreen' = 'inline'
  const toolbar = document.createElement('header')
  toolbar.className = 'trk-artifact-header'
  toolbar.hidden = true
  const title = document.createElement('strong')
  title.textContent = view.artifact.title
  const exit = document.createElement('button')
  exit.type = 'button'
  exit.textContent = 'Back to inline'
  toolbar.append(title, exit)
  container.append(toolbar)
  const dimensions = () => ({
    width: container.clientWidth,
    maxHeight: displayMode === 'fullscreen'
      ? Math.max(100, container.clientHeight - toolbar.offsetHeight - (actions?.offsetHeight ?? 0))
      : 1000,
  })
  let actions: HTMLDivElement | undefined
  let cancelAction: (() => void) | undefined
  const lifetime = new AbortController()
  let requestedHeight = 480
  const log = container.closest<HTMLElement>('#trk-chatlog')
  let scrollAnchor: { edge: 'top' | 'bottom'; position: number } | undefined
  for (const event of ['wheel', 'touchmove', 'pointerdown']) {
    log?.addEventListener(event, () => { scrollAnchor = undefined }, { signal: lifetime.signal, passive: true })
  }
  container.style.setProperty('--artifact-height', `${requestedHeight}px`)
  iframe.style.cssText = `width:100%;height:${Math.min(requestedHeight, dimensions().maxHeight)}px;border:0;display:block`
  container.append(iframe)
  const target = iframe.contentWindow
  if (!target) {
    iframe.remove()
    onError('The artifact frame could not be created.')
    return () => {}
  }
  let hostContext: McpUiHostContext = {
    ...artifactAppearance(), displayMode, availableDisplayModes: ['inline', 'fullscreen'],
    containerDimensions: dimensions(), platform: 'web', locale: navigator.language,
    toolInfo: { tool: view.artifact.invocation.tool },
  }
  const bridge = new AppBridge(null, { name: 'Octomate', version: '1' }, {
    sandbox: { csp: view.resource.ui.csp ?? {}, permissions: {} },
    openLinks: {}, downloadFile: {}, logging: {},
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
    iframe.style.height = `${displayMode === 'fullscreen' ? containerDimensions.maxHeight : Math.min(requestedHeight, containerDimensions.maxHeight)}px`
    container.style.setProperty('--artifact-height', `${Math.min(requestedHeight, 1000) + (actions?.offsetHeight ?? 0)}px`)
    if (initialized) {
      hostContext = { ...hostContext, containerDimensions }
      bridge.setHostContext(hostContext)
    }
  }
  const resize = new ResizeObserver(() => {
    updateDimensions()
    if (!scrollAnchor || !log) return
    const scale = log.getBoundingClientRect().height / log.offsetHeight
    log.scrollTop += (container.getBoundingClientRect()[scrollAnchor.edge] - scrollAnchor.position) / scale
    if (!container.getAnimations().some(animation => animation.playState === 'running')) scrollAnchor = undefined
  })
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
    const nextHeight = Math.min(1000, Math.max(100, Math.ceil(height)))
    if (nextHeight === requestedHeight) return
    if (log && displayMode === 'inline' && document.activeElement === iframe) {
      const bounds = container.getBoundingClientRect()
      const viewport = log.getBoundingClientRect()
      if (bounds.bottom > viewport.top && bounds.top < viewport.bottom) {
        const edge = bounds.top >= viewport.top ? 'top' : 'bottom'
        scrollAnchor = { edge, position: bounds[edge] }
      }
    }
    requestedHeight = nextHeight
    updateDimensions()
  })
  function setDisplayMode(mode: 'inline' | 'fullscreen') {
    if (disposed || mode === displayMode) return
    scrollAnchor = undefined
    container.close()
    displayMode = mode
    container.dataset.displayMode = mode
    toolbar.hidden = mode === 'inline'
    if (mode === 'fullscreen') container.showModal()
    else container.show()
    hostContext = { ...hostContext, displayMode }
    updateDimensions()
  }
  exit.onclick = () => setDisplayMode('inline')
  container.addEventListener('cancel', (event) => {
    event.preventDefault()
    setDisplayMode('inline')
  }, { signal: lifetime.signal })
  bridge.onrequestdisplaymode = async ({ mode }) => {
    const supported = bridge.getAppCapabilities()?.availableDisplayModes
    if (initialized && !disposed && !container.closest('[inert]') && mode !== 'pip' && (!supported || supported.includes(mode))) setDisplayMode(mode)
    return { mode: displayMode }
  }

  // A host gesture confirms downloads and retries links blocked by popup policy.
  function requestBrowserAction(
    links: { label: string; href: string; download?: string }[],
    signal: AbortSignal,
  ): Promise<boolean> {
    if (disposed || signal.aborted || actions) return Promise.resolve(false)
    return new Promise((resolve) => {
      const bar = document.createElement('div')
      bar.className = 'trk-artifact-actions'
      bar.setAttribute('role', 'group')
      bar.setAttribute('aria-label', 'Artifact browser action')
      actions = bar
      const finish = (accepted: boolean) => {
        if (actions !== bar) return
        signal.removeEventListener('abort', cancel)
        bar.remove()
        actions = undefined
        cancelAction = undefined
        updateDimensions()
        resolve(accepted)
      }
      const cancel = () => finish(false)
      cancelAction = cancel
      signal.addEventListener('abort', cancel, { once: true })
      let remaining = links.length
      for (const link of links) {
        const anchor = document.createElement('a')
        anchor.textContent = link.label
        anchor.href = link.href
        if (link.download) anchor.download = link.download
        else { anchor.target = '_blank'; anchor.rel = 'noopener noreferrer' }
        anchor.addEventListener('click', () => {
          // Keep the link mounted until its default browser action has run.
          window.setTimeout(() => {
            anchor.remove()
            if (--remaining === 0) finish(true)
          }, 0)
        }, { once: true })
        bar.append(anchor)
      }
      const dismiss = document.createElement('button')
      dismiss.type = 'button'
      dismiss.textContent = 'Cancel'
      dismiss.onclick = cancel
      bar.append(dismiss)
      container.insertBefore(bar, iframe)
      updateDimensions()
    })
  }
  bridge.onopenlink = async ({ url }, extra) => {
    if (!initialized || disposed || container.closest('[inert]')) return { isError: true }
    let destination: URL
    try { destination = new URL(url) } catch { return { isError: true, message: 'Invalid link.' } }
    if (!['http:', 'https:'].includes(destination.protocol) || destination.username || destination.password) {
      return { isError: true, message: 'Only HTTP(S) links without credentials are supported.' }
    }
    if ([window.location.hostname, new URL(origin).hostname].includes(destination.hostname)) {
      return { isError: true, message: 'Apps cannot open the console or sandbox hostname.' }
    }
    const popup = window.open('about:blank', '_blank')
    if (popup) {
      popup.opener = null
      const link = popup.document.createElement('a')
      link.href = destination.href
      link.rel = 'noreferrer'
      link.referrerPolicy = 'no-referrer'
      popup.document.body.append(link)
      link.click()
      return {}
    }
    const accepted = await requestBrowserAction([{ label: `Open ${destination.host}`, href: destination.href }], extra.mcpReq.signal)
    return { isError: !accepted }
  }
  bridge.ondownloadfile = async ({ contents }, extra) => {
    if (!initialized || disposed || container.closest('[inert]')) return { isError: true }
    if (contents.length === 0 || contents.length > 10) return { isError: true, message: 'Choose between 1 and 10 files.' }
    const files: { name: string; blob: Blob }[] = []
    let size = 0
    for (const content of contents) {
      if (content.type !== 'resource') return { isError: true, message: 'Downloads require embedded file data; resource access is not enabled.' }
      const resource = content.resource
      let name: string
      let blob: Blob
      try {
        name = decodeURIComponent(new URL(resource.uri).pathname.split('/').at(-1) ?? '')
          .replace(/[<>:"/\\|?*\p{Cc}\p{Cf}]/gu, '_').replace(/^[. ]+|[. ]+$/g, '').slice(0, 200) || 'download'
        blob = 'blob' in resource
          ? new Blob([Uint8Array.from(atob(resource.blob), char => char.charCodeAt(0))], { type: resource.mimeType })
          : new Blob([resource.text], { type: resource.mimeType ?? 'text/plain' })
      } catch { return { isError: true, message: 'Invalid file name or base64 content.' } }
      size += blob.size
      if (size > 20 * 1024 * 1024) return { isError: true, message: 'Downloads are limited to 20 MiB per request.' }
      files.push({ name, blob })
    }
    const links = files.map(({ name, blob }) => ({ label: `Download ${name}`, href: URL.createObjectURL(blob), download: name }))
    try {
      return { isError: !await requestBrowserAction(links, extra.mcpReq.signal) }
    } finally {
      // Firefox consumes blob URLs asynchronously after the download gesture.
      window.setTimeout(() => { for (const link of links) URL.revokeObjectURL(link.href) }, 1000)
    }
  }
  bridge.addEventListener('loggingmessage', ({ level, logger, data }) => {
    if (!initialized || disposed) return
    const log = ['error', 'critical', 'alert', 'emergency'].includes(level) ? console.error
      : level === 'warning' ? console.warn : level === 'debug' ? console.debug : console.info
    log(`[Artifact ${view.artifact.id}${logger ? ` / ${logger}` : ''}]`, data)
  })
  bridge.addEventListener('requestteardown', () => {
    if (!initialized || disposed) return
    dispose()
    onClosed()
  })
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
    cancelAction?.()
    lifetime.abort()
    container.close()
    const closing = initialized
      ? bridge.teardownResource({}, { timeout: 200 }).catch(() => {})
      : Promise.resolve()
    void closing.finally(() => { void bridge.close(); iframe.remove(); toolbar.remove() })
  }
  window.addEventListener('pagehide', dispose)
  return dispose
}
