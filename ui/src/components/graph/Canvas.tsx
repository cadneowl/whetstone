import { radiusFor } from '@/components/graphLayout'
import {
  colourOf,
  edgeStyleOf,
  hollowColour,
  type GraphPalette,
  type GraphViewEdge,
  type GraphViewNode,
  type Ring,
} from '@/components/graph/types'

/**
 * One graph, drawn. Kind-agnostic: the vocabulary comes in as a palette and two title functions.
 *
 * Lifted out of `SidecarGraph.tsx` when the skill's own guidance grew a second graph. The reasoning
 * in every comment below was written for that first one and applies unchanged to both — which is the
 * argument for one canvas rather than two: a layout bug is fixed once, and a reader who has learnt to
 * read one picture can read the other.
 *
 * Everything that *means* something is a parameter. What a kind is (`palette`), what makes a node
 * unhealthy (`rings`, `flag`), what a hover should say (`nodeTitle`, `edgeTitle`) — the canvas knows
 * none of it. What it knows is that a dot's size is its connectedness, that hollow means "names
 * something that is not there", and that a ring means "this matched".
 */

// The layout box. Fixed rather than measured: the SVG scales to its container through `viewBox`, and
// measuring would make the positions depend on the panel's width — so the same query would draw
// differently on two screens, which is the one property this whole layout is built to avoid.
export const WIDTH = 900
export const HEIGHT = 460

/** Roughly half a long label at 10px — past this from an edge, centring clips it. */
const LABEL_REACH = 110

/**
 * Past this many nodes a picture stops being readable and starts being a texture.
 *
 * Two things key off it: the anchor kinds stop drawing labels (they would overlap into a smear), and
 * `tooManyToRead` lets a caller say so above the canvas rather than presenting a blob as an answer.
 * Chosen by looking — at ~60 the layout is still legible, and a broad `issue:true` on a real 12-page
 * skill draws 150 and is not.
 */
export const CROWDED = 60

/** Whether this many nodes is more picture than anyone can read. */
export function tooManyToRead(count: number): boolean {
  return count > CROWDED
}

/**
 * The most of a drawing that may be match-ringed before the ring stops meaning anything.
 *
 * A ring separates "matched" from "pulled in by `hops`". Once nearly everything drawn is a match the
 * distinction is empty, and the marks are noise that reads as emphasis.
 */
const RING_CEILING = 0.6

export function anchorFor(x: number): 'start' | 'middle' | 'end' {
  if (x < LABEL_REACH) return 'start'
  if (x > WIDTH - LABEL_REACH) return 'end'
  return 'middle'
}

