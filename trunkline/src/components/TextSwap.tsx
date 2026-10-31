import { useState, type ComponentPropsWithoutRef } from 'react'

/** Replace a label without remounting its control or exposing the outgoing text to assistive technology. */
export function TextSwap({ children, className = '', ...props }: Omit<ComponentPropsWithoutRef<'span'>, 'children'> & { children: string | number }) {
  const [shown, setShown] = useState<{ value: string | number; previous: string | number | null; version: number }>({ value: children, previous: null, version: 0 })
  if (shown.value !== children) {
    setShown({ value: children, previous: shown.value, version: shown.version + 1 })
  }
  return <span {...props} className={`lt-text-swap ${className}`}>
    {shown.previous !== null && <span
      key={`old-${shown.version}`}
      aria-hidden="true"
      className="lt-text-old"
      onAnimationEnd={() => setShown(current => current.version === shown.version ? { ...current, previous: null } : current)}
    >{shown.previous}</span>}
    <span key={shown.version} className={shown.version ? 'lt-text-new' : undefined} data-text-current="">{children}</span>
  </span>
}
