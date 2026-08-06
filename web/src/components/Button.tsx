import type { CSSProperties, ReactNode } from 'react'

const variantClass: Record<string, string> = {
  outline: 'hov-panel',
  solid: 'hov-accent-fill',
  accent: 'hov-panel',
  ghost: 'hov-wash',
}

const variantStyle: Record<string, CSSProperties> = {
  outline: { background: 'transparent', color: 'var(--color-ink)', borderColor: 'var(--color-ink)' },
  solid: { background: 'var(--panel)', color: 'var(--on-panel)', borderColor: 'var(--color-ink)' },
  accent: { background: 'var(--color-accent)', color: '#fff', borderColor: 'var(--color-accent)' },
  ghost: { background: 'transparent', color: 'var(--color-ink)', borderColor: 'transparent' },
}

/** Lonetrail Button — mono label chip with 150ms color-swap hover. */
export function Button({
  variant = 'outline',
  onClick,
  title,
  style,
  children,
}: {
  variant?: 'outline' | 'solid' | 'accent' | 'ghost'
  onClick?: () => void
  title?: string
  style?: CSSProperties
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={variantClass[variant]}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        fontWeight: 700,
        textTransform: 'uppercase',
        letterSpacing: '0.2em',
        padding: '9px 18px',
        border: '1px solid',
        cursor: 'pointer',
        transition: 'background var(--motion-fast) linear, color var(--motion-fast) linear, border-color var(--motion-fast) linear',
        ...variantStyle[variant],
        ...style,
      }}
    >
      {children}
    </button>
  )
}
