import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  useSidecarFile,
  useSidecarGraph,
  type GraphEdge,
  type GraphEdgeKind,
  type GraphNode,
  type GraphNodeKind,
  type SidecarFile,
} from '@/api/client'
import { Canvas, tooManyToRead } from '@/components/graph/Canvas'
import { EdgeLegend, Legend } from '@/components/graph/Legend'
import { Neighbours } from '@/components/graph/Neighbours'
import type { GraphPalette, Ring } from '@/components/graph/types'
import { boxFor, layout } from '@/components/graphLayout'
import { MeaningCoverage } from '@/components/MeaningCoverage'
import {
  clampHops,
  crumbsFor,
  focusQuery,
  parentOf,
  parentQuery,
  type GraphParams,
} from '@/components/graphNav'
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
  // Navigation lives in the URL, not in component state. Three things fall out of that and none of
  // them are available otherwise: browser back/forward becomes undo for an exploration (three hops
  // deep, one keystroke home), a view can be pasted to someone, and it survives a reload. The tab
  // was already a search param, so a graph position that was not read as an inconsistency.
  const [params, setParams] = useSearchParams()
  const query = params.get('q') ?? ''
  const hops = clampHops(params.get('hops'))
  const selected = params.get('node')

  const [draft, setDraft] = useState(query)

  // Debounced, because every keystroke is a filesystem walk on somebody's monorepo — cached, but
  // still a walk. 250ms is under the threshold where a person notices waiting and well over the
  // gap between two keys in a word.
  //
  // `replace` while typing: a history entry per keystroke would make back useless for the thing it
  // is actually wanted for, which is undoing a *navigation* step.
  useEffect(() => {
    const timer = setTimeout(() => {
      if (draft.trim() !== query) navigate({ q: draft.trim(), node: null }, { replace: true })
    }, 250)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  // A query typed elsewhere — a breadcrumb, a Focus button, the browser's back button — has to
  // reach the box, or the input and the picture disagree about what is being asked.
  useEffect(() => setDraft(query), [query])

  function navigate(next: Partial<GraphParams>, options?: { replace?: boolean }) {
    const merged = new URLSearchParams(params)
    for (const [key, value] of Object.entries(next)) {
      if (value === null || value === '') merged.delete(key)
      else merged.set(key, String(value))
    }
    setParams(merged, options)
  }

  const { data, isLoading, error } = useSidecarGraph(skillId, query, hops, true)

  const nodes = useMemo(() => data?.result.nodes ?? [], [data])
  const edges = useMemo(() => data?.result.edges ?? [], [data])
  // Sized to the result rather than fixed — see `boxFor`. Same reasoning as the guidance graph's:
  // a node's radius is in layout units, so a bigger box is the only thing that gives more dots room.
  const box = useMemo(() => boxFor(nodes.length), [nodes.length])
  const positions = useMemo(() => layout(nodes, edges, box), [nodes, edges, box])
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
        <div className="relative flex min-w-64 flex-1 items-center">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') setDraft('')
            }}
            placeholder="ledger · rule:R1 · folder:payments · kind:claim · issue:true"
            aria-label="Query the sidecar graph"
            className="w-full rounded border border-line bg-canvas py-1.5 pr-8 pl-2.5 font-mono text-sm"
          />
          {draft && (
            <button
              type="button"
              onClick={() => setDraft('')}
              aria-label="Clear the query"
              title="Clear (Esc)"
              className="absolute right-2 text-muted hover:text-ink"
            >
              ×
            </button>
          )}
        </div>
        <label className="flex items-center gap-1.5 text-sm text-muted">
          <span title="How far out from each match to follow edges. A rule is one node; one hop out is every claim that excepts it, and two is the folders those claims live in. It is a radius, not a step — it reaches parents, children and siblings alike.">
            hops
          </span>
          <select
            value={hops}
            onChange={(event) => navigate({ hops: Number(event.target.value) })}
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
        {(counts.disputed ?? 0) > 0 && (
          <Badge
            tone="bad"
            title="Something with the code in front of it said these claims no longer hold. They are still injected into every review that touches their folder — correction is a human's call, never automatic."
          >
            {counts.disputed} disputed
          </Badge>
        )}
        {/* Clickable, unlike the counts beside it. Above roughly 60 nodes "which of these is
            broken" cannot be answered by looking, so a number with no way to act on it is worse
            than no number — this sets the query that shows exactly them. */}
        {(counts.problems ?? 0) > 0 && (
          <button
            type="button"
            onClick={() => navigate({ q: 'issue:true', node: null })}
            title="Mechanical defects `whetstone sidecars check` fails on — an oversized file retrieval silently drops, notes left behind by a rename, a claim nothing can verify. Click to show only these."
            className="rounded-full border border-bad/50 px-2 py-px text-xs whitespace-nowrap text-bad hover:bg-bad/10"
          >
            {counts.problems} with defects
          </button>
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

      <Breadcrumb query={query} focused={focused} onGo={(q) => navigate({ q, node: null })} />

      {empty ? (
        <p className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-muted">
          No <code className="font-mono text-xs">.agents/</code> folders under this tree yet.
          Absence is normal and there is deliberately no coverage number anywhere in this design —
          write a note where a review keeps going wrong, and this fills itself in.
        </p>
      ) : (
        <>
          {/* Same restraint as the guidance graph: a picture this size is a texture, and saying so
              points at the fix instead of leaving a reader to conclude the graph does not work. */}
          {tooManyToRead(nodes.length, edges.length) && (
            <p className="text-xs text-warn">
              {nodes.length} nodes and {edges.length} edges is more texture than picture — the list
              below is exact. Narrow with a field (<code className="font-mono">folder:…</code>,{' '}
              <code className="font-mono">kind:claim</code>) so the connections have room to show.
            </p>
          )}
          <Canvas
            nodes={nodes}
            edges={edges}
            positions={positions}
            box={box}
            matched={matched}
            selected={selected}
            palette={PALETTE}
            rings={ringsFor}
            flag={(node) => node.issues.length > 0}
            nodeTitle={nodeTitle}
            edgeTitle={edgeTitle}
            ariaLabel="Sidecar knowledge graph"
            onSelect={(id) => navigate({ node: id }, { replace: true })}
            onFocus={(node) => navigate({ q: focusQuery(node), node: node.id })}
          />
          {/* Directly under the picture, for the reason the guidance graph's card is: below the
              result list it was a hundred rows away, so clicking a dot changed nothing a reader
              could see and the graph read as one where clicking does nothing. */}
          {focused && (
            <Detail
              node={focused}
              skillId={skillId}
              edges={edges}
              labels={(id) => byId.get(id)?.label ?? id}
              kinds={(id) => byId.get(id)?.kind ?? 'folder'}
              onQuery={(text) => navigate({ q: text, node: null })}
              onSelect={(id) => navigate({ node: id }, { replace: true })}
              onFocus={() => navigate({ q: focusQuery(focused), node: focused.id })}
            />
          )}
          <Legend palette={PALETTE} marks={MARKS} note={LEGEND_NOTE} />
          <EdgeLegend palette={PALETTE} groups={EDGE_GROUPS} />
          <Results
            nodes={nodes}
            matched={data.result.matched}
            total={data.result.total_matched}
            truncated={data.result.truncated}
            query={query}
            rescued={data.result.semantic.length > 0}
            selected={selected}
            onSelect={(id) => navigate({ node: id }, { replace: true })}
          />
          <Semantic
            nodes={nodes}
            ids={data.result.semantic}
            scores={data.result.scores}
            status={data.result.semantic_status}
            searched={data.result.semantic_searched}
            total={data.result.semantic_total}
            skillId={skillId}
            asked={query.length > 0}
            selected={selected}
            onSelect={(id) => navigate({ node: id }, { replace: true })}
          />
        </>
      )}
    </section>
  )
}

