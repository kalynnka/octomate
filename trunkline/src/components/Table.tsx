import { useState } from 'react'
import type { ReactNode } from 'react'
import { label, mono } from './text'

/** One column of a dossier table.
 *
 *  The design system's own Table reads each cell as `row[column.key]`, because
 *  the design tool hands it untyped dicts. Here the rows are typed, so a column
 *  renders its own cell and the type checker sees every one; `key` is left to be
 *  what React needs and nothing more. */
export interface TableColumn<Row> {
  key: string
  label: string
  align?: 'left' | 'center' | 'right'
  /** Render the cell in the console's data voice — codes, endpoints, counts. */
  mono?: boolean
  width?: number | string
  render: (row: Row) => ReactNode
}

function Row<R>({
  row,
  columns,
  pad,
}: {
  row: R
  columns: TableColumn<R>[]
  pad: string
}) {
  const [hover, setHover] = useState(false)
  return (
    <tr
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        background: hover ? 'var(--card-bg-hover)' : 'transparent',
        transition: 'background .15s',
      }}
    >
      {columns.map((column) => (
        <td
          key={column.key}
          style={{
            padding: pad,
            borderBottom: '1px solid var(--line-divider)',
            textAlign: column.align ?? 'left',
            ...(column.mono
              ? { ...mono(12, 500), color: 'var(--fg-2)' }
              : { fontFamily: 'var(--font-sans)', fontSize: 13.5, color: 'var(--fg-1)' }),
          }}
        >
          {column.render(row)}
        </td>
      ))}
    </tr>
  )
}

/** Lonetrail data table — caps-mono header over a 2px ink rule, hairline rows.
 *
 *  Wide tables scroll in their own box rather than stretching the page: the
 *  control pages sit in a column whose width the rails decide. */
export function Table<R>({
  columns,
  rows,
  dense = false,
  className,
  empty,
  rowKey,
}: {
  columns: TableColumn<R>[]
  rows: R[]
  dense?: boolean
  className?: string
  /** Shown in place of the body when there is nothing to list. */
  empty?: string
  rowKey: (row: R) => string
}) {
  const pad = dense ? '7px 8px' : '11px 14px'
  return (
    <div className={`trk-table-scroll ${className ?? ''}`} tabIndex={0}>
      <table style={{ borderCollapse: 'separate', borderSpacing: 0, width: '100%' }}>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                style={{
                  position: 'sticky',
                  top: 0,
                  zIndex: 1,
                  background: 'var(--card-bg-hover)',
                  ...label(10, '.15em'),
                  color: 'var(--fg-2)',
                  textAlign: column.align ?? 'left',
                  padding: pad,
                  borderBottom: '2px solid var(--color-ink)',
                  width: column.width,
                  whiteSpace: 'nowrap',
                }}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Row key={rowKey(row)} row={row} columns={columns} pad={pad} />
          ))}
          {rows.length === 0 && (
            <tr>
              <td
                colSpan={columns.length}
                style={{
                  padding: '18px 14px',
                  textAlign: 'center',
                  ...label(8, '.18em'),
                  color: 'var(--fg-3)',
                  borderBottom: '1px solid var(--line-divider)',
                }}
              >
                {empty ?? '// nothing indexed'}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
