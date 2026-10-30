import { useEffect, useState } from 'react'
import { Icon } from './Icon'

export function CopyButton({ text, label, className = '' }: { text: string; label: string; className?: string }) {
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
    onClick={async () => {
      try {
        await navigator.clipboard.writeText(text)
        setResult({ status: 'copied' })
      } catch {
        setResult({ status: 'failed' })
      }
    }}
  >
    <Icon name={status === 'copied' ? 'check' : 'copy'} size={12} />
    <span role="status">{status === 'copied' ? 'Copied' : status === 'failed' ? 'Copy failed' : ''}</span>
  </button>
}
