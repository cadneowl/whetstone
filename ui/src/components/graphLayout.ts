/**
 * Where to draw a graph's nodes — Fruchterman-Reingold per connected component, packed, then
 * un-overlapped.
 *
 * **Deterministic on purpose, twice over.** Seeds come from a golden-angle spiral over the node
 * index rather than a PRNG, and the node order is the server's, which is itself sorted. So the same
 * query draws the same picture every time it is opened, on every machine — which is what makes a
 * screenshot of one worth pasting into a review, and what stops a re-render after a refetch from
 * quietly rearranging the graph somebody was reading.
 *
 * **Per component, and that is the fix for the blob.** One simulation over the whole result put
 * every unconnected pair on the boundary — mutual repulsion with nothing pulling back — and then
 * `fit` scaled the picture by a bounding box those outliers defined. A `issue:true` query on a real
 * skill therefore drew four legible pairs in the corners and crushed the other three hundred nodes
 * into a disc in the middle, where no arrangement of them was readable and no click could land on
 * the node you meant. Laying each component out in its own box and packing the boxes spends the
 * frame in proportion to what is in it, and guarantees two components cannot be drawn on top of
 * each other.
 *
 * **Then nothing overlaps.** A final pass pushes any two circles apart that are closer than their
 * radii allow, so a dot is always a dot you can hit rather than a smear of six. This runs after the
 * fit, because fitting rescales distances and would otherwise put the overlaps straight back.
 *
 * Kept out of the component and free of React so it can be tested as what it is: a pure function
 * from a graph to coordinates.
 */

export interface LayoutInputNode {
  id: string
}

export interface LayoutInputEdge {
  source: string
  target: string
}

export interface Point {
  x: number
  y: number
}

export interface LayoutOptions {
  width: number
  height: number
  /** Lowered automatically for large graphs — see `iterationsFor`. */
  iterations?: number
  /** Distance from an edge of the box to the nearest node centre. */
  padding?: number
}

/** The box that reads comfortably, and the node count it reads comfortably at. */
export const BASE_WIDTH = 900
export const BASE_HEIGHT = 460
const COMFORTABLE = 70

/**
 * The layout box for a graph of this size.
 *
 * A node's radius is in layout units and the SVG scales its box to the panel, so *growing the box
 * shrinks the dots* — which is the only lever that makes three hundred nodes fit a frame that holds
 * seventy. Area grows with the node count and the aspect ratio does not, so the panel keeps its
 * shape on screen and a bigger graph is simply drawn further away. Reading it up close is what the
 * zoom is for.
 */
export function boxFor(count: number): { width: number; height: number } {
  const scale = Math.sqrt(Math.max(1, count / COMFORTABLE))
  return { width: Math.round(BASE_WIDTH * scale), height: Math.round(BASE_HEIGHT * scale) }
}

/**
 * Iterations to spend on `count` nodes.
 *
 * The step is O(n²), so a fixed count that feels instant at forty nodes locks a browser tab at
 * four hundred. Falling with the square root keeps the total work roughly linear in the node count
 * and the layout quality roughly constant, because a bigger graph starts closer to its answer:
 * the spiral seed already separates the nodes it has to separate.
 */
export function iterationsFor(count: number): number {
  if (count <= 2) return 0
  return Math.max(40, Math.round(900 / Math.sqrt(count)))
}

/**
 * The distance a simulation tries to hold two connected nodes at.
 *
 * Everything else here is expressed against it: the clearance packed components get is wider than
 * one, so that two nodes with no edge between them are never drawn closer than two that have one.
 * That is the single claim the picture rests on — an edge has to mean proximity, or every reading
 * of the graph is wrong.
 */
const PITCH = 62

/** Clearance around a packed component. Wider than `PITCH`, for the reason above. */
const MARGIN = PITCH * 0.9

/** Space between two circles' edges once the overlap pass is done — room for the match ring. */
const CLEARANCE = 8

/** How hard the overlap pass tries. Each pass is O(n²) and the first two do nearly all the work. */
const SEPARATION_PASSES = 8

/**
 * One typed-array read.
 *
 * `noUncheckedIndexedAccess` widens every indexed read to `number | undefined`, which is right for
 * the objects and arrays it was turned on for and wrong for a `Float64Array` that was allocated at
 * a known length and is only ever indexed inside `for` loops bounded by that length. Written once
 * here rather than as a non-null assertion at forty call sites, which is both noisier and easier
 * to leave behind on a line where the bound *is* in question.
 */
const at = (values: Float64Array, index: number): number => values[index] as number

