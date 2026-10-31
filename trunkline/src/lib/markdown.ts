import MarkdownIt, { type Token } from 'markdown-it'
import { tex } from '@mdit/plugin-tex'

const markdown = new MarkdownIt().use(tex, { delimiters: 'all', render: content => content })

/** Readable text for compact message previews, preserving literal code and escapes. */
export function markdownText(text: string): string {
  const content = (token: Token): string => token.children
    ? token.children.map(content).join('')
    : token.type === 'softbreak' || token.type === 'hardbreak' ? ' ' : token.content
  return markdown.parse(text, {}).map(content).join(' ').replace(/\s+/g, ' ').trim()
}
