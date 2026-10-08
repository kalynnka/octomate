import MarkdownIt from 'markdown-it'

// html stays off (the default): agent output is prose, not trusted markup —
// any raw HTML in it renders as text. linkify turns bare URLs into links.
const md = new MarkdownIt({ linkify: true, breaks: true })

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
export function Markdown({ text, cursor = false }: { text: string; cursor?: boolean }) {
  const tokens = md.parse(text, {})
  if (cursor) {
    const caret = new MarkdownIt.Token('html_inline', '', 0)
    caret.content = '<span aria-hidden="true" class="lt-caret trk-print-caret"></span>'
    const last = tokens.findLast((token) => token.nesting !== -1 && !token.hidden)
    if (last?.type === 'inline') {
      last.children?.push(caret)
    } else if (last?.type === 'fence' || last?.type === 'code_block') {
      const html = md.renderer.render([last], md.options, {})
      last.type = 'html_block'
      last.content = html.replace(/<\/code><\/pre>\n?$/, `${caret.content}</code></pre>\n`)
    } else {
      tokens.push(caret)
    }
  }
  return <div className="trk-md" dangerouslySetInnerHTML={{ __html: md.renderer.render(tokens, md.options, {}) }} />
}
