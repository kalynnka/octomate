import { useId, useRef, useState } from 'react'
import type { ChannelAddress } from '@/lib/api/events'
import { ApiError } from '@/lib/api/auth'
import { useAddresses, useChannels } from '@/lib/api/hooks'
import { channelMeta } from '@/lib/api/live'
import { ellipsis, label, mono } from '@/components/text'
import { addressRow, channelRows, level, sameAddress, type Crumb, type Destination, type DestinationRow } from './gateway'

const tint = (share: number) => `color-mix(in srgb, var(--color-accent) ${share}%, transparent)`

/**
 * Where a gateway op lands, browsed one level at a time: the connected
 * channels, then what each holds, fetched as it is opened. Suggestions the
 * relay already made for a channel head its first level.
 */
export function DestinationPicker({ threadId, sourceChannel, suggestions, selection, crumbs, open, onOpen, onCrumbs, onSelect }: {
  threadId: string
  sourceChannel: string
  suggestions: ChannelAddress[]
  selection: Destination | null
  crumbs: Crumb[]
  open: boolean
  onOpen: (open: boolean) => void
  onCrumbs: (crumbs: Crumb[]) => void
  onSelect: (destination: Destination) => void
}) {
  const [filter, setFilter] = useState('')
  const trigger = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const { data: channels } = useChannels()
  const at = crumbs.at(-1)
  const listing = useAddresses(threadId, at?.channel, at?.inside)
  const kind = at ? level(at.channel, crumbs.length - 1) : { many: 'surfaces', one: 'surface' }
  const noun = kind.many
  // A refusal says why this level has nothing to list; anything else failed.
  const refused = listing.error instanceof ApiError && listing.error.status === 409
  const loading = Boolean(at) && listing.isPending
  const suggested = crumbs.length === 1 ? suggestions.filter((one) => one.channel_tentacle_id === at?.channel) : []

  const rows: DestinationRow[] = at
    ? [...suggested, ...(listing.data ?? [])].map((address) => addressRow(address, crumbs))
    : channelRows(channels ?? [], suggestions, sourceChannel)
  const needle = filter.trim().toLowerCase()
  const shown = rows.filter((row) => !needle || row.label.toLowerCase().includes(needle))
  const go = (next: Crumb[]) => {
    onCrumbs(next)
    setFilter('')
  }

  return (
    <span
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || !open) return
        event.stopPropagation()
        onOpen(false)
        trigger.current?.focus()
      }}
      onBlur={(event) => {
        if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) onOpen(false)
      }}
      style={{ display: 'inline-flex', minWidth: 0, marginRight: 2 }}
    >
      {open && <span onClick={() => onOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 75 }} />}
      <button
        ref={trigger}
        type="button"
        aria-label="Destination"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        title="destination"
        onClick={() => onOpen(!open)}
        className="hov-border"
        style={{
          ...label(8, '.06em'), background: 'var(--surface-raised)', border: '1px solid var(--color-teal)',
          borderRadius: 2, height: 20, boxSizing: 'border-box', padding: '0 7px', display: 'inline-flex',
          alignItems: 'center', gap: 6, cursor: 'pointer', whiteSpace: 'nowrap', maxWidth: 300, minWidth: 0,
          position: 'relative', zIndex: 76,
        }}
      >
        <i style={{ width: 6, height: 6, flexShrink: 0, background: selection ? channelMeta(selection.address.channel_tentacle_id).brand : 'var(--line-divider)' }} />
        <span style={{ minWidth: 0, ...ellipsis, color: selection ? 'var(--fg-1)' : 'var(--fg-3)' }}>
          {selection ? selection.path.join(' / ') : 'choose destination'}
        </span>
        <span style={{ fontSize: 7, color: 'var(--fg-3)', lineHeight: 1, marginTop: 1 }}>▾</span>
      </button>
      {open && (
        <span
          id={menuId}
          role="group"
          aria-label="Destinations"
          className="lt-menu"
          data-open=""
          style={{
            position: 'absolute', bottom: 'calc(100% + 6px)', right: 12, width: 348, maxWidth: 'calc(100% - 24px)',
            zIndex: 80, background: 'var(--surface-raised)', border: '1px solid var(--line-color)',
            boxShadow: 'var(--shadow-soft)', display: 'flex', flexDirection: 'column',
          }}
        >
          <span style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '7px 10px', borderBottom: '1px solid var(--line-divider)', flexShrink: 0 }}>
            {at && (
              <button
                type="button"
                aria-label="Up one level"
                title="Up one level"
                onClick={() => go(crumbs.slice(0, -1))}
                className="hov-accent-border-wash"
                style={{
                  width: 20, height: 20, flexShrink: 0, boxSizing: 'border-box', padding: 0, cursor: 'pointer',
                  border: '1px solid var(--line-divider)', borderRadius: 0, background: 'transparent',
                  fontFamily: 'var(--font-display)', fontSize: 10, color: 'var(--fg-2)',
                }}
              >
                ←
              </button>
            )}
            <span style={{ flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 5, overflow: 'hidden', whiteSpace: 'nowrap' }}>
              {[{ label: 'Surfaces' }, ...crumbs].map((crumb, index) => {
                const last = index === crumbs.length
                return (
                  <span key={index} style={{ display: 'contents' }}>
                    <button
                      type="button"
                      aria-current={last ? 'location' : undefined}
                      onClick={() => go(crumbs.slice(0, index))}
                      className="hov-accent"
                      style={{
                        ...label(8, '.12em'), color: last ? 'var(--fg-1)' : 'var(--fg-3)', cursor: 'pointer',
                        padding: 0, border: 0, background: 'transparent', minWidth: 0, ...ellipsis,
                        flexShrink: index === 0 || last ? 0 : 1,
                      }}
                    >
                      {crumb.label}
                    </button>
                    {!last && <span aria-hidden="true" style={{ ...mono(8), color: 'var(--fg-3)' }}>/</span>}
                  </span>
                )
              })}
            </span>
            <span style={{ ...mono(7.5), color: 'var(--fg-3)', letterSpacing: '.06em', whiteSpace: 'nowrap' }}>
              {loading ? noun : `${rows.length} ${rows.length === 1 ? kind.one : noun}`}
            </span>
          </span>
          {loading && <span className="lt-scanbar" style={{ display: 'block', height: 2 }} />}
          <label className="trk-gateway-filter" style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '6px 12px', borderBottom: '1px solid var(--line-color)', flexShrink: 0 }}>
            <span aria-hidden="true" style={{ ...mono(9), color: 'var(--fg-3)' }}>⌕</span>
            <input
              // Opening the picker is asking where to go; the note can wait.
              autoFocus
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              aria-label={`Filter ${noun}`}
              placeholder={`filter ${noun}…`}
              style={{ flex: 1, minWidth: 0, border: 0, background: 'transparent', ...mono(10), color: 'var(--fg-1)', padding: '2px 0' }}
            />
          </label>
          <span style={{ display: 'block', maxHeight: 248, overflowY: 'auto', overscrollBehavior: 'contain', borderBottom: '1px solid var(--line-color)' }}>
            {shown.map((row) => {
              const on = Boolean(row.address && selection && sameAddress(row.address, selection.address))
              return (
                <button
                  key={row.key}
                  type="button"
                  disabled={Boolean(row.barred)}
                  aria-pressed={row.address ? on : undefined}
                  title={row.barred ? undefined : row.open ? `Open ${row.label}` : 'Land here'}
                  onClick={() => {
                    if (row.open) return go([...crumbs, row.open])
                    onSelect({ address: row.address!, path: [...crumbs.map((crumb) => crumb.label), row.label] })
                  }}
                  className={row.barred ? undefined : 'hov-wash'}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 9, width: '100%', padding: '7px 12px',
                    cursor: row.barred ? 'not-allowed' : 'pointer', opacity: row.barred ? 0.45 : 1,
                    border: 0, borderBottom: '1px solid var(--line-color)', borderRadius: 0, textAlign: 'left',
                    background: on ? tint(7) : 'transparent', color: 'inherit',
                  }}
                >
                  <span style={{
                    width: 20, height: 20, flexShrink: 0, boxSizing: 'border-box', ...mono(8, 700),
                    border: `1px solid ${row.brand ?? (on ? 'var(--color-accent)' : 'var(--line-divider)')}`,
                    color: row.brand ?? (row.open ? 'var(--fg-2)' : on ? 'var(--color-accent)' : 'var(--color-teal)'),
                    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    {row.glyph}
                  </span>
                  <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
                    <span style={{ ...mono(10, 700), color: 'var(--fg-1)', ...ellipsis }}>{row.label}</span>
                    <span style={{ ...mono(8), color: 'var(--fg-3)', ...ellipsis }}>{row.sub}</span>
                  </span>
                  {row.here && <span style={{ ...label(7.5, '.14em'), color: 'var(--color-accent)', whiteSpace: 'nowrap' }}>● here</span>}
                  <span aria-hidden="true" style={{ width: 12, flexShrink: 0, textAlign: 'right', ...mono(11, 700), color: on ? 'var(--color-accent)' : 'var(--fg-3)' }}>
                    {on ? '✓' : row.open ? '›' : ''}
                  </span>
                </button>
              )
            })}
            {loading && (
              <span role="status">
                {['62%', '48%', '70%'].map((width) => (
                  <span key={width} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '8px 12px', borderBottom: '1px solid var(--line-color)' }}>
                    <span className="lt-skeleton" style={{ display: 'block', width: 20, height: 20, flexShrink: 0 }} />
                    <span style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 5 }}>
                      <span className="lt-skeleton" style={{ display: 'block', height: 8, width }} />
                      <span className="lt-skeleton" style={{ display: 'block', height: 6, width: '40%' }} />
                    </span>
                  </span>
                ))}
                <span style={{ display: 'block', padding: '7px 12px', ...mono(8), color: 'var(--fg-3)', letterSpacing: '.06em' }}>
                  fetching {noun} from {crumbs[0].label}…
                </span>
              </span>
            )}
            {listing.isError && refused && (
              <span role="status" style={{ display: 'block', padding: '10px 12px', ...mono(9), color: 'var(--fg-3)', lineHeight: 1.5 }}>
                {listing.error.message}
              </span>
            )}
            {listing.isError && !refused && (
              <span role="alert" style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '10px 12px', borderBottom: '1px solid var(--line-color)' }}>
                <span aria-hidden="true" style={{ ...mono(9, 700), color: 'var(--color-red)' }}>!</span>
                <span style={{ flex: 1, ...mono(9), color: 'var(--fg-2)', lineHeight: 1.5 }}>
                  Couldn't load {noun} — {listing.error.message}
                </span>
                <button
                  type="button"
                  onClick={() => void listing.refetch()}
                  className="hov-accent-fill"
                  style={{
                    ...label(8, '.14em'), color: 'var(--color-accent)', border: '1px solid var(--color-accent)',
                    borderRadius: 0, background: 'transparent', padding: '3px 8px', cursor: 'pointer',
                  }}
                >
                  Retry
                </button>
              </span>
            )}
            {!loading && !listing.isError && shown.length === 0 && (
              <span role="status" style={{ display: 'block', padding: 12, ...mono(9), color: 'var(--fg-3)' }}>
                {needle ? `No loaded ${noun} match — clear the filter.` : `No ${noun} to list here.`}
              </span>
            )}
          </span>
          <span style={{ display: 'block', padding: '7px 12px', flexShrink: 0, ...mono(7.5), color: 'var(--fg-3)', letterSpacing: '.04em', lineHeight: 1.5, ...ellipsis }}>
            {selection ? `→ ${selection.path.join(' / ')}` : 'pick a destination · levels load from the gateway as you open them'}
          </span>
        </span>
      )}
    </span>
  )
}
