import { useState } from 'react'
import { useSkillFit, type FitComponent, type FitReport, type ModelFit } from '@/api/client'
import { Badge, ErrorNote } from '@/components/primitives'

/**
 * Whether this skill fits the context window it is about to be served through.
 *
 * `[runs] large_prompt_chars` is one global threshold, and it warns identically for a skill about to
 * run on a 200,000-token window and one about to run on a 4,096-token local model. It cannot say
 * which — so the failure it exists to catch stays invisible: guidance goes into one `SKILL.md`, the
 * character count looks unremarkable because nothing compares it to a window, and the skill is
 * quietly poor on the model it is actually served by.
 *
 * Two figures carry the panel. The **floor** is what every review pays before anything varies, and it
 * is the number an author controls; the **ceiling** adds the caps and the largest diff in the corpus.
 * The gap between the floor in one runtime and the other *is* the recommendation.
 *
 * The grade is a letter for scanning and a word for meaning, and the sentence beside it always quotes
 * the arithmetic. Both are about *fit* — the note at the bottom says so, because a letter travels
 * further than the paragraph under it.
 */
export function FitPanel({ skillId }: { skillId: string }) {
  // The probe is a request to somebody's model endpoint, so it happens because a person pressed a
  // button. Held in component state rather than the URL: it is an action, not a position.
  const [probe, setProbe] = useState(false)
  const { data, isLoading, error } = useSkillFit(skillId, probe)

  if (error) return <ErrorNote error={error} />
  if (!data) return <p className="text-sm text-muted">{isLoading ? 'Measuring…' : null}</p>

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        {RUNTIME[data.mode]} The floor is what every review pays before the diff, the wiki or any
        local context arrive — on every case of every trial, on both sides of a gate.
      </p>

      <Components components={data.components} report={data} />
      <Windows models={data.models} />

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setProbe(true)}
          disabled={probe}
          title="Ask the configured endpoint what context window it actually serves. Off until asked: a page load must not call a model endpoint."
          className="rounded border border-line bg-canvas px-2 py-1 text-xs hover:border-accent disabled:opacity-40"
        >
          {probe ? 'Asked the endpoint' : 'Ask the endpoint'}
        </button>
        {data.probe_status && <span className="text-xs text-warn">{data.probe_status}</span>}
      </div>

      {data.advice.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs tracking-wide text-muted uppercase">What to change</h4>
          <ul className="space-y-1.5">
            {data.advice.map((line) => (
              <li
                key={line}
                className="rounded border border-accent/30 bg-accent/5 px-2.5 py-1.5 text-sm"
              >
                {line}
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.notes.map((note) => (
        <p key={note} className="text-xs text-muted italic">
          {note}
        </p>
      ))}
    </div>
  )
}

const RUNTIME: Record<string, string> = {
  prompt:
    'This skill is pasted into one prompt: SKILL.md and every companion page, on every review.',
  agent:
    'This skill runs as an agent: SKILL.md is the instruction set and the pages are fetched on demand.',
  unknown:
    'Whetstone does not assemble this skill’s review prompt, so the figures below describe what its own reviewer is handed rather than what it sends.',
}

/** What takes up room, fixed parts first, each with the sentence that produced its size. */
function Components({ components, report }: { components: FitComponent[]; report: FitReport }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted">
            <th className="py-1 font-medium">In every prompt</th>
            <th className="py-1 text-right font-medium">chars</th>
            <th className="py-1 text-right font-medium">~tokens</th>
            <th className="py-1 pl-3 font-medium">where the number comes from</th>
          </tr>
        </thead>
        <tbody>
          {components.map((component) => (
            <tr key={component.name} className="border-t border-line/60 align-top">
              <td className="py-1 pr-3 whitespace-nowrap">
                {component.name}{' '}
                {component.fixed ? (
                  <Badge tone="neutral" title="Paid on every review, whatever the change is.">
                    fixed
                  </Badge>
                ) : (
                  <Badge tone="neutral" title="Varies per case, and this is its worst case.">
                    varies
                  </Badge>
                )}
              </td>
              <td className="py-1 text-right tabular">{component.chars.toLocaleString()}</td>
              <td className="py-1 text-right tabular">{component.tokens.toLocaleString()}</td>
              <td className="py-1 pl-3 text-xs text-muted">{component.basis}</td>
            </tr>
          ))}
          <tr className="border-t border-line font-medium">
            <td className="py-1">floor · ceiling</td>
            <td />
            <td className="py-1 text-right tabular">
              {report.floor_tokens.toLocaleString()} · {report.ceiling_tokens.toLocaleString()}
            </td>
            <td className="py-1 pl-3 text-xs font-normal text-muted">
              tokens estimated at {report.chars_per_token} characters each — the ratio
              `llm/limits.py` uses, which errs high on code
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}

/** One row per window: the grade, the word, the share of the window, and why. */
function Windows({ models }: { models: ModelFit[] }) {
  return (
    <ul className="space-y-1.5">
      {models.map((row) => (
        <li
          key={`${row.window.source}:${row.window.label}`}
          className={`rounded-lg border px-3 py-2 ${BORDER[row.verdict] ?? 'border-line'}`}
        >
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
            <span
              className={`w-4 text-center font-mono font-semibold ${TONE[row.verdict] ?? ''}`}
              title="A grade about whether the guidance fits — never about whether it is any good."
            >
              {row.grade}
            </span>
            <span className={`w-20 text-xs ${TONE[row.verdict] ?? ''}`}>{row.verdict}</span>
            <span className="font-medium" title={row.window.example || row.window.note}>
              {row.window.label}
            </span>
            <span className="tabular text-xs text-muted">
              {row.window.tokens.toLocaleString()} tokens
            </span>
            <SourceBadge source={row.window.source} note={row.window.note} />
            <span className="ml-auto flex items-center gap-2">
              <Share value={row.floor_share} />
              <span className="tabular text-xs text-muted">
                {(row.floor_share * 100).toFixed(0)}% guidance
              </span>
            </span>
          </div>
          <p className="mt-1 text-xs text-muted">{row.why}</p>
        </li>
      ))}
    </ul>
  )
}

const TONE: Record<string, string> = {
  overflows: 'text-bad',
  tight: 'text-warn',
  crowded: 'text-warn',
  fits: 'text-good',
}

const BORDER: Record<string, string> = {
  overflows: 'border-bad/40 bg-bad/5',
  tight: 'border-warn/40 bg-warn/5',
  crowded: 'border-warn/30',
  fits: 'border-line',
}

/**
 * How much of the window the guidance takes, as a bar.
 *
 * Clamped at full rather than allowed to overflow its track: a bar drawn past its own container reads
 * as a rendering bug, and the row already says by how much in words and in the headroom figure.
 */
function Share({ value }: { value: number }) {
  const percent = Math.min(100, Math.max(0, value * 100))
  return (
    <span className="inline-block h-1.5 w-16 overflow-hidden rounded-full bg-line align-middle">
      <span
        className="block h-full rounded-full"
        style={{
          width: `${percent}%`,
          backgroundColor:
            value >= 0.5
              ? 'var(--color-bad)'
              : value >= 0.25
                ? 'var(--color-warn)'
                : 'var(--color-accent)',
        }}
      />
    </span>
  )
}

/**
 * Where the window's number came from — the thing a reader trusting a grade is really trusting.
 *
 * `measured` is the endpoint's own answer, `configured` is an operator's statement, and `published`
 * is a size band this project ships rather than a claim about any particular model's current limit.
 */
function SourceBadge({ source, note }: { source: string; note: string }) {
  const help: Record<string, string> = {
    published:
      'A size band, not a claim about a specific model. Ask the endpoint, or state your model in `[[models]]` in whetstone.toml, for an exact number.',
    configured: note || 'Stated in whetstone.toml.',
    measured: note || 'Read off the configured endpoint.',
  }
  return (
    <Badge tone={source === 'measured' ? 'accent' : 'neutral'} title={help[source] ?? note}>
      {source}
    </Badge>
  )
}
