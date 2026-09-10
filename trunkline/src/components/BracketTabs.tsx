import { label } from './text'

/** The comp's tab row: the selected label sits inside square brackets, and the
 *  unselected ones hold the brackets' space so nothing shifts as you switch. */
export function BracketTabs<Id extends string>({
  tabs,
  current,
  onPick,
}: {
  tabs: { id: Id; label: string }[]
  current: Id
  onPick: (id: Id) => void
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex' }}>
        {tabs.map((tab) => {
          const on = tab.id === current
          return (
            <button
              key={tab.id}
              type="button"
              aria-pressed={on}
              onClick={() => onPick(tab.id)}
              style={{
                flex: 1,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 4,
                cursor: 'pointer',
                paddingBottom: 10,
                border: 0,
                background: 'transparent',
                ...label(10, '.16em'),
                transition: 'color .15s',
                color: on ? 'var(--color-accent)' : 'var(--fg-3)',
              }}
            >
              <span style={{ opacity: on ? 1 : 0 }}>[</span>
              {tab.label}
              <span style={{ opacity: on ? 1 : 0 }}>]</span>
            </button>
          )
        })}
      </div>
      <i style={{ display: 'block', height: 1, background: 'var(--color-border)' }} />
    </div>
  )
}
