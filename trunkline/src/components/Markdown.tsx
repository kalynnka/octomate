import hljs from 'highlight.js/lib/common'
import MarkdownIt from 'markdown-it'
import type { Mounted } from 'pele'
import { memo, useEffect, useMemo, useRef } from 'react'

// html stays off (the default): agent output is prose, not trusted markup —
// any raw HTML in it renders as text. linkify turns bare URLs into links.
const md = new MarkdownIt({
  linkify: true,
  breaks: true,
  highlight: (code, language) => hljs.getLanguage(language)
    ? hljs.highlight(code, { language, ignoreIllegals: true }).value
    : '',
})

for (const rule of ['fence', 'code_block']) {
  const render = md.renderer.rules[rule]!
  md.renderer.rules[rule] = (tokens, idx, options, env, self) => {
    const token = tokens[idx]
    const html = render(tokens, idx, options, env, self)
    const language = token.info.trim().split(/\s+/)[0]
    if (language === 'mermaid') {
      return `<div class="trk-mermaid" data-source="${md.utils.escapeHtml(token.content)}">${html}</div>\n`
    }
    const name = md.utils.escapeHtml(hljs.getLanguage(language)?.name || language || 'Plain text')
    const count = token.content.replace(/\n$/, '').split('\n').length
    const numbers = Array.from({ length: count }, (_, index) => index + 1).join('\n')
    return `<div class="trk-code-block"><div class="trk-code-header"><span>${name}</span><span>${count} ${count === 1 ? 'line' : 'lines'}</span></div><div class="trk-code-scroll" role="region" aria-label="${name} code" tabindex="0"><span class="trk-code-gutter" aria-hidden="true">${numbers}</span>${html}</div></div>\n`
  }
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
    const mounted: Mounted[] = []
    const library = import('pele/lazy')
    const render = async (node: HTMLElement) => {
      const source = node.dataset.source!
      try {
        const { mountAsync } = await library
        if (!active) return
        const diagram = await mountAsync(node, source, { links: false, images: false, zoom: true })
        if (!active) {
          diagram.destroy()
          return
        }
        if (diagram.error !== undefined) {
          diagram.destroy()
          throw diagram.error
        }
        mounted.push(diagram)
      } catch (error) {
        if (!active) return
        node.innerHTML = `<pre><code>${md.utils.escapeHtml(source)}</code></pre><p role="status">Unable to render this Mermaid diagram.</p>`
        node.querySelector('p')!.title = error instanceof Error ? error.message : String(error)
      }
    }
    diagrams.forEach(node => { void render(node) })
    return () => {
      active = false
      mounted.forEach(diagram => diagram.destroy())
    }
  }, [text, cursor])

  return <div ref={root} className="trk-md" dangerouslySetInnerHTML={{ __html: html }} />
})
