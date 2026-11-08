import katex from 'katex'
import 'katex/dist/katex.min.css'

export function renderMath(node: HTMLElement) {
  katex.render(node.dataset.source!, node, {
    displayMode: node.dataset.display === 'true',
    output: 'htmlAndMathml',
    throwOnError: false,
    trust: false,
    strict: 'ignore',
    maxSize: 20,
    maxExpand: 1000,
  })
}