export function Canvas<N extends GraphViewNode, E extends GraphViewEdge>({
  nodes,
  edges,
  positions,
  matched,
  selected,
  palette,
  rings,
  flag,
  nodeTitle,
  edgeTitle,
  ariaLabel,
  onSelect,
  onFocus,
}: {
  nodes: N[]
  edges: E[]
  positions: Map<string, { x: number; y: number }>
  matched: Set<string>
  selected: string | null
  palette: GraphPalette
  /** Concentric health rings for this node, outermost last. Empty for a healthy one. */
  rings: (node: N) => Ring[]
  /** A wedge in the corner: something is mechanically broken here, or inside it. */
  flag: (node: N) => boolean
  nodeTitle: (node: N) => string
  edgeTitle: (edge: E) => string
  ariaLabel: string
  onSelect: (id: string | null) => void
  onFocus: (node: N) => void
}) {
  // Labels for every node turn a 200-node graph into a wall of text, so most nodes earn one by being
  // selected. A small result set gets them all, because a dozen unlabelled dots is a picture of
  // nothing and the whole point of narrowing a query is to be able to read the answer.
  const roomy = nodes.length <= 14
  // Even the anchor kinds lose their labels once there are too many of them: thirteen file names in
  // a hundred-node blob overlap into an unreadable smear, which is worse than no label at all —
  // hovering still answers, and the node list below the picture is where names are read at this size.
  const crowded = nodes.length > CROWDED
  const labelled = (node: N) =>
    roomy || node.id === selected || (!crowded && alwaysLabelled(node, palette))

  // A ring means "this one matched". A ring on *nearly* everything says nothing at all — the mark
  // exists to separate the matches from what `hops` pulled in around them, and when the two sets are
  // almost the same set it is decoration that reads as emphasis. `<` alone was not enough: a broad
  // query like `issue:true` matches 132 of 150 drawn nodes and satisfied it, then ringed the blob.
  const ringing = matched.size <= nodes.length * RING_CEILING

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={ariaLabel}
      className="w-full rounded-lg border border-line bg-surface"
      onClick={() => onSelect(null)}
    >
      {edges.map((edge) => {
        const a = positions.get(edge.source)
        const b = positions.get(edge.target)
        if (!a || !b) return null
        const style = edgeStyleOf(palette, edge.kind)
        const touched = selected !== null && (edge.source === selected || edge.target === selected)
        return (
          <g key={`${edge.source}|${edge.target}|${edge.kind}`}>
            <line
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={touched ? 'var(--color-accent)' : 'var(--color-muted)'}
              strokeWidth={touched ? 1.6 : 1}
              strokeDasharray={style.dash}
              opacity={selected !== null && !touched ? 0.15 : style.opacity}
            />
            {/* An invisible fat line over the thin one, so a 1px edge has a hoverable target. The
                legend explains the dash patterns as a class; this says what *this* line is, which is
                the question anyone actually has while looking at a specific pair of nodes. */}
            <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="transparent" strokeWidth={8}>
              <title>{edgeTitle(edge)}</title>
            </line>
          </g>
        )
      })}
      {nodes.map((node) => {
        const point = positions.get(node.id)
        if (!point) return null
        const radius = radiusFor(node.degree)
        const isMatch = ringing && matched.has(node.id)
        const dimmed = selected !== null && node.id !== selected
        return (
          <g
            key={node.id}
            transform={`translate(${point.x} ${point.y})`}
            className="cursor-pointer"
            opacity={dimmed ? 0.4 : 1}
            onClick={(event) => {
              event.stopPropagation()
              onSelect(node.id === selected ? null : node.id)
            }}
            onDoubleClick={(event) => {
              event.stopPropagation()
              onFocus(node)
            }}
          >
            <title>{nodeTitle(node)}</title>
            <circle
              r={radius}
              fill={node.missing ? 'none' : colourOf(palette, node.kind)}
              stroke={node.missing ? hollowColour(palette) : 'var(--color-canvas)'}
              strokeWidth={node.missing ? 1.5 : 1}
              strokeDasharray={node.missing ? '3 2' : undefined}
            />
            {/* Health, on the picture rather than a click away. Each ring is a different *kind* of
                fact rather than a degree of one, which is why they are concentric and separate. */}
            {rings(node).map((ring, index) => (
              <circle
                key={index}
                r={radius + 3.5 + index * 2.5}
                fill="none"
                stroke={ring.colour}
                strokeWidth={ring.width}
                strokeDasharray={ring.dash}
              />
            ))}
            {/* A wedge rather than another ring: the rings above already mean different things here,
                and a third would be read as a degree of the same thing rather than a different kind
                of fact. This one is not about what the model is given — it is about the note or the
                rule being broken. */}
            {flag(node) && (
              <circle
                r={2.4}
                cx={radius * 0.75}
                cy={-radius * 0.75}
                fill="var(--color-bad)"
                stroke="var(--color-canvas)"
                strokeWidth={0.8}
              />
            )}
            {/* A match ring rather than a different fill: the kind is what the colour means, and a
                query must not be able to make one kind look like another. Outermost, so it never
                hides a health ring — a query is transient and a defect is not. */}
            {isMatch && (
              <circle
                r={radius + 6 + rings(node).length * 2.5}
                fill="none"
                stroke="var(--color-accent)"
                strokeWidth={1.4}
              />
            )}
            {labelled(node) && (
              <text
                y={-radius - 5}
                // Centred except near an edge, where half a centred label falls outside the SVG and
                // is clipped — which is worse than an off-centre one, because the half that survives
                // reads as the whole label.
                textAnchor={anchorFor(point.x)}
                className="fill-ink"
                style={{ fontSize: 10, pointerEvents: 'none' }}
              >
                {node.label.length > 34 ? `${node.label.slice(0, 33)}…` : node.label}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

/** See `GraphPalette.anchors`: the kinds a reader orients by keep their label at any node count. */
function alwaysLabelled(node: GraphViewNode, palette: GraphPalette): boolean {
  return palette.anchors.includes(node.kind)
}
