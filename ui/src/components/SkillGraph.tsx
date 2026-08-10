import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  useSkillShape,
  type ShapeEdge,
  type ShapeEdgeKind,
  type ShapeNode,
  type ShapeNodeKind,
} from '@/api/client'
import { Canvas, HEIGHT, tooManyToRead, WIDTH } from '@/components/graph/Canvas'
import { EdgeLegend, Legend } from '@/components/graph/Legend'
import type { GraphPalette, Ring } from '@/components/graph/types'
import { layout } from '@/components/graphLayout'
import { clampHops } from '@/components/graphNav'
import { Badge, ErrorNote } from '@/components/primitives'

/**
 * The shape of a skill's own guidance — which file holds which rule, and what that rule is attached
 * to.
 *
 * The prose below this answers *what are the rules*. This answers the question anyone about to edit
 * them has and nothing else could: which rule narrows another one three files away, which rule no
 * case in the corpus is linked to, which page the reviewer never actually receives, which link
 * outlived the file it named. All four are in the folder already and were visible nowhere.
 *
 * **Read-only and off the scoring path**, like the Sidecar tab's graph: `skill_hash` covers the same
 * bytes it did before and no prompt changes because a picture exists.
 */
export function SkillGraph({ skillId }: { skillId: string }) {
  // Navigation lives in the URL for the reasons the sidecar graph's does — back becomes undo for an
  // exploration, a view can be pasted to someone, and it survives a reload.
  //
  // **Namespaced params, and this is not cosmetic.** `SidecarGraph` owns `q`, `hops` and `node`, and
  // `SkillDetail.withTab` deliberately preserves every other param across a tab change so the
  // Improve workspace does not lose its state. Sharing the names would therefore carry a sidecar
  // query — `folder:payments`, which means nothing here — into this graph the moment somebody
  // switched tabs, and the picture would come back empty for no reason a reader could guess.
  const [params, setParams] = useSearchParams()
  const query = params.get('gq') ?? ''
  const hops = clampHops(params.get('ghops'))
  const selected = params.get('gnode')

  const [draft, setDraft] = useState(query)

  // Debounced, and `replace` while typing: a history entry per keystroke would make back useless for
  // the thing it is actually wanted for, which is undoing a navigation step.
  useEffect(() => {
    const timer = setTimeout(() => {
      if (draft.trim() !== query) navigate({ gq: draft.trim(), gnode: null }, { replace: true })
    }, 200)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  // A query typed elsewhere — a Focus button, the browser's back button — has to reach the box, or
  // the input and the picture disagree about what is being asked.
  useEffect(() => setDraft(query), [query])

  function navigate(
    next: Partial<Record<'gq' | 'ghops' | 'gnode', string | number | null>>,
    options?: { replace?: boolean },
  ) {
    const merged = new URLSearchParams(params)
    for (const [key, value] of Object.entries(next)) {
      if (value === null || value === '') merged.delete(key)
      else merged.set(key, String(value))
    }
    setParams(merged, options)
  }

  // Unconditionally enabled, because the *mount* is the gate: `ShapeDisclosure` renders this only
  // while its panel is open. Gating here as well would be two switches for one decision.
  const { data, isLoading, error } = useSkillShape(skillId, query, hops, true)

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
  if (!data && isLoading) return <p className="text-sm text-muted">Reading the folder…</p>
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
            placeholder="unwrap · rule:R1 · kind:directive · file:patterns · issue:true"
            aria-label="Query the guidance graph"
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
          <span title="How far out from each match to follow edges. A rule is one node; one hop out is the file it lives in, the review it came from and the cases that test it, and two is the other rules that mention it. It is a radius, not a step.">
            hops
          </span>
          <select
            value={hops}
            onChange={(event) => navigate({ ghops: Number(event.target.value) })}
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
        <Badge
          tone="neutral"
          title="Files of guidance: SKILL.md, its companion pages, and its wiki."
        >
          {counts.file ?? 0} file{counts.file === 1 ? '' : 's'}
        </Badge>
        <Badge
          tone="neutral"
          title="Rules with an id — `- **R7 — …**`. The only instructions provenance can name, a case can be linked to, and a removal warning can see."
        >
          {counts.rule ?? 0} rule{counts.rule === 1 ? '' : 's'}
        </Badge>
        {(counts.directive ?? 0) > 0 && (
          <button
            type="button"
            onClick={() => navigate({ gq: 'kind:directive', gnode: null })}
            title="Instructions with no rule id. They reach the model like any other, but nothing can trace, test or protect them. Click to show only these."
            className="rounded-full border border-warn/50 px-2 py-px text-xs whitespace-nowrap text-warn hover:bg-warn/10"
          >
            {counts.directive} unnumbered
          </button>
        )}
        <Badge
          tone="neutral"
          title="Merge requests and ADRs the rules cite, grouped by review. Two rules from one discussion are connected here and nowhere else."
        >
          {counts.ref ?? 0} review{counts.ref === 1 ? '' : 's'}
        </Badge>
        <Badge
          tone="neutral"
          title="Eval cases linked to a rule through the review both came from."
        >
          {counts.case ?? 0} linked case{counts.case === 1 ? '' : 's'}
        </Badge>
        {(counts.missing ?? 0) > 0 && (
          <Badge
            tone="warn"
            title="A link naming no page in this skill — renamed, misspelt, or pointing at a file the loader does not serve as guidance."
          >
            {counts.missing} dangling
          </Badge>
        )}
        {(counts.defects ?? 0) > 0 && (
          <button
            type="button"
            onClick={() => navigate({ gq: 'issue:true', gnode: null })}
            title="Everything mechanically wrong with this folder — a page the reviewer never gets, a rule nothing tests, a link that outlived its file. Click to show only these."
            className="rounded-full border border-bad/50 px-2 py-px text-xs whitespace-nowrap text-bad hover:bg-bad/10"
          >
            {counts.defects} with defects
          </button>
        )}
        <span
          className="ml-auto font-mono text-muted"
          title="Identity of the built graph — the builder version, the runtime, and every guidance file's content hash. Two builds with the same digest read the same folder the same way."
        >
          {MODE_LABEL[data.mode]} · {data.digest.slice(0, 12)}
        </span>
      </div>

      {/* Said rather than drawn silently. A hundred-and-fifty-node picture is a texture, and
          presenting one as an answer teaches a reader that the graph is useless — when in fact the
          answer is one term away, and the list under the picture is exact at any size. */}
      {tooManyToRead(nodes.length) && (
        <p className="text-xs text-warn">
          {nodes.length} nodes is more than a picture can show — the list below is exact. Narrow
          with a field (<code className="font-mono">kind:rule</code>,{' '}
          <code className="font-mono">file:…</code>) or drop hops to 0 to read the shape.
        </p>
      )}
      <Canvas
        nodes={nodes}
        edges={edges}
        positions={positions}
        matched={matched}
        selected={selected}
        palette={PALETTE}
        rings={ringsFor}
        flag={(node) => node.issues.length > 0}
        nodeTitle={nodeTitle}
        edgeTitle={edgeTitle}
        ariaLabel="Skill guidance graph"
        onSelect={(id) => navigate({ gnode: id }, { replace: true })}
        onFocus={(node) => navigate({ gq: focusQuery(node), gnode: node.id })}
      />
      <Legend palette={PALETTE} marks={MARKS} note={LEGEND_NOTE} />
      <EdgeLegend palette={PALETTE} groups={EDGE_GROUPS} />
      <Results
        nodes={nodes}
        matched={data.result.matched}
        total={data.result.total_matched}
        truncated={data.result.truncated}
        query={query}
        selected={selected}
        onSelect={(id) => navigate({ gnode: id }, { replace: true })}
      />
      {focused && (
        <Detail
          node={focused}
          onQuery={(text) => navigate({ gq: text, gnode: null })}
          onFocus={() => navigate({ gq: focusQuery(focused), gnode: focused.id })}
        />
      )}
    </section>
  )
}

/** How the guidance actually reaches a review, named — because two defects are only real in one. */
const MODE_LABEL: Record<string, string> = {
  agent: 'run as an agent',
  prompt: 'pasted into one prompt',
  unknown: 'runtime unknown',
}

/** Semantic colours, reusing the app palette rather than introducing a second one. */
const KIND_COLOR: Record<ShapeNodeKind, string> = {
  skill: 'var(--color-muted)',
  file: 'var(--color-accent)',
  section: 'var(--color-muted)',
  rule: 'var(--color-ink)',
  directive: 'var(--color-warn)',
  ref: 'var(--color-muted)',
  case: 'var(--color-good)',
  unresolved: 'var(--color-bad)',
}

const KIND_HELP: Record<ShapeNodeKind, string> = {
  skill: 'The folder itself.',
  file: 'A guidance file — SKILL.md, a companion page, or a wiki page.',
  section: 'A `#` heading, and what sits under it.',
  rule: 'An instruction with an id, which is what makes it traceable.',
  directive: 'An instruction with no id — nothing can provenance, test or protect it.',
  ref: 'The merge request or ADR a rule came from.',
  case: 'An eval case mined from the same review as a rule.',
  unresolved: 'A link naming no page this skill serves.',
}

/** Structural edges are the scaffolding and should recede; authored ones are the point. */
const EDGE_STYLE: Record<ShapeEdgeKind, { opacity: number; dash?: string }> = {
  contains: { opacity: 0.3 },
  states: { opacity: 0.35 },
  refers: { opacity: 0.9, dash: '4 3' },
  cites: { opacity: 0.35, dash: '2 3' },
  tested_by: { opacity: 0.6, dash: '1 3' },
  links: { opacity: 0.85, dash: '5 3' },
}

const EDGE_TITLE: Record<ShapeEdgeKind, string> = {
  contains: 'holds this',
  states: 'this instruction is stated here',
  refers: 'this rule names that one — they are coupled, across files',
  cites: 'this rule came out of that review, ticket or ADR',
  tested_by: 'that case was mined from the same review as this rule',
  links: 'a link an author wrote, and the only path an agent has to a page',
}

const PALETTE: GraphPalette = {
  colour: KIND_COLOR,
  help: KIND_HELP,
  hollow: 'unresolved',
  // Files are the map anyone orients by here, the way folders are in the sidecar graph.
  anchors: ['file', 'skill'],
  edge: EDGE_STYLE,
  edgeHelp: EDGE_TITLE,
}

/**
 * Health rings for one node.
 *
 * One ring, and only for the defect that changes *what the model is given*: a page the reviewer never
 * receives. Everything else the floor finds is about the guidance being untraceable rather than
 * unsent, and gets the corner wedge — which is the same division of labour the sidecar graph makes
 * between "this changes the prompt" and "this note is broken".
 */
function ringsFor(node: ShapeNode): Ring[] {
  const unsent = node.issues.includes('dropped') || node.issues.includes('unreachable')
  return unsent ? [{ colour: KIND_COLOR.unresolved, width: 1.6 }] : []
}

const MARKS = [
  {
    label: 'has a defect',
    help: 'Something mechanically wrong: a rule nothing tests, an instruction with no id, a link that outlived its file. On a file, something inside it.',
    swatch: 'dot' as const,
    colour: 'var(--color-bad)',
  },
  {
    label: 'never reaches the model',
    help: 'A page the byte cap drops from every pasted review, or — as an agent — one nothing links to, so it is never fetched. Either way its rules are not sent and the score was measured without them.',
    swatch: 'ring' as const,
    colour: KIND_COLOR.unresolved,
  },
]

const LEGEND_NOTE = 'a ring marks a query match · a bigger circle has more edges'

const EDGE_GROUPS = [
  {
    kinds: ['contains', 'states'],
    label: 'contains / states',
    help: 'The folder, its files, their headings, and the instructions under them.',
  },
  {
    kinds: ['refers'],
    label: 'refers',
    help: 'One rule naming another — the coupling that matters most when one of the two is rewritten, and that nothing else on any screen shows.',
  },
  {
    kinds: ['cites'],
    label: 'cites',
    help: 'The review a rule came from. Two rules mined from one discussion are connected here and nowhere else.',
  },
  {
    kinds: ['tested_by'],
    label: 'tested by',
    help: 'An eval case mined from the same merge request as the rule. Evidence that something in the corpus is about this rule — not proof that removing it would go red.',
  },
  {
    kinds: ['links'],
    label: 'links',
    help: 'A link an author wrote. Under `agent:` it is the only path to a companion page: the agent asks for a page by the exact path the instructions name.',
  },
]

/**
 * The query that centres the graph on one node.
 *
 * A file becomes its own contents; a rule becomes itself, since one hop already reaches what it is
 * attached to. A directive has no handle of its own — its text is a sentence — so it centres on the
 * file it lives in, which is what someone clicking one is asking to see anyway.
 */
function focusQuery(node: ShapeNode): string {
  if (node.kind === 'file' || node.kind === 'section') return `file:${node.path}`
  if (node.kind === 'rule' && node.rule) return `rule:${node.rule}`
  if (node.kind === 'directive') return `file:${node.path}`
  return node.label
}

/**
 * Everything about a node a hover can answer, so the picture is readable without clicking.
 *
 * What a reader wants from a dot is the things the colour and the label cannot say: where it lives,
 * how the file it is in reaches a review, how connected it is, and whether anything is wrong with it.
 */
function nodeTitle(node: ShapeNode): string {
  const lines = [`${node.kind}: ${node.label}`]
  if (node.path && node.path !== node.label) {
    lines.push(node.line ? `${node.path}:${node.line}` : node.path)
  }
  if (node.section && node.section !== node.label) lines.push(`## ${node.section}`)
  if (node.kind === 'file') {
    lines.push(
      `${node.bytes.toLocaleString()} chars · ${node.blocks} block(s) · ${node.rules} rule(s)`,
    )
    const how = node.delivery ? DELIVERY_HELP[node.delivery] : undefined
    if (how) lines.push(how)
  }
  if (node.kind === 'case' && node.case_kind) {
    lines.push(node.case_kind === 'should_catch' ? 'should catch' : 'should not flag')
    if (node.tier === 'archive') lines.push('archived — drawn at low weight')
  }
  if (node.text && node.kind !== 'file') lines.push(node.text)
  for (const message of node.issue_messages) lines.push(message)
  // Codes with no message of their own here — a rule's defect rolled up to its file.
  if (node.issues.length && !node.issue_messages.length) {
    lines.push(`inside: ${node.issues.join(', ')}`)
  }
  lines.push(`${node.degree} edge(s) · double-click to centre the graph here`)
  return lines.join('\n')
}

const DELIVERY_HELP: Record<string, string> = {
  always: 'sent on every review, in either runtime',
  'on-demand':
    'read with `read_skill_file` when the guidance points at it — or pasted, if the step is not an agent',
  retrieved: 'retrieved per changed path, not sent every time',
}

function edgeTitle(edge: ShapeEdge): string {
  const detail = edge.detail ? `\n${edge.detail}` : ''
  return `${edge.kind} — ${EDGE_TITLE[edge.kind]}${detail}`
}

function Results({
  nodes,
  matched,
  total,
  truncated,
  query,
  selected,
  onSelect,
}: {
  nodes: ShapeNode[]
  matched: string[]
  total: number
  truncated: boolean
  query: string
  selected: string | null
  onSelect: (id: string) => void
}) {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const rows = matched.map((id) => byId.get(id)).filter((node): node is ShapeNode => !!node)

  if (rows.length === 0) {
    return (
      <p className="text-sm text-muted">
        Nothing matches <code className="font-mono text-xs">{query}</code>. Fields are{' '}
        <code className="font-mono text-xs">kind:</code>{' '}
        <code className="font-mono text-xs">file:</code>{' '}
        <code className="font-mono text-xs">section:</code>{' '}
        <code className="font-mono text-xs">rule:</code>{' '}
        <code className="font-mono text-xs">ref:</code>{' '}
        <code className="font-mono text-xs">case:</code>{' '}
        <code className="font-mono text-xs">delivery:</code>{' '}
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
          ' — everything in this folder'
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
                {node.path && node.path !== node.label && (
                  <code className="font-mono text-xs text-muted">
                    {node.path}
                    {node.line ? `:${node.line}` : ''}
                  </code>
                )}
                {node.issues.map((code) => (
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
 * One selected node in full — and the queries that node makes worth asking.
 *
 * The buttons are the investigation half. A rule is most interesting as *the rules that name it* and
 * *the cases that test it*; a review as *everything that came out of it*. All are one edge away and
 * none is a question a text box invites you to type.
 */
function Detail({
  node,
  onQuery,
  onFocus,
}: {
  node: ShapeNode
  onQuery: (query: string) => void
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
        {node.kind === 'file' && node.delivery && (
          <span className="text-xs text-muted" title={DELIVERY_HELP[node.delivery]}>
            {node.delivery}
          </span>
        )}
      </div>
      {node.text && node.kind !== 'unresolved' && <p className="mt-2">{node.text}</p>}
      {node.path && (
        <p className="mt-2 font-mono text-xs text-muted">
          {node.path}
          {node.line ? `:${node.line}` : ''}
          {node.section && node.section !== node.label && ` · ## ${node.section}`}
        </p>
      )}
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
                title="Show everything with this defect"
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
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        {node.kind === 'rule' && node.rule && (
          <Ask onQuery={onQuery} query={node.rule}>
            everything that mentions {node.rule}
          </Ask>
        )}
        {node.kind === 'ref' && (
          <Ask onQuery={onQuery} query={`ref:${node.label}`}>
            everything from {node.label}
          </Ask>
        )}
        {(node.kind === 'file' || node.kind === 'section') && node.path && (
          <Ask onQuery={onQuery} query={`file:${node.path}`}>
            everything in {node.path}
          </Ask>
        )}
        {(node.kind === 'rule' || node.kind === 'directive') && node.path && (
          <Ask onQuery={onQuery} query={`file:${node.path}`}>
            the rest of {node.path}
          </Ask>
        )}
      </div>
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
