import type { CSSProperties } from 'react'

/** Corner brackets — the dossier's clipped-document mark. Frames tool and file
 *  cards and the account pages; the parent must be positioned. */
export function Brackets() {
  const c = (pos: CSSProperties, borders: CSSProperties) => (
    <span style={{ position: 'absolute', width: 11, height: 11, ...pos, ...borders }} />
  )
  const b = '2px solid var(--trk-bracket)'
  return (
    <>
      {c({ top: -1, left: -1 }, { borderTop: b, borderLeft: b })}
      {c({ top: -1, right: -1 }, { borderTop: b, borderRight: b })}
      {c({ bottom: -1, left: -1 }, { borderBottom: b, borderLeft: b })}
      {c({ bottom: -1, right: -1 }, { borderBottom: b, borderRight: b })}
    </>
  )
}
