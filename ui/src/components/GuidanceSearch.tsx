import { useEffect, useState } from 'react'
import { useGuidanceSearch, type GuidanceChunk } from '@/api/client'
import { ErrorNote } from '@/components/primitives'

/**
 * Find something in a skill's own guidance, across every file the reviewer is given.
 *
 * The tab below renders the whole folder top to bottom, which answers *"what are the rules"*. This
 * answers the other question — *"is there already a rule about swallowed errors"* — which is the
 * one asked immediately before writing a new one, and which scrolling answers badly the moment a
 * skill outgrows `SKILL.md` into `patterns/rust.md` and a wiki page.
 *
 * Two halves, ordered by how much they can be trusted. Exact matches in document order, then
 * blocks that *mean* something close and contain none of what you typed — labelled, scored, and
 * never able to reorder or displace an exact one.
 */
export function GuidanceSearch({ skillId }: { skillId: string }) {
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')

  useEffect(() => {
    const timer = setTimeout(() => setQuery(draft.trim()), 250)
    return () => clearTimeout(timer)
  }, [draft])

  const { data, error } = useGuidanceSearch(skillId, query)
  const active = query.length > 0

  return (
    <section className="mb-4">
      <input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        placeholder="Search this skill's guidance — unwrap · rule:R1 · file:patterns · kind:wiki"
        aria-label="Search this skill's guidance"
        className="w-full rounded border border-line bg-canvas px-2.5 py-1.5 font-mono text-sm"
      />
      {error && <ErrorNote error={error} />}
      {active && data && (
        <div className="mt-2 space-y-3">
          <Exact result={data} />
          <Close result={data} />
        </div>
      )}
    </section>
  )
}

type Result = NonNullable<ReturnType<typeof useGuidanceSearch>['data']>

function Exact({ result }: { result: Result }) {
  if (result.matched.length === 0) {
    // The count is the useful half of "nothing found": it separates "this skill says nothing like
    // that" from "there is barely any guidance here to say it in".
    return (
      <p className="text-sm text-muted">
        None of this skill&apos;s {result.chunks} guidance block
        {result.chunks === 1 ? '' : 's'} contains{' '}
        <code className="font-mono text-xs">{result.query}</code>.
        {result.semantic.length === 0 && (
          <>
            {' '}
            Fields are <code className="font-mono text-xs">rule:</code>{' '}
            <code className="font-mono text-xs">file:</code>{' '}
            <code className="font-mono text-xs">section:</code>{' '}
            <code className="font-mono text-xs">kind:</code>; anything else is a substring.
          </>
        )}
      </p>
    )
  }
  return (
    <div>
      <p className="mb-1.5 text-sm text-muted">
        {result.total_matched} of {result.chunks} blocks contain{' '}
        <code className="font-mono text-xs">{result.query}</code>
        {result.truncated && (
          <span className="text-warn"> · showing the first {result.matched.length}</span>
        )}
      </p>
      <ul className="space-y-1">
        {result.matched.map((chunk) => (
          <Row key={chunk.id} chunk={chunk} />
        ))}
      </ul>
    </div>
  )
}

function Close({ result }: { result: Result }) {
  if (result.semantic_status) {
    return (
      <p className="text-sm text-muted">
        <span className="text-warn">Meaning search off.</span> {result.semantic_status}
      </p>
    )
  }
  if (result.semantic.length === 0) return null
  return (
    <div>
      <p className="mb-1.5 text-sm text-muted">
        Also close in meaning — {result.semantic.length} block
        {result.semantic.length === 1 ? '' : 's'} that contain none of what you typed.{' '}
        <span title="Cosine similarity against a local embedding model. Additive only: it can add rows below the exact matches and can never reorder or hide one.">
          Scored, not matched.
        </span>
      </p>
      <ul className="space-y-1">
        {result.semantic.map((chunk) => (
          <Row key={chunk.id} chunk={chunk} score={result.scores[chunk.id]} />
        ))}
      </ul>
    </div>
  )
}

/**
 * One hit: where it is, and what it says.
 *
 * A rule links to its anchor in the rendered guidance below, which `Guidance.tsx` already emits as
 * `#rule-R1`. Anything else shows its text and its line, because there is nothing on the page to
 * point at — and the text *is* the answer, so a row that carries it has already been useful.
 */
function Row({ chunk, score }: { chunk: GuidanceChunk; score?: number }) {
  const body = (
    <>
      <span className="flex flex-wrap items-baseline gap-x-2">
        {score !== undefined && (
          <span className="tabular font-mono text-xs text-muted">{score.toFixed(2)}</span>
        )}
        {chunk.rule && <span className="font-mono text-xs text-accent">{chunk.rule}</span>}
        <code className="font-mono text-xs text-muted">
          {chunk.source}:{chunk.line}
        </code>
        {chunk.section && <span className="text-xs text-muted">· {chunk.section}</span>}
        {chunk.kind === 'wiki' && (
          <span
            className="text-xs text-muted"
            title="Repo context, retrieved per change rather than sent on every review — so this text reaches the reviewer only when a changed path matches its globs."
          >
            · wiki
          </span>
        )}
      </span>
      <span className="mt-0.5 block">{chunk.text}</span>
    </>
  )
  const className = `block w-full rounded border px-2.5 py-1.5 text-left text-sm ${
    score === undefined ? 'border-line/60' : 'border-line border-dashed'
  }`
  return (
    <li>
      {chunk.rule ? (
        <a href={`#rule-${chunk.rule}`} className={`${className} hover:border-accent`}>
          {body}
        </a>
      ) : (
        <div className={className}>{body}</div>
      )}
    </li>
  )
}