/**
 * Where you are in the tree, and the one control that goes both ways.
 *
 * Going *in* had a button (`everything under payments`) and going *out* had nothing — you edited
 * the path in the query box by hand, which is not navigation, it is typing. `hops` is no answer
 * either: it is a radius, so it reaches the parent, the children and the siblings at once.
 *
 * The path is read back out of the query rather than tracked separately, so it also answers the
 * question nothing on this screen answered before — *where am I* — including after a reload or
 * when someone pastes you a link.
 */
function Breadcrumb({
  query,
  focused,
  onGo,
}: {
  query: string
  focused: GraphNode | null
  onGo: (query: string) => void
}) {
  const segments = crumbsFor(query, focused?.path || null)
  if (segments === null) return null

  return (
    <nav aria-label="Folder path" className="flex flex-wrap items-center gap-1 text-sm">
      <Crumb onGo={onGo} to="" active={segments.length === 0}>
        whole tree
      </Crumb>
      {segments.map((segment: string, index: number) => (
        <span key={index} className="flex items-center gap-1">
          <span className="text-muted">/</span>
          <Crumb
            onGo={onGo}
            to={segments.slice(0, index + 1).join('/')}
            active={index === segments.length - 1}
          >
            {segment}
          </Crumb>
        </span>
      ))}
    </nav>
  )
}

