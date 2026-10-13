import { Fragment, useMemo } from 'react'
import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt()

/** Keep the textarea's character positions and line metrics while drawing Markdown. */
export function DraftMarkdown({ text }: { text: string }) {
  const blocks = useMemo(() => {
    const lines = new Map<number, string>()
    for (const token of markdown.parse(text, {})) {
      if (token.map && (token.type === 'heading_open' || token.type === 'list_item_open')) {
        lines.set(token.map[0], token.type)
      }
    }
    return lines
  }, [text])
  return text.split('\n').map((line, index) => {
    const heading = blocks.get(index) === 'heading_open'
    const prefix = heading ? line.match(/^ {0,3}#{1,6}(?:[ \t]+|$)/)?.[0] : undefined
    const bullet = blocks.get(index) === 'list_item_open' ? line.match(/^(\s*)[-+*](?=\s)/) : null
    return (
      <Fragment key={index}>
        {index > 0 && '\n'}
        <span style={heading ? { fontWeight: 700, color: 'var(--color-accent)' } : undefined}>
          {prefix ? <><span style={{ opacity: 0.3 }}>{prefix}</span>{line.slice(prefix.length)}</>
            : bullet ? <>{bullet[1]}<span style={{ color: 'var(--color-accent)' }}>•</span>{line.slice(bullet[0].length)}</>
              : line}
        </span>
      </Fragment>
    )
  })
}
