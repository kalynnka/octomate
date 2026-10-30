import MarkdownIt, { type Token } from 'markdown-it'

const markdown = new MarkdownIt()

/** Readable text for compact message previews, preserving literal code and escapes. */
export function markdownText(text: string): string {
  const content = (token: Token): string => token.children
    ? token.children.map(content).join('')
    : token.type === 'softbreak' || token.type === 'hardbreak' ? ' ' : token.content
  return markdown.parse(text, {}).map(content).join(' ').replace(/\s+/g, ' ').trim()
}
