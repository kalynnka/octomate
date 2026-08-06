import type { BarDatum } from '@/lib/api/types'
import { mono, label } from './text'

/** Lonetrail BarChart — flat fills over a 2px ink baseline. */
export function BarChart({
  data,
  height = 140,
  color = 'var(--color-teal)',
  showValues = true,
}: {
  data: BarDatum[]
  height?: number
  color?: string
  showValues?: boolean
}) {
  const max = Math.max(...data.map((d) => d.value), 1)
  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-end',
          gap: 14,
          height: height - 30,
          borderBottom: '2px solid var(--color-ink)',
        }}
      >
        {data.map((d) => (
          <div
            key={d.label}
            style={{
              flex: 1,
              maxWidth: 44,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'flex-end',
              gap: 4,
              height: '100%',
            }}
          >
            {showValues && (
              <span style={{ ...mono(10), color: 'var(--fg-2)' }}>{d.value}</span>
            )}
            <div
              style={{
                width: '100%',
                height: Math.max(2, Math.round((d.value / max) * (height - 30 - 18))),
                background: color,
              }}
            />
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 14, marginTop: 5 }}>
        {data.map((d) => (
          <span
            key={d.label}
            style={{
              flex: 1,
              maxWidth: 44,
              textAlign: 'center',
              ...label(9, '.1em'),
              color: 'var(--fg-3)',
            }}
          >
            {d.label}
          </span>
        ))}
      </div>
    </div>
  )
}
