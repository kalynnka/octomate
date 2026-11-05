import hljs from 'highlight.js/lib/common'

export function highlightCode(block: HTMLElement) {
  const language = block.dataset.language!
  const grammar = hljs.getLanguage(language)
  const code = block.querySelector('code')!
  if (grammar) {
    const caret = code.querySelector('.trk-print-caret')
    code.innerHTML = hljs.highlight(code.textContent!, { language, ignoreIllegals: true }).value
    if (caret) code.append(caret)
  }
  const name = grammar?.name || language
  const header = block.querySelector('.trk-code-header > span')
  if (header) {
    header.textContent = name
    block.querySelector('[role="region"]')!.setAttribute('aria-label', `${name} code`)
  }
}