function Crumb({
  onGo,
  to,
  active,
  children,
}: {
  onGo: (query: string) => void
  to: string
  active: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={() => onGo(to ? `folder:${to}` : '')}
      title={to ? `Everything under ${to}` : 'The whole tree'}
      className={`rounded px-1.5 py-0.5 font-mono text-xs ${
        active ? 'bg-accent/10 text-accent' : 'text-muted hover:text-ink'
      }`}
    >
      {children}
    </button>
  )
}

/** Semantic colours, reusing the app palette rather than introducing a second one. */
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

/** What each edge kind asserts, for the hover on one specific line. */
const EDGE_TITLE: Record<GraphEdgeKind, string> = {
  parent: 'is inside — the ancestor walk retrieval performs',
  contains: 'keeps this claim',
  describes: 'this claim is about that file',
  excepts: 'this claim narrows that central rule',
  cites: 'this claim came out of that review, ticket or ADR',
  links: 'a [[link]] written inside the claim',
  see: 'a `see:` link in the folder’s frontmatter',
}

/** The same seven edges as sentences, read from each end — see `GraphPalette.edgeRelation`. */
const EDGE_RELATION: Record<GraphEdgeKind, { out: string; in: string }> = {
  parent: { out: 'is inside', in: 'holds' },
  contains: { out: 'keeps', in: 'kept in' },
  describes: { out: 'is about', in: 'described by' },
  excepts: { out: 'narrows', in: 'narrowed by' },
  cites: { out: 'came out of', in: 'produced' },
  links: { out: 'links to', in: 'linked from' },
  see: { out: 'see', in: 'seen from' },
}

/** Everything `graph/Canvas` needs to know about this graph's vocabulary. */
const PALETTE: GraphPalette = {
  colour: KIND_COLOR,
  help: KIND_HELP,
  hollow: 'unresolved',
  // Folders are the map anyone orients by, so they keep their label at any node count.
  anchors: ['folder'],
  edge: EDGE_STYLE,
  edgeHelp: EDGE_TITLE,
  edgeRelation: EDGE_RELATION,
}

/**
 * The health rings for one node, outermost last.
 *
 * The two facts worth seeing without asking are the two that change what a review is given: a claim
 * the code has contradicted is still injected into every review touching its folder, and an
 * `unconfirmed` folder is injected into none of them. Both drew identically to a healthy node, which
 * made the map silent about the only thing it could warn of.
 */
function ringsFor(node: GraphNode): Ring[] {
  const out: Ring[] = []
  if (node.contradicted > 0) out.push({ colour: KIND_COLOR.unresolved, width: 1.6 })
  if (node.status === 'unconfirmed') {
    out.push({ colour: 'var(--color-warn)', width: 1.4, dash: '2 2' })
  }
  return out
}

const MARKS = [
  {
    label: 'has a defect',
    help: 'A mechanical defect `whetstone sidecars check` fails on — uncited, oversized, notes left behind by a rename. On a folder, something inside it.',
    swatch: 'dot' as const,
    colour: 'var(--color-bad)',
  },
  {
    label: 'contradicted',
    help: 'Something with the code in front of it found this claim no longer holds. Still injected into every review that touches its folder — correction is a human’s call.',
    swatch: 'ring' as const,
    colour: KIND_COLOR.unresolved,
  },
  {
    label: 'unconfirmed',
    help: 'On a rung retrieval withholds: agent-authored or bootstrap-decomposed, and nothing independent has agreed with it yet.',
    swatch: 'dashed-ring' as const,
    colour: 'var(--color-warn)',
  },
]

