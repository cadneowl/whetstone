import { useEffect, useMemo, useState } from 'react'
import {
  useSidecarFile,
  useSidecarGraph,
  type GraphEdge,
  type GraphEdgeKind,
  type GraphNode,
  type GraphNodeKind,
  type SidecarFile,
} from '@/api/client'
import { layout, radiusFor } from '@/components/graphLayout'
import { Badge, ErrorNote } from '@/components/primitives'

/**
 * The graph a source tree's `.agents/` notes already form, and a box to ask it questions.
 *
 * The panel above this one counts the notes. This says what they are *about* — which rule a folder
 * excepts, which review a claim came out of, which file a section describes, which other folder a
 * claim says its invariant also holds in. All four are in the files today and were visible nowhere,
 * so the question anyone deciding where to write the next note actually asks — *what does this tree
 * already know, and where are the holes* — had no answer short of grepping a monorepo.
 *
 * **It reads; it does not shape a review.** Retrieval is still the ancestor walk in `collect.py`
 * and no hash moves because of anything on this screen. Widening what a reviewer is given is a
 * separate decision that needs a scored with-graph arm first, and a picture is not that arm.
 */
export function SidecarGraph({ skillId }: { skillId: string }) {
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  const [hops, setHops] = useState(1)
  const [selected, setSelected] = useState<string | null>(null)

  // Debounced, because every keystroke is a filesystem walk on somebody's monorepo — cached, but
  // still a walk. 250ms is under the threshold where a person notices waiting and well over the
  // gap between two keys in a word.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(draft.trim()), 250)
    return () => clearTimeout(timer)
  }, [draft])

  const { data, isLoading, error } = useSidecarGraph(skillId, query, hops, true)

  const nodes = useMemo(() => data?.result.nodes ?? [], [data])
  const edges = useMemo(() => data?.result.edges ?? [], [data])
  const positions = useMemo(
    () => layout(nodes, edges, { width: WIDTH, height: HEIGHT }),
    [nodes, edges],
  )
  const matched = useMemo(() => new Set(data?.result.matched ?? []), [data])
  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes])
  const focused = selected ? (byId.get(selected) ?? null) : null

  if (error) return <ErrorNote error={error} />
  if (!data && isLoading) return <p className="text-sm text-muted">Walking the source tree…</p>
  if (!data) return null
  if (data.problem) {
    return (
      <div className="rounded-lg border border-warn/40 bg-warn/5 px-4 py-3 text-sm">
        <p className="font-semibold">No graph to draw</p>
        <p className="mt-1 text-muted">{data.problem}</p>
      </div>
    )
  }

  const counts = data.counts ?? {}
  const empty = (counts.folder ?? 0) === 0

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="ledger · rule:R1 · folder:payments · kind:claim uncited:true"
          aria-label="Query the sidecar graph"
          className="min-w-64 flex-1 rounded border border-line bg-canvas px-2.5 py-1.5 font-mono text-sm"
        />
        <label className="flex items-center gap-1.5 text-sm text-muted">
          <span title="How far out from each match to follow edges. A rule is one node; one hop out is every claim that excepts it, and two is the folders those claims live in.">
            hops
          </span>
          <select
            value={hops}
            onChange={(event) => setHops(Number(event.target.value))}
            aria-label="Hops from each match"
            className="rounded border border-line bg-canvas px-1.5 py-1 text-sm"
          >
            {[0, 1, 2, 3].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <Badge tone="neutral" title="Folders under this tree that keep notes for this role.">
          {counts.folder ?? 0} folder{counts.folder === 1 ? '' : 's'}
        </Badge>
        <Badge
          tone="neutral"
          title="Bullets across every `context.md` and role file the walk found."
        >
          {counts.claim ?? 0} claim{counts.claim === 1 ? '' : 's'}
        </Badge>
        <Badge
          tone="neutral"
          title="Rules a claim narrows with `Excepts R7`. The same rule excepted in three folders is the signal that the rule itself wants rewriting, not a fourth exception."
        >
          {counts.rule ?? 0} rule{counts.rule === 1 ? '' : 's'}
        </Badge>
        <Badge
          tone="neutral"
          title="Merge requests, tickets and ADRs the claims cite, grouped by ticket. Two claims citing one review are connected here and nowhere else."
        >
          {counts.ref ?? 0} reference{counts.ref === 1 ? '' : 's'}
        </Badge>
        {(counts.missing ?? 0) > 0 && (
          <Badge
            tone="warn"
            title="A `## file.py` heading naming a file that is not there, or a `[[link]]` to a folder that moved. Drawn hollow, and failed by `whetstone sidecars check`."
          >
            {counts.missing} dangling
          </Badge>
        )}
        {(counts.uncited ?? 0) > 0 && (
          <Badge tone="warn" title="Claims with no `<!-- src: … -->`, which nothing can verify.">
            {counts.uncited} uncited
          </Badge>
        )}
        <span
          className="ml-auto font-mono text-muted"
          title="Identity of the built graph — the builder version and every folder's content hash. Two builds with the same digest read the same notes."
        >
          {data.digest.slice(0, 12)}
          {data.reused > 0 && data.parsed === 0 ? ' · cached' : ` · ${data.parsed} read`}
        </span>
      </div>

      {data.truncated && (
        <p className="text-sm text-warn">
          The walk stopped at its folder limit, so this graph is partial. Nothing is missing from
          what it drew — but a folder past the limit is not in it.
        </p>
      )}

      {empty ? (
        <p className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-muted">
          No <code className="font-mono text-xs">.agents/</code> folders under this tree yet.
          Absence is normal and there is deliberately no coverage number anywhere in this design —
          write a note where a review keeps going wrong, and this fills itself in.
        </p>
      ) : (
        <>
          <Canvas
            nodes={nodes}
            edges={edges}
            positions={positions}
            matched={matched}
            selected={selected}
            onSelect={setSelected}
          />
          <Legend />
          <EdgeLegend />
          <Results
            nodes={nodes}
            matched={data.result.matched}
            total={data.result.total_matched}
            truncated={data.result.truncated}
            query={query}
            rescued={data.result.semantic.length > 0}
            selected={selected}
            onSelect={setSelected}
          />
          <Semantic
            nodes={nodes}
            ids={data.result.semantic}
            scores={data.result.scores}
            status={data.result.semantic_status}
            asked={query.length > 0}
            selected={selected}
            onSelect={setSelected}
          />
        </>
      )}

      {focused && (
        <Detail
          node={focused}
          skillId={skillId}
          onQuery={(text) => {
            setDraft(text)
            setSelected(null)
          }}
        />
      )}
    </section>
  )
}

