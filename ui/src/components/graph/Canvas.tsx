import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import {
  clampZoom,
  isWhole,
  panBy,
  toLayout,
  WHOLE,
  zoomAt,
  type View,
  type ViewPoint,
} from '@/components/graph/view'

/**
 * One graph, drawn — and operable: pan it, zoom it, drag a dot out of a cluster to see what it is
 * attached to.
 *
 * Kind-agnostic: the vocabulary comes in as a palette and two title functions. Lifted out of
 * `SidecarGraph.tsx` when the skill's own guidance grew a second graph, which is the argument for
 * one canvas rather than two: a layout bug is fixed once, and a reader who has learnt to read one
 * picture can read the other.
 *
 * Everything that *means* something is a parameter. What a kind is (`palette`), what makes a node
 * unhealthy (`rings`, `flag`), what a hover should say (`nodeTitle`, `edgeTitle`) — the canvas knows
 * none of it. What it knows is that a dot's size is its connectedness, that hollow means "names
 * something that is not there", and that a ring means "this matched".
 *
 * **Why it moves.** A still picture of three hundred nodes is a texture: the dots that matter are
 * behind other dots, the edge you are tracing leaves the cluster and comes back, and a click lands
 * on whichever circle was painted last. Zoom, pan and node dragging are not decoration on that —
 * they are the difference between a diagram you read and one you look at. Everything a reader
 * changes lives in local state and nothing here writes to the URL: a moved dot is a way of looking,
 * not a fact about the skill, and it is thrown away the moment the query behind the picture changes.
 */

/** Wheel notch to zoom factor. Tuned so one notch is a noticeable step and ten are not a jump. */
const WHEEL_SENSITIVITY = 0.0016

/** A pointer that travelled less than this never meant to pan, so the click still counts. */
const CLICK_SLOP = 3

/** Roughly half a long label at 10px — past this from an edge, centring clips it. */
const LABEL_REACH = 110

/**
 * Past this many nodes the anchor kinds stop drawing labels — a dozen file names in a hundred-node
 * drawing overlap into a smear, which is worse than no label. Hovering still answers, the selected
 * node and its neighbours are always named, and the list below the picture is where names are read
 * at that size.
 */
const LABEL_LIMIT = 60

/** Past this, no arrangement of dots is readable however well connected they are. */
export const CROWDED = 150

/**
 * Below this many edges per node a drawing is mostly unconnected dots.
 *
 * Which is the thing that actually makes a picture worthless, and it took shipping the wrong test to
 * see it. A broad query like `issue:true` can match more nodes than the limit allows, so `hops` adds
 * no neighbours at all and the result is edgeless — mutual repulsion with nothing pulling back, which
 * force layout renders as a ring of dots around the boundary. Meanwhile a 73-node graph with 73 edges
 * draws as legible clusters. Node count alone flagged the second and would have missed a sparse 50.
 */
const CONNECTED_ENOUGH = 0.5

/** The fewest nodes worth complaining about, however sparse. A handful of dots is readable. */
const SPARSE_FLOOR = 40

/** Whether this drawing is more texture than picture — by connectedness first, then by size. */
export function tooManyToRead(nodes: number, edges: number): boolean {
  if (nodes > CROWDED) return true
  return nodes > SPARSE_FLOOR && edges < nodes * CONNECTED_ENOUGH
}

/**
 * The most of a drawing that may be match-ringed before the ring stops meaning anything.
 *
 * A ring separates "matched" from "pulled in by `hops`". Once nearly everything drawn is a match the
 * distinction is empty, and the marks are noise that reads as emphasis.
 */
const RING_CEILING = 0.6

export function anchorFor(x: number, width: number): 'start' | 'middle' | 'end' {
  if (x < LABEL_REACH) return 'start'
  if (x > width - LABEL_REACH) return 'end'
  return 'middle'
}

type Point = ViewPoint

/** One step of the zoom buttons. Matches roughly three wheel notches. */
const BUTTON_STEP = 1.4

/**
 * Extra invisible radius on every dot, so a small one is still a target.
 *
 * At the server's 400-node cap the box is wide enough that a low-degree dot draws with a radius of
 * about three pixels, and a seven-pixel target is an attempt at a click rather than a click. Half
 * the layout's own clearance is the most that can be
 * added safely: `graphLayout.separate` guarantees two centres are at least the sum of their radii
 * plus that clearance apart, so padding each by half of it makes the targets meet and never
 * overlap. A hit area that swallowed its neighbour's would put back exactly the bug this fixes —
 * a click landing on a dot you did not point at.
 */