/**
 * Node positions, fitted to a `width` x `height` box.
 *
 * Isolated nodes are laid out too — a folder nobody links and a rule nothing excepts are both real
 * answers to a query, and dropping them would make the picture claim the graph is denser than it
 * is. They are packed as their own one-node components, which is why they end up in a legible band
 * rather than smeared around the rim.
 */
export function layout(
  nodes: LayoutInputNode[],
  edges: LayoutInputEdge[],
  options: LayoutOptions,
): Map<string, Point> {
  const { width, height, padding = 30 } = options
  const count = nodes.length
  const out = new Map<string, Point>()
  const only = nodes[0]
  if (!only) return out
  if (count === 1) {
    out.set(only.id, { x: width / 2, y: height / 2 })
    return out
  }

  const index = new Map<string, number>()
  nodes.forEach((node, i) => index.set(node.id, i))

  const links: Array<[number, number]> = []
  const degree = new Int32Array(count)
  const neighbours: number[][] = nodes.map(() => [])
  for (const edge of edges) {
    const a = index.get(edge.source)
    const b = index.get(edge.target)
    if (a === undefined || b === undefined || a === b) continue
    links.push([a, b])
    degree[a] = (degree[a] as number) + 1
    degree[b] = (degree[b] as number) + 1
    ;(neighbours[a] as number[]).push(b)
    ;(neighbours[b] as number[]).push(a)
  }

  const radii = new Float64Array(count)
  for (let i = 0; i < count; i += 1) radii[i] = radiusFor(degree[i] as number)

  const xs = new Float64Array(count)
  const ys = new Float64Array(count)
  const aspect = width / height
  const groups = componentsOf(count, neighbours)
  const boxes = groups.map((members) => {
    const local = new Map<number, number>()
    members.forEach((node, i) => local.set(node, i))
    const inside: Array<[number, number]> = []
    for (const [a, b] of links) {
      const la = local.get(a)
      const lb = local.get(b)
      if (la !== undefined && lb !== undefined) inside.push([la, lb])
    }
    return settle(members, inside, radii, xs, ys, aspect, options.iterations)
  })

  pack(boxes, aspect)
  for (let g = 0; g < boxes.length; g += 1) {
    const box = boxes[g] as ComponentBox
    for (const node of groups[g] as number[]) {
      xs[node] = at(xs, node) + box.x
      ys[node] = at(ys, node) + box.y
    }
  }

  fitInto(xs, ys, count, { width, height, padding })
  separate(xs, ys, radii, count, { width, height, padding })

  nodes.forEach((node, i) => out.set(node.id, { x: at(xs, i), y: at(ys, i) }))
  return out
}

/** The members of each connected component, in the node order the server sent. */
function componentsOf(count: number, neighbours: number[][]): number[][] {
  const seen = new Uint8Array(count)
  const groups: number[][] = []
  for (let start = 0; start < count; start += 1) {
    if (seen[start]) continue
    seen[start] = 1
    const members = [start]
    // A read cursor rather than `shift()`, which is O(n) per call and turns this into O(n²) on the
    // one graph size where it matters.
    for (let cursor = 0; cursor < members.length; cursor += 1) {
      for (const next of neighbours[members[cursor] as number] as number[]) {
        if (seen[next]) continue
        seen[next] = 1
        members.push(next)
      }
    }
    groups.push(members)
  }
  return groups
}

interface ComponentBox {
  /** Where the component's own origin lands, filled in by `pack`. */
  x: number
  y: number
  width: number
  height: number
  /** Ordering key for packing: bigger components are placed first. */
  area: number
}

/**
 * Lay one component out, writing its local coordinates into `xs`/`ys`, and report the box it wants.
 *
 * The box is sized from the component's own node count so a two-node pair is given a pair's worth
 * of room and a two-hundred-node hairball is given a hairball's, in the same units — which is what
 * makes packing them side by side mean anything.
 */
function settle(
  members: number[],
  links: Array<[number, number]>,
  radii: Float64Array,
  xs: Float64Array,
  ys: Float64Array,
  aspect: number,
  iterations?: number,
): ComponentBox {
  const size = members.length
  const width = Math.sqrt(size * PITCH * PITCH * aspect)
  const height = width / aspect
  const local = simulate(size, links, width, height, iterations ?? iterationsFor(size))

  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  for (let i = 0; i < size; i += 1) {
    const radius = at(radii, members[i] as number)
    minX = Math.min(minX, at(local.xs, i) - radius)
    maxX = Math.max(maxX, at(local.xs, i) + radius)
    minY = Math.min(minY, at(local.ys, i) - radius)
    maxY = Math.max(maxY, at(local.ys, i) + radius)
  }

  // Rebased so the component's own origin is its top-left corner; `pack` then only has to move a
  // box, and every node in it follows.
  for (let i = 0; i < size; i += 1) {
    const node = members[i] as number
    xs[node] = at(local.xs, i) - minX + MARGIN
    ys[node] = at(local.ys, i) - minY + MARGIN
  }

  const boxWidth = maxX - minX + MARGIN * 2
  const boxHeight = maxY - minY + MARGIN * 2
  return { x: 0, y: 0, width: boxWidth, height: boxHeight, area: boxWidth * boxHeight }
}

