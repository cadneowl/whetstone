import { useMemo, useState } from 'react'
import { parseDiff, type DiffLine } from './parse'

export interface Overlay {
  /**
   * Inclusive new-file line range, matching `Region.line_range` on the server.
   *
   * A server-side region with no `line_range` means "anywhere in this file". Callers express that
   * by opening the range to `Number.MAX_SAFE_INTEGER`, which is right for deciding what to
   * highlight and wrong to ever show a reader — the legend printed a literal
   * `lines 1–9007199254740991`. `wholeFile` says so in words instead.
   */
  range: [number, number]
  wholeFile?: boolean
  path?: string
  kind: 'expectation' | 'finding'
  label?: string
  tone?: 'good' | 'bad' | 'warn' | 'accent'
}

export type Selection = { path: string; range: [number, number] } | null

interface Props {
  diff: string
  overlays?: Overlay[]
  selection?: Selection
  /** Enables drag-to-select on the line gutter. Omit for a read-only diff. */
  onSelect?: (selection: Selection) => void
}

const TONE_BG: Record<NonNullable<Overlay['tone']>, string> = {
  good: 'bg-good/10',
  bad: 'bg-bad/10',
  warn: 'bg-warn/10',
  accent: 'bg-accent/10',
}

const TONE_BAR: Record<NonNullable<Overlay['tone']>, string> = {
  good: 'bg-good',
  bad: 'bg-bad',
  warn: 'bg-warn',
  accent: 'bg-accent',
}

/**
 * A unified diff with regions highlighted, and optionally selectable, by new-file line number.
 *
 * Custom rather than an off-the-shelf viewer because the line-number mapping *is* the feature:
 * expectations and findings are anchored to new-file lines, and selection has to produce a range in
 * the same coordinate space the server validates against.
 */
export function DiffView({ diff, overlays = [], selection = null, onSelect }: Props) {
  const files = useMemo(() => parseDiff(diff), [diff])
  const [dragging, setDragging] = useState<{ path: string; anchor: number } | null>(null)

  if (files.length === 0) {
    return <p className="p-4 text-sm text-muted italic">This case carries no diff.</p>
  }

  const selectable = Boolean(onSelect)

  function begin(path: string, line: number) {
    if (!onSelect) return
    setDragging({ path, anchor: line })
    onSelect({ path, range: [line, line] })
  }

  function extend(path: string, line: number) {
    if (!onSelect || !dragging || dragging.path !== path) return
    const lo = Math.min(dragging.anchor, line)
    const hi = Math.max(dragging.anchor, line)
    onSelect({ path, range: [lo, hi] })
  }

  return (
    <div
      className="overflow-hidden rounded-lg border border-line"
      onPointerUp={() => setDragging(null)}
      onPointerLeave={() => setDragging(null)}
    >
      {files.map((file) => {
        const applicable = overlays.filter((o) => !o.path || o.path === file.path)
        const fileSelection = selection && selection.path === file.path ? selection.range : null
        return (
          <div key={file.path}>
            <div className="flex items-center gap-3 border-b border-line bg-surface px-3 py-1.5 font-mono text-xs text-muted">
              <span>{file.path}</span>
              {selectable && (
                <span className="ml-auto font-sans text-[11px] not-italic">
                  drag the line numbers to set the region
                </span>
              )}
            </div>
            <div className="overflow-x-auto">
              <table
                className={`w-full border-collapse font-mono text-[13px] ${
                  dragging ? 'select-none' : ''
                }`}
              >
                <tbody>
                  {file.lines.map((line, i) => (
                    <Row
                      key={i}
                      line={line}
                      overlays={applicable}
                      selection={fileSelection}
                      selectable={selectable}
                      onBegin={(n) => begin(file.path, n)}
                      onExtend={(n) => extend(file.path, n)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )
      })}
      {overlays.length > 0 && <Legend overlays={overlays} />}
    </div>
  )
}

function Row({
  line,
  overlays,
  selection,
  selectable,
  onBegin,
  onExtend,
}: {
  line: DiffLine
  overlays: Overlay[]
  selection: [number, number] | null
  selectable: boolean
  onBegin: (line: number) => void
  onExtend: (line: number) => void
}) {
  if (line.kind === 'hunk' || line.kind === 'meta') {
    return (
      <tr>
        <td colSpan={3} className="bg-surface px-3 py-1 text-xs text-muted select-none">
          {line.content}
        </td>
      </tr>
    )
  }

  const number = line.newLine
  const covering = number === null ? [] : overlays.filter((o) => within(o.range, number))
  const first = covering[0]
  const selected = selection !== null && number !== null && within(selection, number)

  const marker = line.kind === 'add' ? '+' : line.kind === 'del' ? '-' : ' '
  const lineTint = line.kind === 'add' ? 'bg-good/5' : line.kind === 'del' ? 'bg-bad/5' : ''
  const overlayTint = first ? TONE_BG[first.tone ?? 'accent'] : ''
  const selectedTint = selected ? 'bg-accent/20' : ''

  const label = covering
    .map((o) => o.label)
    .filter(Boolean)
    .join(' · ')

  return (
    <tr className={`${lineTint} ${overlayTint} ${selectedTint}`} title={label || undefined}>
      <td className="w-1 p-0">
        {first && <div className={`h-full w-[3px] ${TONE_BAR[first.tone ?? 'accent']}`} />}
      </td>
      <td
        className={`w-12 border-r border-line px-2 text-right align-top text-xs text-muted tabular ${
          selectable && number !== null ? 'cursor-row-resize hover:bg-accent/20' : 'select-none'
        }`}
        onPointerDown={
          selectable && number !== null
            ? (e) => {
                e.preventDefault()
                onBegin(number)
              }
            : undefined
        }
        onPointerEnter={selectable && number !== null ? () => onExtend(number) : undefined}
      >
        {number ?? ''}
      </td>
      <td className="px-3 whitespace-pre">
        <span className="mr-1 text-muted select-none">{marker}</span>
        {line.content}
      </td>
    </tr>
  )
}

function Legend({ overlays }: { overlays: Overlay[] }) {
  const labelled = overlays.filter((o) => o.label)
  if (labelled.length === 0) return null
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 border-t border-line bg-surface px-3 py-2 text-xs text-muted">
      {labelled.map((o, i) => (
        <li key={i} className="flex items-center gap-1.5">
          <span className={`inline-block h-2 w-2 rounded-sm ${TONE_BAR[o.tone ?? 'accent']}`} />
          <span>
            {o.label}
            <span className="ml-1 opacity-70">
              {o.wholeFile ? '(anywhere in this file)' : `(lines ${o.range[0]}–${o.range[1]})`}
            </span>
          </span>
        </li>
      ))}
    </ul>
  )
}

function within([lo, hi]: [number, number], line: number): boolean {
  return line >= lo && line <= hi
}
