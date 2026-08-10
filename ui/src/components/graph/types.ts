/**
 * The least a graph has to be for `Canvas` to draw it.
 *
 * Two graphs now share this canvas — the source tree's `.agents/` notes and the skill's own
 * guidance — and they agree about almost nothing except the shape of the picture. So the canvas is
 * told about the *drawing*: a dot per node, sized by connectedness, coloured by kind, hollow when it
 * names something that is not there; a line per edge, dashed by kind. Everything that means anything
 * — what a kind *is*, what makes a node unhealthy, what a hover should say — comes in as data.
 *
 * `kind` is a plain `string` rather than a union on purpose. Each graph has its own closed set of
 * kinds and keeps its own literal type for them; widening here is what lets one canvas serve both
 * without knowing either vocabulary. The cost is that a palette lookup can miss, which is why every
 * accessor below takes a fallback.
 */

export interface GraphViewNode {
  id: string
  kind: string
  label: string
  /** How many edges touch this node. The eye's first read of what matters here. */
  degree: number
  /** Names something that is not in the tree, or not readable. Drawn hollow. */
  missing: boolean
}

export interface GraphViewEdge {
  source: string
  target: string
  kind: string
  detail?: string
}

/** A concentric ring around a node — one fact about its health, not a degree of another one. */
export interface Ring {
  colour: string
  width: number
  dash?: string
}

/**
 * What a kind looks like and what it means.
 *
 * `hollow` names the kind whose colour is the graph's "this is broken" colour, so a missing node can
 * be outlined in it without the canvas having an opinion about which kind that is.
 */
export interface GraphPalette {
  colour: Record<string, string>
  help: Record<string, string>
  hollow: string
  /**
   * Kinds that keep their label however many nodes are on screen — the map a reader orients by.
   *
   * A folder in the sidecar graph, a file in the skill graph. Everything else earns a label by being
   * selected or by the result set being small enough to read, because labelling two hundred dots is
   * a wall of text.
   */
  anchors: string[]
  edge: Record<string, { opacity: number; dash?: string }>
  edgeHelp: Record<string, string>
}

/** A node's colour, or the muted default when a graph grows a kind its palette has not learnt. */
export function colourOf(palette: GraphPalette, kind: string): string {
  return palette.colour[kind] ?? 'var(--color-muted)'
}

export function hollowColour(palette: GraphPalette): string {
  return palette.colour[palette.hollow] ?? 'var(--color-bad)'
}

export function edgeStyleOf(
  palette: GraphPalette,
  kind: string,
): { opacity: number; dash?: string } {
  return palette.edge[kind] ?? { opacity: 0.4 }
}