/** Fruchterman-Reingold over `count` nodes with the randomness taken out. */
function simulate(
  count: number,
  links: Array<[number, number]>,
  width: number,
  height: number,
  iterations: number,
): { xs: Float64Array; ys: Float64Array } {
  const xs = new Float64Array(count)
  const ys = new Float64Array(count)
  if (count === 1) {
    xs[0] = width / 2
    ys[0] = height / 2
    return { xs, ys }
  }

  // Golden-angle spiral: even coverage of the disc with no two seeds close together, from the
  // index alone. A grid would seed whole rows at identical y and give the first iterations
  // nothing to push apart along one axis.
  const spread = Math.min(width, height) / 2
  for (let i = 0; i < count; i += 1) {
    const angle = i * 2.39996322972865332 // π(3−√5)
    const radius = spread * Math.sqrt((i + 0.5) / count)
    xs[i] = width / 2 + radius * Math.cos(angle)
    ys[i] = height / 2 + radius * Math.sin(angle)
  }

  const k = Math.sqrt((width * height) / count)
  const dx = new Float64Array(count)
  const dy = new Float64Array(count)

  for (let step = 0; step < iterations; step += 1) {
    // Linear cooling from a tenth of the box to nothing. Cooling is what turns an oscillating
    // system into one that settles, and the last iterations only nudge.
    const temperature = (Math.min(width, height) / 10) * (1 - step / iterations)
    dx.fill(0)
    dy.fill(0)

    for (let i = 0; i < count; i += 1) {
      const xi = at(xs, i)
      const yi = at(ys, i)
      let fx = at(dx, i)
      let fy = at(dy, i)
      for (let j = i + 1; j < count; j += 1) {
        let vx = xi - at(xs, j)
        let vy = yi - at(ys, j)
        let distance = Math.hypot(vx, vy)
        if (distance < 0.01) {
          // Two nodes exactly on top of each other have no direction to separate along. Nudging by
          // the index rather than at random keeps the whole layout reproducible.
          vx = ((i % 7) - 3) / 10 || 0.1
          vy = ((j % 5) - 2) / 10 || 0.1
          distance = Math.hypot(vx, vy)
        }
        const force = (k * k) / distance / distance
        fx += vx * force
        fy += vy * force
        dx[j] = at(dx, j) - vx * force
        dy[j] = at(dy, j) - vy * force
      }
      dx[i] = fx
      dy[i] = fy
    }

    for (const [a, b] of links) {
      const vx = at(xs, a) - at(xs, b)
      const vy = at(ys, a) - at(ys, b)
      const distance = Math.max(0.01, Math.hypot(vx, vy))
      const force = distance / k
      dx[a] = at(dx, a) - vx * force
      dy[a] = at(dy, a) - vy * force
      dx[b] = at(dx, b) + vx * force
      dy[b] = at(dy, b) + vy * force
    }

    for (let i = 0; i < count; i += 1) {
      const fx = at(dx, i)
      const fy = at(dy, i)
      const magnitude = Math.max(0.01, Math.hypot(fx, fy))
      const limited = Math.min(magnitude, temperature)
      xs[i] = at(xs, i) + (fx / magnitude) * limited
      ys[i] = at(ys, i) + (fy / magnitude) * limited
    }
  }

  return { xs, ys }
}

/**
 * Arrange the component boxes into shelves, biggest first.
 *
 * Shelves rather than anything cleverer because the requirement is modest and the cost of being
 * wrong is high: the arrangement has to be compact enough that `fit` does not shrink the picture to
 * fill a mostly-empty bounding box, stable enough that adding a node does not reshuffle the screen,
 * and it must never overlap two boxes. Each shelf is centred, so a graph that is one big component
 * plus a tail of orphans reads as exactly that.
 */
