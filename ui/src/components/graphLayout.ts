/**
 * Where to draw a sidecar graph's nodes — Fruchterman-Reingold, with the randomness taken out.
 *
 * **Deterministic on purpose, twice over.** Seeds come from a golden-angle spiral over the node
 * index rather than a PRNG, and the node order is the server's, which is itself sorted. So the same
 * query draws the same picture every time it is opened, on every machine — which is what makes a
 * screenshot of one worth pasting into a review, and what stops a re-render after a refetch from
 * quietly rearranging the graph somebody was reading.
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
 * is.
 */
export function layout(
  nodes: LayoutInputNode[],
  edges: LayoutInputEdge[],
  options: LayoutOptions,
): Map<string, Point> {
  const { width, height, padding = 24 } = options
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

  // Golden-angle spiral: even coverage of the disc with no two seeds close together, from the
  // index alone. A grid would seed whole rows at identical y and give the first iterations
  // nothing to push apart along one axis.
  const spread = Math.min(width, height) / 2
  const xs = new Float64Array(count)
  const ys = new Float64Array(count)
  for (let i = 0; i < count; i += 1) {
    const angle = i * 2.39996322972865332 // π(3−√5)
    const radius = spread * Math.sqrt((i + 0.5) / count)
    xs[i] = width / 2 + radius * Math.cos(angle)
    ys[i] = height / 2 + radius * Math.sin(angle)
  }

  const links: Array<[number, number]> = []
  for (const edge of edges) {
    const a = index.get(edge.source)
    const b = index.get(edge.target)
    if (a === undefined || b === undefined || a === b) continue
    links.push([a, b])
  }

  const area = width * height
  const k = Math.sqrt(area / count)
  const iterations = options.iterations ?? iterationsFor(count)
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

  return fit(nodes, xs, ys, { width, height, padding })
}

/**
 * Scale the settled positions to fill the box.
 *
 * The simulation's own scale drifts with node count and connectivity, so a small graph would draw
 * as a dot in the middle of a large panel and a wide one would run off the edges. Fitting after
 * the fact means the picture always uses the space it has, and the aspect ratio is preserved so a
 * chain does not become a circle.
 */
function fit(
  nodes: LayoutInputNode[],
  xs: Float64Array,
  ys: Float64Array,
  box: { width: number; height: number; padding: number },
): Map<string, Point> {
  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  for (let i = 0; i < nodes.length; i += 1) {
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

  const out = new Map<string, Point>()
  nodes.forEach((node, i) => {
    out.set(node.id, {
      x: offsetX + (at(xs, i) - minX) * scale,
      y: offsetY + (at(ys, i) - minY) * scale,
    })
  })
  return out
}

/** Radius for a node with this many edges — the eye's first read of what matters here. */
export function radiusFor(degree: number): number {
  return Math.min(11, 4 + Math.sqrt(Math.max(0, degree)) * 1.7)
}
