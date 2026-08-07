import type { CSSProperties } from 'react'
import { useConsole } from '@/state/console'
import { LedgerRow } from './cards'
import { label, mono, display, statusNote } from '@/components/text'

const CARD_MAX = 'var(--trk-card-max)'

export function ChatLog() {
  const detail = useConsole((s) => s.detail)
  const live = useConsole((s) => s.live)
  const notices = useConsole((s) => s.notices)
  const ledgerN = useConsole((s) => s.ledgerN)
  const ntOn = useConsole((s) => s.ntOn)
  const ntAgent = useConsole((s) => s.ntAgent)
  const ntModel = useConsole((s) => s.ntModel)
  const sbFold = useConsole((s) => s.sbFold)
  const mgmtOpen = useConsole((s) => s.mgmtOpen)
  const traceOn = useConsole((s) => s.traceOn) ?? true
  const pvOpen = useConsole((s) => s.pvOpen)
  const { onChatScroll } = useConsole((s) => s.actions)

  const cardMax = ['64%', '78%', '88%', '92%'][
    (sbFold ? 0 : 1) + (mgmtOpen ? 1 : 0) + (traceOn && !pvOpen ? 1 : 0)
  ]
  const ledger = detail?.ledger ?? []
  // Latest page first; scroll-top reveals earlier pages (loadOlder).
  const visible = ledger.slice(-ledgerN)
  const blank = ntOn && live.length === 0

  return (
    <div
      id="trk-chatlog"
      className="lt-fade-in"
      onScroll={(e) => onChatScroll(e.currentTarget.scrollTop)}
      style={
        {
          '--trk-card-max': cardMax,
          position: 'relative',
          padding: '20px 24px',
          display: 'flex',
          flexDirection: 'column',
          gap: 14,
          flex: 1,
          minHeight: 0,
          overflowY: 'auto',
        } as CSSProperties
      }
    >
      {ledger.length > 0 && !ntOn && visible.length >= ledger.length && (
        <div style={{ textAlign: 'center', ...label(8.5), color: 'var(--fg-3)', padding: '2px 0' }}>
          // start of thread
        </div>
      )}
      {ledger.length > 0 && !ntOn && visible.length < ledger.length && (
        <div style={{ textAlign: 'center', ...statusNote, color: 'var(--fg-3)', padding: '2px 0' }}>
          ↑ scroll — earlier messages load step by step
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {blank && (
          <div
            className="lt-fade-in"
            style={{
              margin: 'auto',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 10,
              textAlign: 'center',
              padding: '30px 0',
            }}
          >
            <span style={{ ...label(8, '.22em'), color: 'var(--fg-3)' }}>// blank ledger</span>
            <span style={{ ...display(16, 600), color: 'var(--fg-1)' }}>New trunkline thread</span>
            <span style={{ ...mono(9), lineHeight: 1.8, color: 'var(--fg-3)', maxWidth: 340 }}>
              no session yet — the first directive registers the thread and summons{' '}
              <span style={{ color: 'var(--color-accent)', fontWeight: 700 }}>
                {ntAgent} · {ntModel}
              </span>
              . set agent · model · effort in the input card below.
            </span>
          </div>
        )}
        {visible.map((item, i) => (
          // Motion runs newest-first: the stagger index counts up from the
          // bottom, so the latest page rises while earlier reveals stay calm.
          <LedgerRow
            key={item.uid}
            item={item}
            cardMax={CARD_MAX}
            i={Math.min(visible.length - 1 - i, 7)}
          />
        ))}
        {live.map((item) => (
          <LedgerRow key={item.uid} item={item} cardMax={CARD_MAX} />
        ))}
        {notices.map((text, i) => (
          <LedgerRow key={`nt${i}`} item={{ kind: 'notice', uid: `nt${i}`, text }} cardMax={CARD_MAX} />
        ))}
      </div>
    </div>
  )
}
