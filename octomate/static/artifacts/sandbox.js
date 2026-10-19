// This document is trusted; the app is confined to the opaque-origin child.
const parentOrigin = document.documentElement.dataset.parentOrigin
const policy = document.documentElement.dataset.csp
const reserved = 'ui/notifications/sandbox-'
let frame

/** Shared defaults sit below authored CSS; only the sandbox parent can update theme tokens and fonts. */
function applyHostTheme(sandboxOrigin) {
  const defaults = document.createElement('style')
  defaults.textContent = `@layer octomate {
    :where(html) { background-color: var(--color-background-primary); color: var(--color-text-primary); font-family: var(--font-sans); font-size: var(--font-text-md-size); line-height: var(--font-text-md-line-height); }
    :where(body) { background-color: inherit; color: inherit; }
    :where(h1) { font-size: var(--font-heading-lg-size); line-height: var(--font-heading-lg-line-height); }
    :where(h2) { font-size: var(--font-heading-md-size); line-height: var(--font-heading-md-line-height); }
    :where(h3) { font-size: var(--font-heading-sm-size); line-height: var(--font-heading-sm-line-height); }
    :where(h4) { font-size: var(--font-text-lg-size); line-height: var(--font-text-lg-line-height); }
    :where(h5, h6) { font-size: var(--font-text-md-size); line-height: var(--font-text-md-line-height); }
    :where(h1, h2, h3, h4, h5, h6, strong, b, dt, legend, th) { font-weight: var(--font-weight-bold); }
    :where(small, figcaption, caption) { font-size: var(--font-text-sm-size); color: var(--color-text-secondary); }
    :where(ul, ol) { padding-inline-start: 1.5em; }
    :where(li + li) { margin-block-start: 0.25em; }
    :where(dd) { margin-inline-start: 1.5em; margin-block-end: 0.75em; }
    :where(blockquote) { margin-inline: 0; padding-inline-start: 1em; border-inline-start: 3px solid var(--color-border-primary); color: var(--color-text-secondary); }
    :where(table) { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
    :where(caption) { text-align: start; padding-block-end: 0.5em; }
    :where(th, td) { padding: 0.5em 0.75em; border: var(--border-width-regular) solid var(--color-border-primary); text-align: start; vertical-align: top; }
    :where(td) { overflow-wrap: anywhere; }
    :where(th, tfoot) { background: var(--color-background-secondary); }
    :where(fieldset) { min-inline-size: 0; margin-block: 1em; padding: 1em; border: var(--border-width-regular) solid var(--color-border-primary); border-radius: var(--border-radius-sm); }
    :where(legend) { padding-inline: 0.4em; color: var(--color-text-secondary); }
    :where(button, input, select, textarea) { font: inherit; accent-color: var(--color-ring-primary); }
    :where(input, select, textarea) { box-sizing: border-box; max-inline-size: 100%; }
    :where(button, input[type="button"], input[type="submit"], input[type="reset"]), :where(input)::file-selector-button { color: var(--color-text-primary); background: var(--color-background-tertiary); border: var(--border-width-regular) solid var(--color-border-primary); border-radius: var(--border-radius-sm); padding: 0.5em 0.85em; cursor: pointer; }
    :where(button:enabled:hover, input[type="button"]:enabled:hover, input[type="submit"]:enabled:hover, input[type="reset"]:enabled:hover), :where(input:enabled)::file-selector-button:hover { background: var(--color-background-secondary); }
    :where(input)::file-selector-button { font: inherit; margin-inline-end: 0.75em; }
    :where(input:disabled)::file-selector-button { cursor: not-allowed; }
    :where(textarea, select, input:not([type="button"], [type="submit"], [type="reset"], [type="checkbox"], [type="radio"], [type="range"], [type="color"], [type="file"], [type="hidden"], [type="image"])) { color: var(--color-text-primary); background: var(--color-background-secondary); border: var(--border-width-regular) solid var(--color-border-primary); border-radius: var(--border-radius-sm); padding: 0.5em; }
    @supports (appearance: base-select) {
      :where(select:not([multiple], [size])), :where(select:not([multiple], [size]))::picker(select) { appearance: base-select; }
      :where(select:not([multiple], [size])) { display: inline-flex; align-items: center; gap: 0.75em; padding-inline: 0.75em; cursor: pointer; }
      :where(select:open) { border-color: var(--color-ring-primary); }
      :where(select)::picker-icon { content: '▾'; color: var(--color-ring-primary); margin-inline-start: auto; }
      :where(select:open)::picker-icon { rotate: 180deg; }
      :where(select)::picker(select) { margin-block: 4px; padding: 4px; max-block-size: min(18rem, 50dvh); overflow: auto; overscroll-behavior: contain; font: inherit; color: var(--color-text-primary); background: var(--color-background-primary); border: var(--border-width-regular) solid var(--color-border-primary); border-radius: 0; box-shadow: var(--shadow-lg); scrollbar-color: var(--color-border-primary) var(--color-background-primary); scrollbar-width: thin; }
      :where(select:not([multiple], [size]) option) { padding: 0.5em 0.75em; gap: 0.75em; border-inline-start: 2px solid transparent; border-radius: 0; color: var(--color-text-secondary); background: transparent; cursor: pointer; }
      :where(select:not([multiple], [size]) option:checked) { color: var(--color-text-primary); background: color-mix(in srgb, var(--color-ring-primary) 10%, var(--color-background-primary)); font-weight: var(--font-weight-bold); }
      :where(select:not([multiple], [size]) option:is(:hover, :focus-visible)) { color: var(--color-text-primary); background: var(--color-background-tertiary); border-inline-start-color: var(--color-ring-primary); outline: none; }
      :where(select option)::checkmark { color: var(--color-ring-primary); }
      :where(select option:disabled) { color: var(--color-text-disabled); cursor: default; }
    }
    :where(input, textarea)::placeholder { color: var(--color-text-tertiary); }
    :where(input[readonly], textarea[readonly]) { background: var(--color-background-primary); color: var(--color-text-secondary); }
    :where(button:disabled, input:disabled, select:disabled, textarea:disabled) { opacity: 0.6; cursor: not-allowed; }
    :where(button[aria-pressed="true"], [role="tab"][aria-selected="true"]) { background: color-mix(in srgb, var(--color-ring-primary) 10%, var(--color-background-primary)); border-color: var(--color-ring-primary); }
    :where(progress, meter) { max-inline-size: 100%; vertical-align: middle; accent-color: var(--color-ring-info); }
    :where(a[href]) { color: var(--color-text-info); text-decoration-color: var(--color-border-info); text-underline-offset: 0.15em; }
    :where(code, pre, kbd, samp) { font-family: var(--font-mono); }
    :where(code:not(pre code), kbd, samp) { background: var(--color-background-secondary); border-radius: var(--border-radius-xs); padding: 0.1em 0.3em; }
    :where(kbd) { border: var(--border-width-regular) solid var(--color-border-primary); box-shadow: 0 1px 0 var(--color-border-primary); }
    :where(mark) { background: var(--color-background-warning); color: var(--color-text-warning); border-radius: var(--border-radius-xs); padding-inline: 0.2em; }
    :where(pre) { background: var(--color-background-secondary); border-radius: var(--border-radius-md); padding: 1em; overflow: auto; }
    :where(details) { border: var(--border-width-regular) solid var(--color-border-primary); border-radius: var(--border-radius-sm); padding: 0.75em 1em; }
    :where(summary) { cursor: pointer; font-weight: var(--font-weight-semibold); }
    :where(summary)::marker { color: var(--color-ring-primary); }
    :where(details[open] > summary) { margin-block-end: 0.75em; }
    :where(dialog, [popover]) { box-sizing: border-box; max-inline-size: calc(100% - 2em); max-block-size: calc(100% - 2em); overflow: auto; padding: 1em; background: var(--color-background-tertiary); color: var(--color-text-primary); border: var(--border-width-regular) solid var(--color-border-primary); border-radius: var(--border-radius-md); box-shadow: var(--shadow-lg); font: inherit; }
    :where(dialog)::backdrop { background: color-mix(in srgb, var(--color-background-primary) 65%, transparent); }
    :where([role="tooltip"]) { padding: 0.5em 0.75em; background: var(--color-background-info); color: var(--color-text-info); border: var(--border-width-regular) solid var(--color-border-info); border-radius: var(--border-radius-sm); box-shadow: var(--shadow-sm); font-size: var(--font-text-sm-size); }
    :where([role="tablist"]:not([hidden])) { display: flex; gap: 0.25em; border-block-end: var(--border-width-regular) solid var(--color-border-primary); }
    :where([role="tablist"][aria-orientation="vertical"]) { flex-direction: column; }
    :where([role="tab"]) { border-radius: 0; }
    :where(img, video) { max-inline-size: 100%; height: auto; }
    :where(hr) { border: 0; border-top: var(--border-width-regular) solid var(--color-border-primary); }
    :where(a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible, [tabindex]:focus-visible) { outline: 2px solid var(--color-ring-primary); outline-offset: 2px; }
    :where(:is(input, select, textarea):is(:user-invalid, [aria-invalid="true"])) { border-color: var(--color-border-danger); outline-color: var(--color-ring-danger); }
  }`
  const fonts = document.createElement('style')
  document.head.append(defaults, fonts)
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || event.origin !== sandboxOrigin) return
    const message = event.data
    if (message?.jsonrpc !== '2.0') return
    const context = message.method === 'ui/notifications/host-context-changed'
      ? message.params : message.result?.hostContext
    if (!context) return
    const root = document.documentElement
    if (context.theme === 'light' || context.theme === 'dark') {
      root.dataset.theme = context.theme
      root.style.colorScheme = context.theme
    }
    for (const [name, value] of Object.entries(context.styles?.variables ?? {})) {
      if (name.startsWith('--') && typeof value === 'string') root.style.setProperty(name, value)
    }
    const fontCss = context.styles?.css?.fonts
    if (typeof fontCss === 'string' && fonts.textContent !== fontCss) fonts.textContent = fontCss
  })
}