const LEGEND_NOTE = 'a ring marks a query match · a bigger circle has more edges'

const EDGE_GROUPS = [
  {
    kinds: ['parent', 'contains'],
    label: 'parent / contains',
    help: 'This folder is inside that one — the ancestor walk `collect.py` performs, drawn.',
  },
  {
    kinds: ['excepts'],
    label: 'excepts',
    help: 'A claim narrowing a central rule with `Excepts R7`.',
  },
  {
    kinds: ['cites'],
    label: 'cites',
    help: 'The review, ticket or ADR a claim came from. Two folders citing one ADR are connected here and nowhere else.',
  },
  {
    kinds: ['links', 'see'],
    label: 'links / see',
    help: 'An authored `[[link]]` in a claim, or a `see:` in the frontmatter. The only edges a human writes.',
  },
]

/**
 * Everything about a node that a hover can answer, so the picture is readable without clicking.
 *
 * The old title was `kind: label`, which repeated what the colour and the text already said. What
 * a reader wants from a dot is the four things that are otherwise invisible: how connected it is,
 * where it lives, whether retrieval will inject it, and whether anything has argued with it.
 */
function nodeTitle(node: GraphNode): string {
  const lines = [`${node.kind}: ${node.label}`]
  if (node.path && node.path !== node.label) lines.push(node.path)
  if (node.sidecar) lines.push(`${node.sidecar}:${node.line}`)
  if (node.kind === 'folder' && node.claims) lines.push(`${node.claims} claim(s) in this folder`)
  if (node.status === 'unconfirmed') {
    lines.push('unconfirmed — withheld from every review until something independent agrees')
  }
  if (node.kind === 'claim' && !node.cited) lines.push('uncited — nothing can verify this')
  if (node.contradicted > 0) {
    lines.push(
      `${node.contradicted} run(s) found code disagreeing with ` +
        (node.kind === 'folder' ? 'claims here' : 'this'),
    )
  }
  if (node.confirmed > 0) lines.push(`${node.confirmed} cited it as still holding`)
  if (node.missing) lines.push('not in the source tree — renamed, or misspelt')
  // The reason, not just the code. `oversized` on its own sends someone to look up what the cap is
  // and what happens when it is passed; the floor already wrote that sentence for CI.
  for (const message of node.issue_messages) lines.push(message)
  // Codes with no message of their own here — a claim's defect rolled up to its folder.
  const explained = node.issue_messages.length
  if (node.issues.length && !explained) lines.push(`inside: ${node.issues.join(', ')}`)
  lines.push(`${node.degree} edge(s) · double-click to centre the graph here`)
  return lines.join('\n')
}

