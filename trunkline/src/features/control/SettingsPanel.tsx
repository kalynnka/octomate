import { useConsole } from '@/state/console'
import { label, mono } from '@/components/text'

export function SettingsPanel() {
  const theme = useConsole((s) => s.theme)
  const interfaceSize = useConsole((s) => s.interfaceSize)
  const { setTheme, setInterfaceSize } = useConsole((s) => s.actions)

  return (
    <div className="trk-control-scroll">
      <fieldset className="trk-setting">
        <legend style={label(11)}>Appearance</legend>
        <p className="trk-control-note">Use a light or dark palette, or follow your system.</p>
        <div className="trk-setting-options">
          {([
            { value: 'light', name: 'Light', note: 'Warm paper', color: '#ede6d6' },
            { value: 'dark', name: 'Dark', note: 'Warm charcoal', color: '#34312c' },
            {
              value: 'auto', name: 'System', note: 'Follow device',
              color: 'linear-gradient(135deg, #ede6d6 50%, #34312c 50%)',
            },
          ] as const).map((option) => (
            <label
              key={option.value}
              title={option.note}
              className="trk-setting-option"
              data-selected={theme === option.value}
            >
              <input
                type="radio"
                name="theme"
                value={option.value}
                checked={theme === option.value}
                onChange={() => setTheme(option.value)}
              />
              <span className="trk-theme-swatch" style={{ background: option.color }} />
              <span style={mono(12, 700)}>{option.name}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset className="trk-setting">
        <legend style={label(11)}>Text &amp; interface size</legend>
        <p className="trk-control-note">
          Scale text and controls together, on top of the display’s automatic scaling.
        </p>
        <div className="trk-setting-options">
          {([
            { value: 'small', name: 'Small', note: '80%' },
            { value: 'standard', name: 'Standard', note: '90%' },
            { value: 'large', name: 'Large', note: '100%' },
          ] as const).map((option) => (
            <label
              key={option.value}
              className="trk-setting-option"
              data-selected={interfaceSize === option.value}
            >
              <input
                type="radio"
                name="interface-size"
                value={option.value}
                checked={interfaceSize === option.value}
                onChange={() => setInterfaceSize(option.value)}
              />
              <span style={mono(12, 700)}>{option.name}</span>
              <span style={{ ...mono(10), color: 'var(--fg-3)' }}>{option.note}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <p className="trk-control-note">Preferences are saved in this browser and apply immediately.</p>
    </div>
  )
}