// The layout box. Fixed rather than measured: the SVG scales to its container through `viewBox`,
// and measuring would make the positions depend on the panel's width — so the same query would
// draw differently on two screens, which is the one property this whole layout is built to avoid.
const WIDTH = 900
const HEIGHT = 460

/** Semantic colours, reusing the palette rather than introducing a second one. */
const KIND_COLOR: Record<GraphNodeKind, string> = {
  folder: 'var(--color-accent)',
  claim: 'var(--color-ink)',
  rule: 'var(--color-warn)',
  ref: 'var(--color-muted)',
  file: 'var(--color-good)',
  unresolved: 'var(--color-bad)',
}

const KIND_HELP: Record<GraphNodeKind, string> = {
  folder: 'A directory keeping notes for this role.',
  claim: 'One bullet — an assertion about the folder it sits in.',
  rule: 'A central rule some claim narrows with `Excepts`.',
  ref: 'The review, ticket or ADR a claim came from.',
  file: 'A file a `## heading` section describes.',
  unresolved: 'A link naming nothing in this tree — renamed, or misspelt.',
}

/** Structural edges are the spine and should recede; authored ones are the point and should not. */
const EDGE_STYLE: Record<GraphEdgeKind, { opacity: number; dash?: string }> = {
  parent: { opacity: 0.45 },
  contains: { opacity: 0.3 },
  describes: { opacity: 0.5 },
  excepts: { opacity: 0.8, dash: '4 3' },
  cites: { opacity: 0.35, dash: '2 3' },
  links: { opacity: 0.9, dash: '5 3' },
  see: { opacity: 0.7, dash: '5 3' },
}

