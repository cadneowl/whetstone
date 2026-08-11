import {
  colourOf,
  relationOf,
  type GraphPalette,
  type GraphViewEdge,
} from '@/components/graph/types'

/**
 * What one node is attached to, in words, with every neighbour one click away.
 *
 * The picture shows *that* a node has eleven edges; the size of its dot is that number and nothing
 * more. It cannot show which of them is the file the rule lives in, which is the rule that narrows
 * it, and which is the review both came out of — the lines are a dash pattern apart and they leave
 * the cluster in eleven directions. So the connectivity, which is the whole reason to draw a graph
 * at all, was the one thing a reader could not actually get out of it.
 *
 * Grouped by what the edge *means* from this end rather than by kind, because `contains` read
 * forwards and backwards are two different sentences and only one of them is ever the one being
 * asked. Ordered by the palette's own edge order, so the structural edges that recede in the picture
 * recede here too.
 */
export function Neighbours<E extends GraphViewEdge>({
  id,
  labels,
  kinds,
  edges,
  palette,
  onSelect,
}: {
  id: string
  /** Label for a node id — the graph owns what a node is called. */
  labels: (id: string) => string
  /** Kind for a node id, so a neighbour carries the colour it has in the picture. */
  kinds: (id: string) => string
  edges: E[]
  palette: GraphPalette
  onSelect: (id: string) => void
}) {
  const groups = groupsOf(id, edges, palette)
  if (groups.length === 0) {
    return (
      <p className="mt-3 border-t border-accent/20 pt-2 text-xs text-muted">
        Nothing points at this and it points at nothing — it is drawn on its own for that reason.
      </p>
    )
  }

  const total = groups.reduce((sum, group) => sum + group.ids.length, 0)
  return (
    <div className="mt-3 border-t border-accent/20 pt-2">
      <p className="mb-1.5 text-xs text-muted">
        {total} connection{total === 1 ? '' : 's'} — what this is attached to, and what is attached
        to it
      </p>
      <div className="max-h-56 space-y-1.5 overflow-y-auto pr-1">
        {groups.map((group) => (
          <div key={group.key} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span
              className="shrink-0 text-xs text-muted"
              title={palette.edgeHelp[group.kind] ?? group.kind}
            >
              {group.phrase}
            </span>
            {group.ids.map((neighbour) => (
              <button
                key={neighbour}
                type="button"
                onClick={() => onSelect(neighbour)}
                title={`Select ${labels(neighbour)}`}
                className="rounded border border-line/70 bg-canvas px-1.5 py-px text-left text-xs hover:border-accent"
              >
                <span
                  className="mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle"
                  style={{ backgroundColor: colourOf(palette, kinds(neighbour)) }}
                />
                {labels(neighbour)}
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

interface Group {
  key: string
  kind: string
  phrase: string
  ids: string[]
}

/**
 * The edges touching `id`, bucketed by kind and direction.
 *
 * Exported for its own test: this is the part that can be quietly wrong — a self-edge counted
 * twice, or a duplicate pair listing the same neighbour six times — and being quietly wrong here
 * means the card under the picture asserts a connection the graph does not have.
 */
export function groupsOf<E extends GraphViewEdge>(
  id: string,
  edges: E[],
  palette: GraphPalette,
): Group[] {
  const order = Object.keys(palette.edge)
  const buckets = new Map<string, Group>()
  for (const edge of edges) {
    const outgoing = edge.source === id
    const incoming = edge.target === id
    // A node linking to itself is one line in the picture and would otherwise be two rows here,
    // claiming the node is attached to something other than itself.
    if (!outgoing && !incoming) continue
    const other = outgoing ? edge.target : edge.source
    if (other === id) continue
    const key = `${edge.kind}|${outgoing ? 'out' : 'in'}`
    let group = buckets.get(key)
    if (!group) {
      group = {
        key,
        kind: edge.kind,
        phrase: relationOf(palette, edge.kind, outgoing),
        ids: [],
      }
      buckets.set(key, group)
    }
    if (!group.ids.includes(other)) group.ids.push(other)
  }

  // Palette order first — the structural edges that recede in the picture recede here too — then
  // outgoing before incoming, so what this node does is read before what is done to it.
  return [...buckets.values()].sort((a, b) => {
    const rank = order.indexOf(a.kind) - order.indexOf(b.kind)
    if (rank !== 0) return rank
    return Number(a.key.endsWith('|in')) - Number(b.key.endsWith('|in'))
  })
}
