import MarkdownIt from 'markdown-it'

// html stays off (the default): agent output is prose, not trusted markup —
// any raw HTML in it renders as text. linkify turns bare URLs into links.
const md = new MarkdownIt({ linkify: true })

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
export function Markdown({ text }: { text: string }) {
  return <div className="trk-md" dangerouslySetInnerHTML={{ __html: md.render(text) }} />
}
