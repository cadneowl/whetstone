import { colourOf, edgeStyleOf, hollowColour, type GraphPalette } from '@/components/graph/types'

/**
 * What the colours, the marks and the dashes mean — driven by the palette, so a graph that grows a
 * kind cannot grow it without explaining it.
 *
 * A new mark with no key reads as a rendering artefact, which is the whole reason this is not
 * optional. `marks` is the per-graph half: each ring the canvas draws means a different kind of
 * trouble, and only the graph knows which.
 */
export function Legend({
  palette,
  marks,
  note,
}: {
  palette: GraphPalette
  marks: { label: string; help: string; swatch: 'dot' | 'ring' | 'dashed-ring'; colour: string }[]
  note: string
}) {
  const kinds = Object.keys(palette.colour)
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      {kinds.map((kind) => (
        <li key={kind} className="flex items-center gap-1.5" title={palette.help[kind] ?? kind}>
          <span
            className="inline-block h-2.5 w-2.5 rounded-full"
            style={
              kind === palette.hollow
                ? { border: `1.5px dashed ${hollowColour(palette)}` }
                : { backgroundColor: colourOf(palette, kind) }
            }
          />
          {kind}
        </li>
      ))}
      {marks.map((mark) => (
        <li key={mark.label} className="flex items-center gap-1.5" title={mark.help}>
          <span
            className={
              mark.swatch === 'dot'
                ? 'inline-block h-1.5 w-1.5 rounded-full'
                : 'inline-block h-2.5 w-2.5 rounded-full border'
            }
            style={
              mark.swatch === 'dot'
                ? { backgroundColor: mark.colour }
                : {
                    borderColor: mark.colour,
                    borderStyle: mark.swatch === 'dashed-ring' ? 'dashed' : 'solid',
                  }
            }
          />
          {mark.label}
        </li>
      ))}
      <li className="ml-auto">{note}</li>
    </ul>
  )
}

/**
 * What the dashes on an edge mean.
 *
 * Grouped rather than listed one per kind: the reader's question is "why is that line dotted", and
 * structural edges recede while authored ones do not — which is a distinction about two groups, not
 * about seven names. Each group's own kinds still carry their sentence on hover.
 */
export function EdgeLegend({
  palette,
  groups,
}: {
  palette: GraphPalette
  groups: { kinds: string[]; label: string; help: string }[]
}) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      {groups.map((group) => (
        <li key={group.label} className="flex items-center gap-1.5" title={group.help}>
          <svg width="26" height="8" aria-hidden="true">
            <line
              x1={0}
              y1={4}
              x2={26}
              y2={4}
              stroke="var(--color-muted)"
              strokeWidth={1.4}
              strokeDasharray={edgeStyleOf(palette, group.kinds[0] ?? '').dash}
            />
          </svg>
          {group.label}
        </li>
      ))}
    </ul>
  )
}
