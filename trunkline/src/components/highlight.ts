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
  block.querySelector('.trk-code-header > span')!.textContent = name
  block.querySelector('[role="region"]')!.setAttribute('aria-label', `${name} code`)
}