function pack(boxes: ComponentBox[], aspect: number): void {
  if (boxes.length <= 1) return
  const order = boxes
    .map((_, i) => i)
    .sort((a, b) => {
      const difference = (boxes[b] as ComponentBox).area - (boxes[a] as ComponentBox).area
      return difference !== 0 ? difference : a - b
    })

  const total = boxes.reduce((sum, box) => sum + box.area, 0)
  const widest = boxes.reduce((most, box) => Math.max(most, box.width), 0)
  const target = Math.max(widest, Math.sqrt(total * aspect))

  const shelves: number[][] = []
  let shelf: number[] = []
  let cursor = 0
  for (const i of order) {
    const box = boxes[i] as ComponentBox
    if (shelf.length > 0 && cursor + box.width > target) {
      shelves.push(shelf)
      shelf = []
      cursor = 0
    }
    box.x = cursor
    shelf.push(i)
    cursor += box.width
  }
  if (shelf.length > 0) shelves.push(shelf)

  let top = 0
  for (const row of shelves) {
    const used = row.reduce((sum, i) => sum + (boxes[i] as ComponentBox).width, 0)
    const tallest = row.reduce((most, i) => Math.max(most, (boxes[i] as ComponentBox).height), 0)
    const offset = (target - used) / 2
    for (const i of row) {
      const box = boxes[i] as ComponentBox
      box.x += offset
      box.y = top + (tallest - box.height) / 2
    }
    top += tallest
  }
}

/**
 * Scale the settled positions to fill the box.
 *
 * The arrangement's own scale drifts with node count and connectivity, so a small graph would draw
 * as a dot in the middle of a large panel and a wide one would run off the edges. Fitting after
 * the fact means the picture always uses the space it has, and the aspect ratio is preserved so a
 * chain does not become a circle.
 */
function fitInto(
  xs: Float64Array,
  ys: Float64Array,
  count: number,
  box: { width: number; height: number; padding: number },
): void {
  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  for (let i = 0; i < count; i += 1) {
    minX = Math.min(minX, at(xs, i))
    maxX = Math.max(maxX, at(xs, i))
    minY = Math.min(minY, at(ys, i))
    maxY = Math.max(maxY, at(ys, i))
  }
  const usableWidth = box.width - box.padding * 2
  const usableHeight = box.height - box.padding * 2
  const spanX = Math.max(1e-6, maxX - minX)
  const spanY = Math.max(1e-6, maxY - minY)
  const scale = Math.min(usableWidth / spanX, usableHeight / spanY)
  // Centre whatever the aspect-preserving scale did not consume.
  const offsetX = box.padding + (usableWidth - spanX * scale) / 2
  const offsetY = box.padding + (usableHeight - spanY * scale) / 2

  for (let i = 0; i < count; i += 1) {
    xs[i] = offsetX + (at(xs, i) - minX) * scale
    ys[i] = offsetY + (at(ys, i) - minY) * scale
  }
}

/**
 * Push apart any two circles drawn closer than their radii allow.
 *
 * A node's radius is in layout units and does not scale with the box, so a fit that shrank the
 * arrangement can leave six dots occupying one dot's worth of screen — which is what "I click on a
 * circle and get nothing" actually is: the click landing on whichever of the six was drawn last.
 * Runs last, and clamps back inside the box each pass so nothing is pushed off the edge.
 */
function separate(
  xs: Float64Array,
  ys: Float64Array,
  radii: Float64Array,
  count: number,
  box: { width: number; height: number; padding: number },
): void {
  const low = box.padding / 2
  const highX = box.width - box.padding / 2
  const highY = box.height - box.padding / 2
  for (let pass = 0; pass < SEPARATION_PASSES; pass += 1) {
    let moved = false
    for (let i = 0; i < count; i += 1) {
      for (let j = i + 1; j < count; j += 1) {
        const least = at(radii, i) + at(radii, j) + CLEARANCE
        let vx = at(xs, j) - at(xs, i)
        let vy = at(ys, j) - at(ys, i)
        let distance = Math.hypot(vx, vy)
        if (distance >= least) continue
        if (distance < 0.01) {
          // Same nudge as the simulation's, and for the same reason: two nodes at one point have no
          // direction to separate along, and picking one from the indices keeps this reproducible.
          vx = ((i % 7) - 3) / 10 || 0.1
          vy = ((j % 5) - 2) / 10 || 0.1
          distance = Math.hypot(vx, vy)
        }
        const push = (least - distance) / 2
        const ux = (vx / distance) * push
        const uy = (vy / distance) * push
        xs[i] = clamp(at(xs, i) - ux, low, highX)
        ys[i] = clamp(at(ys, i) - uy, low, highY)
        xs[j] = clamp(at(xs, j) + ux, low, highX)
        ys[j] = clamp(at(ys, j) + uy, low, highY)
        moved = true
      }
    }
    if (!moved) return
  }
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value))
}

/** Radius for a node with this many edges — the eye's first read of what matters here. */
export function radiusFor(degree: number): number {
  return Math.min(11, 4 + Math.sqrt(Math.max(0, degree)) * 1.7)
}