/** Give plain HTML the lifecycle and size reporting normally supplied by an MCP app. */
function initializeHtml(sandboxOrigin) {
  const send = (method, params, id) => window.parent.postMessage({ jsonrpc: '2.0', method, params, ...(id ? { id } : {}) }, sandboxOrigin)
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || event.origin !== sandboxOrigin) return
    const message = event.data
    if (message?.jsonrpc !== '2.0') return
    if (message.id === 'html-init' && message.result) {
      send('ui/notifications/initialized', {})
      const resize = new ResizeObserver(() => {
        const body = document.body
        const bounds = body.getBoundingClientRect()
        const border = body.offsetHeight - body.clientHeight
        const height = bounds.top + Math.max(bounds.height, body.scrollHeight + border) + parseFloat(getComputedStyle(body).marginBottom)
        send('ui/notifications/size-changed', { height })
      })
      resize.observe(document.body, { box: 'border-box' })
    } else if (message.method === 'ui/resource-teardown') {
      window.parent.postMessage({ jsonrpc: '2.0', id: message.id, result: {} }, sandboxOrigin)
    }
  })
  window.addEventListener('load', () => send('ui/initialize', {
    protocolVersion: '2026-01-26', appInfo: { name: 'HTML visualization', version: '1' }, appCapabilities: {},
  }, 'html-init'), { once: true })
}

