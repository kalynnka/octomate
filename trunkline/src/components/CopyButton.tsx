import { useEffect, useState, type ReactNode } from 'react'
import { Icon } from './Icon'
import { TextSwap } from './TextSwap'

export function CopyButton({ text, label, className = '', children, showIcon = true }: { text: string; label: string; className?: string; children?: ReactNode; showIcon?: boolean }) {
  const [pressed, setPressed] = useState(false)
  const [result, setResult] = useState<{ status: 'copied' | 'failed' } | null>(null)
  useEffect(() => {
    if (!result) return
    const timer = window.setTimeout(() => setResult(null), 1800)
    return () => window.clearTimeout(timer)
  }, [result])
  const status = result?.status
  return <button
    type="button"
    className={`trk-copy-button ${className}`}
    aria-label={label}
    title={status === 'copied' ? 'Copied' : status === 'failed' ? 'Copy failed' : label}
    data-state={status}
    data-pressed={pressed ? '' : undefined}
    onKeyDown={event => { if (event.key === ' ' || event.key === 'Enter') setPressed(true) }}
    onKeyUp={event => { if (event.key === ' ' || event.key === 'Enter') setPressed(false) }}
    onBlur={() => setPressed(false)}
    onClick={async () => {
      try {
        await navigator.clipboard.writeText(text)
        setResult({ status: 'copied' })
      } catch {
        setResult({ status: 'failed' })
      }
    }}
  >
    {children}
    {showIcon && <Icon name={status === 'copied' ? 'check' : 'copy'} size={12} />}
    <span role="status"><TextSwap>{status === 'copied' ? 'Copied' : status === 'failed' ? 'Copy failed' : ''}</TextSwap></span>
  </button>
}
