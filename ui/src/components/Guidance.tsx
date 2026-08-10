import type { ReactNode } from 'react'
import type { SkillDetail } from '@/api/client'
import { Badge } from './primitives'

/**
 * Renders a skill's guidance — the whole folder — and anchors each rule to its provenance.
 *
 * Purpose-built rather than a markdown library: the requirement isn't "render markdown", it's
 * "show each rule with the review signals that justified it, and mark the ones nothing tests".
 * Building only what SKILL.md actually uses also avoids `dangerouslySetInnerHTML` entirely — no
 * sanitiser to keep correct.
 *
 * The companion pages are rendered after the body because a skill is a folder: `SKILL.md` is often
 * a table of contents, and this panel's own caption promises "the exact prose the reviewer is
 * given". Showing the body alone made that caption false in the worst way — a page listing no
 * rules at all, above a promise that these were all of them.
 */
export function Guidance({ detail }: { detail: SkillDetail }) {
  const { skill, untested_rules, has_runs } = detail
  const untested = new Set(untested_rules)
  const render = (text: string, keyPrefix: string) =>
    text
      .split('\n\n')
      .filter((block) => block.trim())
      .map((block, i) => (
        <Block
          key={`${keyPrefix}-${i}`}
          text={block}
          provenance={skill.provenance}
          untested={untested}
          hasRuns={has_runs}
        />
      ))

  // `id` per file, matching the `#rule-R7` anchors the bullets already carry. The graph above this
  // panel draws a dot per file and per rule, and until both had somewhere to land, "which page is
  // that" was answered by scrolling and guessing.
  return (
    <div className="space-y-3">
      <section id="file-SKILL.md" className="space-y-3">
        {render(skill.body, 'body')}
      </section>
      {skill.pages.map((page) => (
        <section
          key={page.path}
          id={`file-${page.path}`}
          className="space-y-3 border-t border-line pt-3"
        >
          <p className="font-mono text-xs text-muted">{page.path}</p>
          {render(page.text, page.path)}
        </section>
      ))}
      {!has_runs && (
        <p className="pt-2 text-xs text-muted italic">
          No runs recorded yet — which rules are actually exercised can't be determined until this
          skill has been evaluated at least once.
        </p>
      )}
    </div>
  )
}

type Provenance = SkillDetail['skill']['provenance']

function Block({
  text,
  provenance,
  untested,
  hasRuns,
}: {
  text: string
  provenance: Provenance
  untested: Set<string>
  hasRuns: boolean
}) {
  const trimmed = text.trim()

  if (trimmed.startsWith('#')) {
    const level = trimmed.match(/^#+/)?.[0].length ?? 1
    const content = trimmed.replace(/^#+\s*/, '')
    return level <= 1 ? (
      <h3 className="text-base font-semibold">{content}</h3>
    ) : (
      <h4 className="text-sm font-semibold text-muted">{content}</h4>
    )
  }

  const bullets = trimmed.split('\n').filter((l) => /^\s*[-*]\s/.test(l))
  if (bullets.length > 0) {
    return (
      <ul className="space-y-2">
        {joinWrapped(trimmed).map((item, i) => (
          <Bullet
            key={i}
            text={item}
            provenance={provenance}
            untested={untested}
            hasRuns={hasRuns}
          />
        ))}
      </ul>
    )
  }

  return <p className="text-sm leading-relaxed">{inline(trimmed)}</p>
}

function Bullet({
  text,
  provenance,
  untested,
  hasRuns,
}: {
  text: string
  provenance: Provenance
  untested: Set<string>
  hasRuns: boolean
}) {
  const body = text.replace(/^\s*[-*]\s+/, '')
  const ruleId = /\*\*\s*([A-Z][A-Z0-9]*\d)\b/.exec(body)?.[1]
  const refs = ruleId ? (provenance[ruleId] ?? []) : []

  return (
    <li id={ruleId ? `rule-${ruleId}` : undefined} className="text-sm leading-relaxed">
      <div className="flex flex-wrap items-baseline gap-2">
        <span>{inline(body)}</span>
        {ruleId && hasRuns && untested.has(ruleId) && (
          <Badge
            tone="warn"
            title="The reviewer never cited this rule in the latest run, so any cases guarding it passed without exercising it"
          >
            untested guidance
          </Badge>
        )}
      </div>
      {refs.length > 0 && (
        <p className="mt-1 text-xs text-muted">
          {ruleId} ←{' '}
          {refs.map((r, i) => (
            <span key={i}>
              {i > 0 && ', '}
              <code className="font-mono">{r.ref ?? r.source}</code>
            </span>
          ))}
        </p>
      )}
    </li>
  )
}

/** Markdown wraps bullets across lines; rejoin continuations before rendering. */
function joinWrapped(block: string): string[] {
  const items: string[] = []
  for (const line of block.split('\n')) {
    if (/^\s*[-*]\s/.test(line) || items.length === 0) items.push(line)
    else items[items.length - 1] += ' ' + line.trim()
  }
  return items.filter((i) => i.trim())
}

/** `**bold**` and `` `code` `` only — as React nodes, never as raw HTML. */
function inline(text: string): ReactNode[] {
  const nodes: ReactNode[] = []
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g
  let last = 0
  let match: RegExpExecArray | null

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index))
    const token = match[0]
    if (token.startsWith('**')) {
      nodes.push(
        <strong key={match.index} className="font-semibold">
          {token.slice(2, -2)}
        </strong>,
      )
    } else {
      nodes.push(
        <code key={match.index} className="rounded bg-line/60 px-1 py-px font-mono text-[0.85em]">
          {token.slice(1, -1)}
        </code>,
      )
    }
    last = match.index + token.length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}