function edgeTitle(edge: GraphEdge): string {
  const detail = edge.detail ? `\n${edge.detail}` : ''
  return `${edge.kind} — ${EDGE_TITLE[edge.kind]}${detail}`
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
        <code className="font-mono text-xs">uncited:</code>{' '}
        <code className="font-mono text-xs">issue:</code>; anything else is a substring.
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
                {node.status === 'unconfirmed' && (
                  <span className="text-xs text-warn" title="Withheld from every review.">
                    unconfirmed
                  </span>
                )}
                {node.contradicted > 0 && (
                  <span
                    className="text-xs text-bad"
                    title="Runs that found code disagreeing with this claim."
                  >
                    {node.contradicted} contradicted
                  </span>
                )}
                {/* `uncited` is already its own chip above, so listing it again here would double
                    it on the one defect that is also the commonest. */}
                {node.issues
                  .filter((code) => code !== 'uncited')
                  .map((code) => (
                    <span key={code} className="font-mono text-xs text-bad">
                      {code}
                    </span>
                  ))}
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
 *
 * A third fact, which used to be told as if it were the second: a tree whose claims have not all
 * been embedded yet. That is coverage, not failure — `MeaningCoverage` renders it as an offer to
 * finish, and the rows found so far stay on screen rather than being dropped behind a warning.
 */
function Semantic({
  nodes,
  ids,
  scores,
  status,
  searched,
  total,
  skillId,
  asked,
  selected,
  onSelect,
}: {
  nodes: GraphNode[]
  ids: string[]
  scores: Record<string, number>
  status: string
  searched: number
  total: number
  skillId: string
  asked: boolean
  selected: string | null
  onSelect: (id: string) => void
}) {
  if (!asked) return null

  const coverage = (
    <MeaningCoverage
      skillId={skillId}
      scope="sidecars"
      status={status}
      searched={searched}
      total={total}
      unit="claim"
    />
  )
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const rows = ids.map((id) => byId.get(id)).filter((node): node is GraphNode => !!node)
  if (rows.length === 0) return coverage
  return (
    <div className="space-y-2">
      <div>
        <p className="mb-1.5 text-sm text-muted">
          Also close in meaning — {rows.length} claim{rows.length === 1 ? '' : 's'} that contain
          none of what you typed.{' '}
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
      {coverage}
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
  edges,
  labels,
  kinds,
  onQuery,
  onSelect,
  onFocus,
}: {
  node: GraphNode
  skillId: string
  edges: GraphEdge[]
  labels: (id: string) => string
  kinds: (id: string) => string
  onQuery: (query: string) => void
  onSelect: (id: string) => void
  onFocus: () => void
}) {
  return (
    <div className="rounded-lg border border-accent/40 bg-accent/5 px-4 py-3 text-sm">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-xs" style={{ color: KIND_COLOR[node.kind] }}>
          {node.kind}
        </span>
        <span className="font-semibold">{node.label}</span>
        <button
          type="button"
          onClick={onFocus}
          title="Redraw the graph around this node. Double-clicking it in the picture does the same."
          className="rounded border border-line bg-canvas px-2 py-0.5 text-xs hover:border-accent"
        >
          Centre here
        </button>
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
      {/* The floor's findings, in full. Decidable, already computed, and until now delivered only
          to whoever had wired up a pre-commit hook — while this card, the one place someone is
          looking at a single note, said nothing about it being broken. */}
      {node.issues.length > 0 && (
        <div className="mt-2 rounded border border-bad/40 bg-bad/5 px-3 py-2">
          <p className="flex flex-wrap items-baseline gap-x-2 text-xs">
            <span className="font-semibold text-bad">
              {node.issue_messages.length > 0 ? 'Broken' : 'Something inside is broken'}
            </span>
            {node.issues.map((code) => (
              <button
                key={code}
                type="button"
                onClick={() => onQuery(`issue:${code}`)}
                title={`Show every node with this defect`}
                className="font-mono text-bad underline decoration-dotted"
              >
                {code}
              </button>
            ))}
          </p>
          {node.issue_messages.map((message) => (
            <p key={message} className="mt-1 text-xs text-muted">
              {message}
            </p>
          ))}
          <p className="mt-1 text-xs text-muted">
            Mechanical, not a judgement about whether the claim is true —{' '}
            <code className="font-mono">whetstone sidecars check</code> fails on exactly these.
          </p>
        </div>
      )}
      {/* The ledger, on the map. This is the maintenance loop's whole output, and it lived in a
          collapsed list further up the same tab while the picture beside it drew a contradicted
          claim exactly like a healthy one. */}
      {(node.contradicted > 0 || node.confirmed > 0) && (
        <p className="mt-2 flex flex-wrap items-baseline gap-x-3 text-xs">
          {node.contradicted > 0 && (
            <span className="text-bad">
              {node.contradicted} run{node.contradicted === 1 ? '' : 's'} found code disagreeing
              {node.kind === 'folder' ? ' with claims here' : ''}
            </span>
          )}
          {node.confirmed > 0 && (
            <span className="text-good">{node.confirmed} cited it as still holding</span>
          )}
          <span className="text-muted">
            Still injected either way — correction is a human editing the note in its own repo.
          </span>
        </p>
      )}
      {node.evidence && <p className="mt-1 text-sm text-bad italic">— {node.evidence}</p>}
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
        {/* Out, not just in. Every other button on this card goes deeper; without this one the
            only way back up the tree was editing the path in the query box by hand. */}
        {parentOf(node.path) !== null && (
          <Ask onQuery={onQuery} query={parentQuery(node.path)}>
            ↑ up to {parentOf(node.path) || 'the whole tree'}
          </Ask>
        )}
      </div>
      <Neighbours
        id={node.id}
        labels={labels}
        kinds={kinds}
        edges={edges}
        palette={PALETTE}
        onSelect={onSelect}
      />
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
