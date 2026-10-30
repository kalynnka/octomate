import hljs from 'highlight.js/lib/common'
import MarkdownIt from 'markdown-it'
import { memo, useEffect, useId, useMemo, useRef } from 'react'
import { useConsole } from '@/state/console'

// html stays off (the default): agent output is prose, not trusted markup —
// any raw HTML in it renders as text. linkify turns bare URLs into links.
const md = new MarkdownIt({
  linkify: true,
  breaks: true,
  highlight: (code, language) => hljs.getLanguage(language)
    ? hljs.highlight(code, { language, ignoreIllegals: true }).value
    : '',
})

const renderFence = md.renderer.rules.fence!
md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const html = renderFence(tokens, idx, options, env, self)
  if (tokens[idx].info.trim().split(/\s+/)[0] !== 'mermaid') return html
  return `<div class="trk-mermaid" data-source="${md.utils.escapeHtml(tokens[idx].content)}">${html}</div>\n`
}

// Links leave the console; open them beside it rather than over it.
const renderLink =
  md.renderer.rules.link_open ??
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noreferrer')
  return renderLink(tokens, idx, options, env, self)
}

/** Agent prose rendered as markdown — element rhythm comes from `.trk-md`
 * in console.css; the font voice inherits from the ledger card. */
export const Markdown = memo(function Markdown({ text, cursor = false }: { text: string; cursor?: boolean }) {
  const root = useRef<HTMLDivElement>(null)
  const id = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const generation = useRef(0)
  const dark = useConsole(s => s.theme === 'dark' || (s.theme === 'auto' && s.sysDark))
  const html = useMemo(() => {
    const tokens = md.parse(text, {})
    if (cursor) {
      const caret = new MarkdownIt.Token('html_inline', '', 0)
      caret.content = '<span aria-hidden="true" class="lt-caret trk-print-caret"></span>'
      const last = tokens.findLast((token) => token.nesting !== -1 && !token.hidden)
      if (last?.type === 'inline') {
        last.children?.push(caret)
      } else if (last?.type === 'fence' || last?.type === 'code_block') {
        const rendered = md.renderer.render([last], md.options, {})
        last.type = 'html_block'
        last.content = rendered.replace(/<\/code><\/pre>/, `${caret.content}</code></pre>`)
      } else {
        tokens.push(caret)
      }
    }
    return md.renderer.render(tokens, md.options, {})
  }, [text, cursor])

  useEffect(() => {
    if (cursor) return
    const diagrams = root.current?.querySelectorAll<HTMLElement>('.trk-mermaid')
    if (!diagrams?.length) return
    let active = true
    const revision = ++generation.current
    const library = import('mermaid')
    const render = async (node: HTMLElement, index: number) => {
      const source = node.dataset.source!
      const container = document.createElement('div')
      try {
        const [{ default: mermaid }] = await Promise.all([library, document.fonts.ready])
        if (!active) return
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          htmlLabels: false,
          suppressErrorRendering: true,
          logLevel: 'fatal',
          layout: 'dagre',
          theme: dark ? 'dark' : 'neutral',
          fontFamily: 'Fira Code, monospace',
        })
        // Mermaid measures in viewport pixels, so neutralize the console zoom during layout.
        container.style.cssText = `position: fixed; visibility: hidden; zoom: calc(1 / var(--trk-zoom, 1)); width: ${node.clientWidth}px;`
        document.body.append(container)
        const { svg } = await mermaid.render(`mermaid-${id}-${revision}-${index}`, source, container)
        if (active) node.innerHTML = svg
      } catch (error) {
        if (!active) return
        node.innerHTML = `<pre><code>${md.utils.escapeHtml(source)}</code></pre><p role="status">Unable to render this Mermaid diagram.</p>`
        node.querySelector('p')!.title = error instanceof Error ? error.message : String(error)
      } finally {
        container.remove()
      }
    }
    diagrams.forEach((node, index) => { void render(node, index) })
    return () => { active = false }
  }, [text, cursor, dark, id])

  return <div ref={root} className="trk-md" dangerouslySetInnerHTML={{ __html: html }} />
})