const HIT_PAD = 4

/**
 * Zoomed in this far, every dot gets its name.
 *
 * Labels are rationed at rest because two hundred of them overlap into a smear. But zooming is the
 * reader saying "this part, closer" — at 2.5× only a sixth of the picture is on screen, so the
 * labels have the room the whole graph never had. Without this, the way to find out what a dot is
 * called on a big graph is to hover it, one at a time, forever.
 */
const LABEL_ZOOM = 2.5

export function Canvas<N extends GraphViewNode, E extends GraphViewEdge>({
  nodes,
  edges,
  positions,
  box,
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
  positions: Map<string, Point>
  /** The layout box these positions were computed in — the SVG's own coordinate system. */
  box: { width: number; height: number }
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
  const svg = useRef<SVGSVGElement | null>(null)
  const [view, setView] = useState<View>(WHOLE)
  const [moved, setMoved] = useState<Map<string, Point>>(() => new Map())
  const [hovered, setHovered] = useState<string | null>(null)
  // Only so the cursor can say so. Kept apart from the gesture ref below, which changes on every
  // pointer move and must not re-render a three-hundred-node picture to do it.
  const [holding, setHolding] = useState(false)
  // Live gesture state. A ref rather than state because it changes on every pointer move and
  // nothing renders from it — re-rendering the whole graph to remember where a drag started would
  // make dragging a three-hundred-node picture stutter.
  const gesture = useRef<
    | { kind: 'pan'; from: Point; view: View; travelled: number }
    | { kind: 'node'; id: string; offset: Point }
    | null
  >(null)

  // A new picture is a new question, so the way you were looking at the old one is not carried
  // over.
  //
  // Keyed on *which nodes these are*, not on the identity of the `positions` map. Skills are read
  // from disk on every request and the console refetches on window focus, so an unchanged graph
  // arrives as a new object every time somebody alt-tabs back — and keying on identity threw away
  // the zoom and every dragged dot each time they did. The signature is O(n) over at most four
  // hundred ids, which is nothing next to the layout it guards.
  const shape = useMemo(() => nodes.map((node) => node.id).join('\n'), [nodes])
  useEffect(() => {
    setView(WHOLE)
    setMoved(new Map())
  }, [shape])

  /** A client point in the SVG's own coordinates, before pan and zoom. */
  const inBox = useCallback((event: { clientX: number; clientY: number }): Point | null => {
    const element = svg.current
    // `getScreenCTM` is the only conversion that survives `preserveAspectRatio` letterboxing, and
    // it is absent in jsdom — where there is no geometry to convert anyway.
    const matrix = element?.getScreenCTM?.()
    if (!element || !matrix || typeof DOMPoint === 'undefined') return null
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse())
    return { x: point.x, y: point.y }
  }, [])

  /** A client point in layout coordinates — where a dot dropped here would land. */
  const inLayout = useCallback(
    (event: { clientX: number; clientY: number }, current: View): Point | null => {
      const point = inBox(event)
      return point ? toLayout(point, current) : null
    },
    [inBox],
  )

  // Wheel is bound by hand because React registers `onWheel` passively, so `preventDefault` there
  // is ignored and the page scrolls away underneath the graph you were zooming.
  useEffect(() => {
    const element = svg.current
    if (!element) return
    function onWheel(event: WheelEvent) {
      event.preventDefault()
      setView((current) => {
        const factor = Math.exp(-event.deltaY * WHEEL_SENSITIVITY)
        const anchor = inBox(event)
        // Hold whatever is under the pointer still, which is what makes zooming feel like moving a
        // lens rather than resizing a picture. With no geometry to read, the scale still changes.
        if (!anchor) return { ...current, k: clampZoom(current.k * factor) }
        return zoomAt(current, anchor, factor)
      })
    }
    element.addEventListener('wheel', onWheel, { passive: false })
    return () => element.removeEventListener('wheel', onWheel)
  }, [inBox])

  const positionOf = useCallback(
    (id: string) => moved.get(id) ?? positions.get(id),
    [moved, positions],
  )

  // What the selection is attached to. The whole reason to click a dot is to find out, and dimming
  // everything else — as this used to do — dims the answer along with the noise.
  const neighbours = useMemo(() => {
    const map = new Map<string, Set<string>>()
    for (const edge of edges) {
      if (!map.has(edge.source)) map.set(edge.source, new Set())
      if (!map.has(edge.target)) map.set(edge.target, new Set())
      map.get(edge.source)?.add(edge.target)
      map.get(edge.target)?.add(edge.source)
    }
    return map
  }, [edges])

  // Hover previews, selection sticks. One rule for both so an edge, a label and a dot never
  // disagree about what is currently being asked about.
  const focus = hovered ?? selected
  const near = focus ? (neighbours.get(focus) ?? new Set<string>()) : null

  const roomy = nodes.length <= 14 || view.k >= LABEL_ZOOM
  const crowded = nodes.length > LABEL_LIMIT
  const labelled = (node: N) =>
    roomy ||
    node.id === selected ||
    node.id === focus ||
    Boolean(near?.has(node.id)) ||
    (!crowded && alwaysLabelled(node, palette))

  // A ring means "this one matched". A ring on *nearly* everything says nothing at all — the mark
  // exists to separate the matches from what `hops` pulled in around them, and when the two sets are
  // almost the same set it is decoration that reads as emphasis. `<` alone was not enough: a broad
  // query like `issue:true` matches 132 of 150 drawn nodes and satisfied it, then ringed the blob.
  const ringing = matched.size <= nodes.length * RING_CEILING

  function onPointerDown(event: React.PointerEvent<SVGSVGElement>) {
    if (event.button !== 0 || gesture.current) return
    const start = inBox(event)
    if (!start) return
    svg.current?.setPointerCapture(event.pointerId)
    gesture.current = { kind: 'pan', from: start, view, travelled: 0 }
    setHolding(true)
  }

  function onNodePointerDown(event: React.PointerEvent<SVGGElement>, node: N) {
    if (event.button !== 0) return
    const point = inLayout(event, view)
    const here = positionOf(node.id)
    if (!point || !here) return
    event.stopPropagation()
    svg.current?.setPointerCapture(event.pointerId)
    gesture.current = {
      kind: 'node',
      id: node.id,
      offset: { x: here.x - point.x, y: here.y - point.y },
    }
    setHolding(true)
  }

  function onPointerMove(event: React.PointerEvent<SVGSVGElement>) {
    const active = gesture.current
    if (!active) return
    if (active.kind === 'pan') {
      const now = inBox(event)
      if (!now) return
      // How far the pointer got from where it started, not how far it travelled getting there — a
      // sum would let a hand tremble its way past the click threshold without the picture moving.
      active.travelled = Math.max(
        active.travelled,
        Math.hypot(now.x - active.from.x, now.y - active.from.y),
      )
      // Recomputed from where the drag started rather than accumulated per move, so a hundred
      // pointer events cannot add up to a picture that has slid off its own gesture.
      setView(panBy(active.view, active.from, now))
      return
    }
    const point = inLayout(event, view)
    if (!point) return
    const next = { x: point.x + active.offset.x, y: point.y + active.offset.y }
    setMoved((current) => new Map(current).set(active.id, next))
  }

  function onPointerUp(event: React.PointerEvent<SVGSVGElement>) {
    const active = gesture.current
    gesture.current = null
    setHolding(false)
    svg.current?.releasePointerCapture?.(event.pointerId)
    // A pan that went nowhere was a click on the background, and clicking the background clears the
    // selection. Distinguished by distance rather than by time, because a slow careful click is
    // still a click and a fast flick is still a pan.
    if (active?.kind === 'pan' && active.travelled <= CLICK_SLOP) onSelect(null)
  }

  const rearranged = !isWhole(view) || moved.size > 0

  // Built once and reused while the reader pans.
  //
  // Panning changes only the transform on the group these sit in, but React re-renders the whole
  // component on every pointer move — and at the 400-node cap the edges are two thirds of the 2,800
  // elements on screen. Holding their element identity lets React skip the entire subtree, which is
  // the difference between a drag that tracks the pointer and one that catches up afterwards. It
  // works only because nothing below depends on the view: `vector-effect` is what takes the zoom out
  // of the stroke widths.
  const edgeLayer = useMemo(
    () =>
      edges.map((edge) => {
        const a = positionOf(edge.source)
        const b = positionOf(edge.target)
        if (!a || !b) return null
        const style = edgeStyleOf(palette, edge.kind)
        const touched = focus !== null && (edge.source === focus || edge.target === focus)
        return (
          <g key={`${edge.source}|${edge.target}|${edge.kind}`}>
            <line
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={touched ? 'var(--color-accent)' : 'var(--color-muted)'}
              strokeWidth={touched ? 1.6 : 1}
              vectorEffect="non-scaling-stroke"
              strokeDasharray={style.dash}
              opacity={focus !== null && !touched ? 0.12 : style.opacity}
            />
            {/* An invisible fat line over the thin one, so a 1px edge has a hoverable target. The
                legend explains the dash patterns as a class; this says what *this* line is, which is
                the question anyone actually has while looking at a specific pair of nodes. */}
            <line
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke="transparent"
              strokeWidth={8}
              vectorEffect="non-scaling-stroke"
            >
              <title>{edgeTitle(edge)}</title>
            </line>
          </g>
        )
      }),
    [edges, positionOf, focus, palette, edgeTitle],
  )

  return (
    <div className="relative">
      <svg
        ref={svg}
        viewBox={`0 0 ${box.width} ${box.height}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={ariaLabel}
        className="w-full touch-none rounded-lg border border-line bg-surface"
        style={{ maxHeight: '78vh', cursor: holding ? 'grabbing' : 'grab' }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          {edgeLayer}
          {nodes.map((node) => {
            const point = positionOf(node.id)
            if (!point) return null
            const radius = radiusFor(node.degree)
            const isMatch = ringing && matched.has(node.id)
            // Dimmed only if something is in focus and this is neither it nor next to it. The
            // neighbourhood staying lit is the answer to "what is this attached to".
            const dimmed = focus !== null && node.id !== focus && !near?.has(node.id)
            return (
              <g
                key={node.id}
                transform={`translate(${point.x} ${point.y})`}
                className="cursor-pointer"
                opacity={dimmed ? 0.25 : 1}
                onPointerDown={(event) => onNodePointerDown(event, node)}
                onPointerEnter={() => setHovered(node.id)}
                onPointerLeave={() =>
                  setHovered((current) => (current === node.id ? null : current))
                }
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
                {/* The target, which is bigger than the dot. `fill="transparent"` and not `"none"`:
                    the second draws nothing *and receives no pointer events*, which is the whole
                    job. Drawn first so the visible circle paints over it. */}
                <circle r={radius + HIT_PAD} fill="transparent" />
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
                {/* The selected dot says so on the picture. Without it, clicking one of six dots in a
                    cluster and reading a card below is an act of faith about which one you hit. */}
                {node.id === selected && (
                  <circle
                    r={radius + 10 + rings(node).length * 2.5}
                    fill="none"
                    stroke="var(--color-accent)"
                    strokeWidth={2}
                    strokeDasharray="3 2"
                  />
                )}
                {labelled(node) && (
                  <text
                    y={-radius - 5}
                    // Centred except near an edge, where half a centred label falls outside the SVG and
                    // is clipped — which is worse than an off-centre one, because the half that survives
                    // reads as the whole label.
                    textAnchor={anchorFor(point.x, box.width)}
                    className="fill-ink"
                    style={{ fontSize: 10 / view.k, pointerEvents: 'none' }}
                  >
                    {node.label.length > 34 ? `${node.label.slice(0, 33)}…` : node.label}
                  </text>
                )}
              </g>
            )
          })}
        </g>
      </svg>
      <div className="absolute top-2 right-2 flex items-center gap-1">
        <Control label="Zoom in" onClick={() => setView(zoomBy(box, BUTTON_STEP))}>
          +
        </Control>
        <Control label="Zoom out" onClick={() => setView(zoomBy(box, 1 / BUTTON_STEP))}>
          −
        </Control>
        {rearranged && (
          <Control
            label="Put every dot back where the layout put it, and show the whole graph"
            onClick={() => {
              setView(WHOLE)
              setMoved(new Map())
            }}
          >
            reset
          </Control>
        )}
      </div>
      <p className="mt-1 text-xs text-muted">
        Drag to pan · scroll to zoom · drag a dot out of a cluster to see what it is attached to ·
        click it for the detail below · double-click to redraw the graph around it
        {view.k !== 1 && <span className="ml-2 font-mono">{view.k.toFixed(1)}×</span>}
      </p>
    </div>
  )
}

/** Zoom about the centre of the frame, which is what a button — unlike a wheel — has to mean. */
function zoomBy(box: { width: number; height: number }, factor: number) {
  return (current: View): View => zoomAt(current, { x: box.width / 2, y: box.height / 2 }, factor)
}

function Control({
  label,
  onClick,
  children,
}: {
  label: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="rounded border border-line bg-canvas/90 px-2 py-0.5 text-xs text-muted hover:border-accent hover:text-ink"
    >
      {children}
    </button>
  )
}

/** See `GraphPalette.anchors`: the kinds a reader orients by keep their label at any node count. */
function alwaysLabelled(node: GraphViewNode, palette: GraphPalette): boolean {
  return palette.anchors.includes(node.kind)
}