function Canvas({
  nodes,
  edges,
  positions,
  matched,
  selected,
  onSelect,
}: {
  nodes: GraphNode[]
  edges: GraphEdge[]
  positions: Map<string, { x: number; y: number }>
  matched: Set<string>
  selected: string | null
  onSelect: (id: string | null) => void
}) {
  // Labels for every node turn a 200-node graph into a wall of text, so most nodes earn one by
  // being selected. Folders always keep theirs — they are the map anyone orients by — and a small
  // result set gets them all, because a dozen unlabelled dots is a picture of nothing and the
  // whole point of narrowing a query is to be able to read the answer.
  const roomy = nodes.length <= 14
  const labelled = (node: GraphNode) => roomy || node.kind === 'folder' || node.id === selected

  // A ring means "this one matched". With no query everything matched, so a ring on every node
  // says nothing and reads as though the graph were highlighting something.
  const ringing = matched.size < nodes.length

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label="Sidecar knowledge graph"
      className="w-full rounded-lg border border-line bg-surface"
      onClick={() => onSelect(null)}
    >
      {edges.map((edge) => {
        const a = positions.get(edge.source)
        const b = positions.get(edge.target)
        if (!a || !b) return null
        const style = EDGE_STYLE[edge.kind]
        const touched = selected !== null && (edge.source === selected || edge.target === selected)
        return (
          <line
            key={`${edge.source}|${edge.target}|${edge.kind}`}
            x1={a.x}
            y1={a.y}
            x2={b.x}
            y2={b.y}
            stroke={touched ? 'var(--color-accent)' : 'var(--color-muted)'}
            strokeWidth={touched ? 1.6 : 1}
            strokeDasharray={style.dash}
            opacity={selected !== null && !touched ? 0.15 : style.opacity}
          />
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
          >
            <title>{`${node.kind}: ${node.label}`}</title>
            <circle
              r={radius}
              fill={node.missing ? 'none' : KIND_COLOR[node.kind]}
              stroke={node.missing ? KIND_COLOR.unresolved : 'var(--color-canvas)'}
              strokeWidth={node.missing ? 1.5 : 1}
              strokeDasharray={node.missing ? '3 2' : undefined}
            />
            {/* A match ring rather than a different fill: the kind is what the colour means, and a
                query must not be able to make a folder look like a rule. */}
            {isMatch && (
              <circle r={radius + 3.5} fill="none" stroke="var(--color-accent)" strokeWidth={1.4} />
            )}
            {labelled(node) && (
              <text
                y={-radius - 5}
                // Centred except near an edge, where half a centred label falls outside the SVG
                // and is clipped — which is worse than an off-centre one, because the half that
                // survives reads as the whole label.
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

/** Roughly half a long label at 10px — past this from an edge, centring clips it. */
const LABEL_REACH = 110

function anchorFor(x: number): 'start' | 'middle' | 'end' {
  if (x < LABEL_REACH) return 'start'
  if (x > WIDTH - LABEL_REACH) return 'end'
  return 'middle'
}

function Legend() {
  const kinds = Object.keys(KIND_COLOR) as GraphNodeKind[]
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      {kinds.map((kind) => (
        <li key={kind} className="flex items-center gap-1.5" title={KIND_HELP[kind]}>
          <span
            className="inline-block h-2.5 w-2.5 rounded-full"
            style={{
              backgroundColor: kind === 'unresolved' ? 'transparent' : KIND_COLOR[kind],
              border: kind === 'unresolved' ? `1.5px dashed ${KIND_COLOR.unresolved}` : undefined,
            }}
          />
          {kind}
        </li>
      ))}
      <li className="ml-auto">a ring marks a query match · a bigger circle has more edges</li>
    </ul>
  )
}

/** The legend, and what the dashes on an edge mean. */
function EdgeLegend() {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      <li title="This folder is inside that one — the ancestor walk `collect.py` performs, drawn.">
        — parent / contains
      </li>
      <li title="A claim narrowing a central rule with `Excepts R7`.">–– excepts</li>
      <li title="The review, ticket or ADR a claim came from. Two folders citing one ADR are connected here and nowhere else.">
        ·· cites
      </li>
      <li title="An authored `[[link]]` in a claim, or a `see:` in the frontmatter. The only edges a human writes.">
        – – links / see
      </li>
    </ul>
  )
}

function Results({
  nodes,
  matched,
  total,
  truncated,
  query,
  rescued,
  selected,
  onSelect,
}: {
  nodes: GraphNode[]
  matched: string[]
  total: number
  truncated: boolean
  query: string
  rescued: boolean
  selected: string | null
  onSelect: (id: string) => void
}) {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const rows = matched.map((id) => byId.get(id)).filter((node): node is GraphNode => !!node)

  if (rows.length === 0) {
    // The field crib sheet is help for a dead end, and it stops being help the moment the meaning
    // search answered underneath it — a wall of syntax above two good rows reads as a failure the
    // reader then has to notice was not one.
    if (rescued) {
      return (
        <p className="text-sm text-muted">
          No claim contains <code className="font-mono text-xs">{query}</code>.
        </p>
      )
    }
    return (
      <p className="text-sm text-muted">
        Nothing matches <code className="font-mono text-xs">{query}</code>. Fields are{' '}
        <code className="font-mono text-xs">kind:</code>{' '}
        <code className="font-mono text-xs">folder:</code>{' '}
        <code className="font-mono text-xs">rule:</code>{' '}
        <code className="font-mono text-xs">ref:</code>{' '}
        <code className="font-mono text-xs">file:</code>{' '}
        <code className="font-mono text-xs">status:</code>{' '}
        <code className="font-mono text-xs">excepts:</code>{' '}
        <code className="font-mono text-xs">uncited:</code>; anything else is a substring.
      </p>
    )
  }

  return (
    <div>
      <p className="mb-1.5 text-sm text-muted">
        {total} match{total === 1 ? '' : 'es'}
        {query ? (
          <>
            {' '}
            for <code className="font-mono text-xs">{query}</code>
          </>
        ) : (
          ' — everything in this tree'
        )}
        {truncated && <span className="text-warn"> · more than the limit; narrow the query</span>}
      </p>
      <ul className="max-h-72 space-y-1 overflow-y-auto pr-1">
        {rows.map((node) => (
          <li key={node.id}>
            <button
              type="button"
              onClick={() => onSelect(node.id)}
              className={`w-full rounded border px-2.5 py-1.5 text-left text-sm ${
                node.id === selected ? 'border-accent bg-accent/5' : 'border-line/60'
              }`}
            >
              <span className="flex flex-wrap items-baseline gap-x-2">
                <span
                  className="font-mono text-xs"
                  style={{
                    color:
                      KIND_COLOR[node.kind] === 'var(--color-ink)'
                        ? undefined
                        : KIND_COLOR[node.kind],
                  }}
                >
                  {node.kind}
                </span>
                <span className="flex-1">{node.label}</span>
                {node.sidecar && (
                  <code className="font-mono text-xs text-muted">
                    {node.sidecar}:{node.line}
                  </code>
                )}
                {node.missing && <span className="text-xs text-bad">not in the tree</span>}
                {node.kind === 'claim' && !node.cited && (
                  <span className="text-xs text-warn">uncited</span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * Claims that mean something close to the query without containing any of it.
 *
 * A separate block, under the exact matches and labelled, because the two have different
 * warranties: a lexical hit contains what you typed, and this is a model's opinion about a
 * sentence. Merging them into one ranked list would spend the exact half's credibility on the
 * approximate half — so the score is on every row, and a reader who does not trust it can stop
 * reading at the heading.
 *
 * Silent when nothing was asked. Loud when something was asked and the embedder could not answer:
 * "no semantic results" and "no embedding model configured" are different facts about this tree,
 * and only one of them is about the notes.
 */
function Semantic({
  nodes,
  ids,
  scores,
  status,
  asked,
  selected,
  onSelect,
}: {
  nodes: GraphNode[]
  ids: string[]
  scores: Record<string, number>
  status: string
  asked: boolean
  selected: string | null
  onSelect: (id: string) => void
}) {
  if (!asked) return null
  if (status) {
    return (
      <p className="text-sm text-muted">
        <span className="text-warn">Meaning search off.</span> {status}
      </p>
    )
  }
  if (ids.length === 0) return null

  const byId = new Map(nodes.map((node) => [node.id, node]))
  const rows = ids.map((id) => byId.get(id)).filter((node): node is GraphNode => !!node)
  return (
    <div>
      <p className="mb-1.5 text-sm text-muted">
        Also close in meaning — {rows.length} claim{rows.length === 1 ? '' : 's'} that contain none
        of what you typed.{' '}
        <span title="Cosine similarity against a local embedding model. Additive only: it can add rows below the exact matches and can never reorder or hide one.">
          Scored, not matched.
        </span>
      </p>
      <ul className="space-y-1">
        {rows.map((node) => (
          <li key={node.id}>
            <button
              type="button"
              onClick={() => onSelect(node.id)}
              className={`w-full rounded border border-dashed px-2.5 py-1.5 text-left text-sm ${
                node.id === selected ? 'border-accent bg-accent/5' : 'border-line'
              }`}
            >
              <span className="flex flex-wrap items-baseline gap-x-2">
                <span className="tabular font-mono text-xs text-muted">
                  {(scores[node.id] ?? 0).toFixed(2)}
                </span>
                <span className="flex-1">{node.label}</span>
                {node.sidecar && (
                  <code className="font-mono text-xs text-muted">
                    {node.sidecar}:{node.line}
                  </code>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * One selected node, in full — and the queries that node makes worth asking.
 *
 * The buttons are the investigation half: a rule is only interesting as *the folders that except
 * it*, and a reference as *everything that came out of that review*. Both are one edge away and
 * neither is a question a text box invites you to type.
 */
function Detail({
  node,
  skillId,
  onQuery,
}: {
  node: GraphNode
  skillId: string
  onQuery: (query: string) => void
}) {
  return (
    <div className="rounded-lg border border-accent/40 bg-accent/5 px-4 py-3 text-sm">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-xs" style={{ color: KIND_COLOR[node.kind] }}>
          {node.kind}
        </span>
        <span className="font-semibold">{node.label}</span>
        {node.status && (
          <span
            className={node.status === 'unconfirmed' ? 'text-warn text-xs' : 'text-muted text-xs'}
            title={
              node.status === 'unconfirmed'
                ? 'Never injected into a consuming run — agent-authored or bootstrap-decomposed, and nothing independent has agreed with it yet.'
                : 'On a rung retrieval will inject.'
            }
          >
            {node.status}
          </span>
        )}
        {node.excepts && <span className="text-xs text-warn">excepts {node.excepts}</span>}
      </div>
      {node.text && <p className="mt-2">{node.text}</p>}
      {node.sidecar && (
        <p className="mt-2 font-mono text-xs text-muted">
          {node.sidecar}:{node.line}
          {node.section && ` · ## ${node.section}`}
        </p>
      )}
      {node.missing && (
        <p className="mt-2 text-bad">
          Nothing in the source tree has this path. The link or heading outlived what it named —{' '}
          <code className="font-mono text-xs">whetstone sidecars check</code> fails it.
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        {node.kind === 'rule' && (
          <Ask onQuery={onQuery} query={`excepts:${node.label}`}>
            every claim that excepts {node.label}
          </Ask>
        )}
        {node.kind === 'ref' && (
          <Ask onQuery={onQuery} query={node.label}>
            everything citing {node.label}
          </Ask>
        )}
        {(node.kind === 'folder' || node.kind === 'file') && node.path && (
          <Ask onQuery={onQuery} query={`folder:${node.path}`}>
            everything under {node.path}
          </Ask>
        )}
        {node.kind === 'claim' && node.path && (
          <Ask onQuery={onQuery} query={`folder:${node.path}`}>
            the rest of {node.path}
          </Ask>
        )}
      </div>
      {node.sidecar && <FilePanel skillId={skillId} path={node.sidecar} line={node.line} />}
    </div>
  )
}

/**
 * The whole `.agents/` file behind a claim, with that claim's line marked.
 *
 * The claim's text is already above this, so what it adds is everything *around* it: the rung the
 * file sits on, the tree it was last confirmed against, the orientation prose the format does not
 * parse as a claim, and the other bullets in the same folder. A claim shown alone reads as the
 * folder's only note, and the commonest question after "what does this say" is "what else does
 * this folder say".
 *
 * Collapsed by default and fetched on open — it is a filesystem read of somebody's repository, and
 * it should happen because a person asked, not because they selected a node.
 */
function FilePanel({ skillId, path, line }: { skillId: string; path: string; line: number }) {
  const [open, setOpen] = useState(false)
  const { data, isLoading, error } = useSidecarFile(skillId, open ? path : null)

  return (
    <details
      className="mt-3 border-t border-accent/20 pt-2"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer text-sm text-muted">
        Open <code className="font-mono text-xs">{path}</code>
      </summary>
      {error && <ErrorNote error={error} />}
      {isLoading && <p className="mt-2 text-sm text-muted">Reading the file…</p>}
      {data?.problem && <p className="mt-2 text-sm text-bad">{data.problem}</p>}
      {data && !data.problem && <FileBody file={data} line={line} />}
    </details>
  )
}

function FileBody({ file, line }: { file: SidecarFile; line: number }) {
  const claims = new Set(file.claim_lines)
  const lines = file.text.replace(/\n$/, '').split('\n')
  return (
    <div className="mt-2">
      <ul className="mb-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-muted">
        <li
          className={file.status === 'unconfirmed' ? 'text-warn' : undefined}
          title="The rung on the trust ladder. `unconfirmed` is never injected into a consuming run."
        >
          status {file.status || '(unstated)'}
        </li>
        {file.confirmed_at_tree && (
          <li title="`git rev-parse HEAD:<folder>` when these claims were last verified. Git is a Merkle tree, so comparing it against the folder's current tree object is free and exact — it scopes the maintainer sweep, and does not certify freshness.">
            confirmed_at_tree {file.confirmed_at_tree}
          </li>
        )}
        {file.confirmed_by && (
          <li title="What the rung rests on — the run or eval case whose evidence promoted it.">
            confirmed_by {file.confirmed_by}
          </li>
        )}
        <li className="ml-auto">{file.bytes.toLocaleString()} bytes</li>
      </ul>
      {/* Plain text, not rendered markdown. The file *is* the artefact — a reader here is checking
          what a reviewer was handed and what a `git blame` would show, and rendering it would hide
          the citation comments, which are the part every claim is required to carry. */}
      <pre className="max-h-96 overflow-auto rounded border border-line bg-canvas p-2 font-mono text-xs">
        {lines.map((text, index) => {
          const number = index + 1
          const selected = number === line
          return (
            <div
              key={number}
              className={
                selected
                  ? 'bg-accent/15'
                  : claims.has(number)
                    ? 'border-l-2 border-accent/40 -ml-0.5 pl-0.5'
                    : undefined
              }
            >
              <span className="mr-3 inline-block w-6 text-right text-muted select-none">
                {number}
              </span>
              {text || ' '}
            </div>
          )
        })}
      </pre>
    </div>
  )
}

function Ask({
  onQuery,
  query,
  children,
}: {
  onQuery: (query: string) => void
  query: string
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={() => onQuery(query)}
      className="rounded border border-line bg-canvas px-2 py-1 text-xs hover:border-accent"
    >
      {children}
    </button>
  )
}