window.addEventListener('message', (event) => {
  const message = event.data
  if (!message || message.jsonrpc !== '2.0') return
  const sandboxMessage = typeof message.method === 'string' && message.method.startsWith(reserved)
  if (event.source === window.parent && event.origin === parentOrigin) {
    if (message.method === `${reserved}resource-ready`) {
      if (frame || typeof message.params?.html !== 'string') return
      frame = document.createElement('iframe')
      frame.title = 'Artifact content'
      frame.setAttribute('sandbox', 'allow-scripts')
      frame.setAttribute('allow', "camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'; fullscreen 'none'")
      frame.referrerPolicy = 'no-referrer'
      const meta = document.createElement('meta')
      meta.httpEquiv = 'Content-Security-Policy'
      meta.content = policy
      const theme = document.createElement('script')
      theme.textContent = `(${applyHostTheme.toString()})(${JSON.stringify(window.location.origin)})`
      if (document.documentElement.dataset.contentKind === 'html') {
        theme.textContent += `;(${initializeHtml.toString()})(${JSON.stringify(window.location.origin)})`
      }
      frame.srcdoc = `<!doctype html><html><head>${meta.outerHTML}${theme.outerHTML}</head><body>${message.params.html}</body></html>`
      document.body.append(frame)
    } else if (!sandboxMessage) {
      frame?.contentWindow.postMessage(message, '*')
    }
  } else if (frame && event.source === frame.contentWindow && event.origin === 'null' && !sandboxMessage) {
    window.parent.postMessage(message, parentOrigin)
  }
})

window.parent.postMessage({ jsonrpc: '2.0', method: `${reserved}proxy-ready`, params: {} }, parentOrigin)
