import { useEffect, useState } from 'react'
import { useImprovePrompt, type ImprovePrompt as Prompt, type JobRequest } from '@/api/client'
import { Badge, ErrorNote } from '@/components/primitives'

/**
 * The steer box and this panel share one piece of state, so without a delay every keystroke in
 * "Steer this run" is a new query key and therefore a new request — each one re-reading the run and
 * re-clustering its failures, server-side, to show a prompt that is about to change again.
 */
function useSettled<T>(value: T, ms = 400): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return settled
}

/**
 * What the drafter is about to be shown — `improve/prompt.md` with every `{{variable}}` filled.
 *
 * This is the one input in the loop that was invisible. A run is a score you can drill into, a gate
 * is a verdict with its reasons, a draft is a rewrite you read line by line. The prompt behind the
 * draft is assembled from six moving parts — the failure digest, the clustering, the holdout
 * blindfold, the case narrowing, the guidance and its pages, the wiki — and when a draft comes back
 * wrong the first question is what it was shown. Until this panel the only way to answer that was to
 * read `improve.py`, which is why a bug that collapsed the whole promoted corpus into one cluster
 * survived: the prompt said "and 6 more like it" and nothing put those words in front of anyone.
 *
 * Rendered from the same request the launch button holds, through the same assembly `propose` uses,
 * so this is the prompt that launch would send rather than a plausible reconstruction of it.
 */
export function ImprovePromptPanel({ request }: { request: JobRequest }) {
  const [open, setOpen] = useState(false)
  const instruction = useSettled(request.instruction ?? '')
  const { data, error, isFetching } = useImprovePrompt({ ...request, instruction }, open)

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="text-xs text-muted transition-colors hover:text-ink"
        title="The improve step's prompt file, with its variables filled — what the model will read"
      >
        {open ? '▾' : '▸'} the prompt this would send
      </button>
      {open && (
        <div className="mt-2 space-y-2 rounded-lg border border-line bg-canvas p-3">
          {isFetching && !data && <p className="text-xs text-muted">Assembling…</p>}
          {error != null && <ErrorNote error={error} />}
          {data && <Rendered prompt={data} />}
        </div>
      )}
    </div>
  )
}

function Rendered({ prompt }: { prompt: Prompt }) {
  return (
    <>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-muted">
        <span className="font-mono">{prompt.source}</span>
        {prompt.from_run ? (
          <span>
            from run <span className="font-mono">{prompt.from_run}</span>
          </span>
        ) : (
          <span>no run — the failure section is empty</span>
        )}
        <span>
          {prompt.total_failures} failure(s), shown as {prompt.shown} cluster(s)
        </span>
        {prompt.holdout_withheld > 0 && (
          <Badge
            tone="neutral"
            title="Failures on holdout cases, deliberately kept out: a drafter shown the exam turns the overfitting alarm into training data."
          >
            {prompt.holdout_withheld} withheld as holdout
          </Badge>
        )}
        {prompt.runs_as_agent && (
          <Badge
            tone="accent"
            title="Its instructions are the skill's own SKILL.md plus a runtime preamble, assembled per call. The text below is the task message it opens on."
          >
            drafts as an agent
          </Badge>
        )}
      </div>

      {prompt.warnings.map((warning) => (
        <p key={warning} className="text-xs text-warn">
          ⚠ {warning}
        </p>
      ))}

      {/* Which variables the template places, and how much text each one turned into. A variable
          the template never names renders as an absence — nothing errors, nothing is missing, and
          the drafter simply never sees that input. This is where that becomes visible. */}
      <p className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
        {prompt.variables.map((v) => (
          <span
            key={v.name}
            className={v.used ? '' : 'opacity-50'}
            title={
              v.used
                ? `${v.chars} characters, placed by the template`
                : `${v.chars} characters — your template never places {{${v.name}}}`
            }
          >
            <span className="font-mono">
              {v.used ? '' : '·'}
              {v.name}
            </span>{' '}
            {v.chars}
          </span>
        ))}
      </p>
      {prompt.appended.length > 0 && (
        <p className="text-[11px] text-muted">
          appended by Whetstone because the template does not place{' '}
          {prompt.appended.map((name) => `{{${name}}}`).join(' or ')} — never dropped, so a steer
          you typed always reaches the model
        </p>
      )}

      <Block
        label={
          prompt.calls_a_model
            ? 'the prompt, as sent'
            : 'the digest, as handed to your program on stdin'
        }
        text={prompt.text}
        open
      />
      {prompt.system && <Block label="the system prompt" text={prompt.system} />}
      {prompt.template && <Block label="the template it rendered from" text={prompt.template} />}
    </>
  )
}

function Block({ label, text, open = false }: { label: string; text: string; open?: boolean }) {
  const [shown, setShown] = useState(open)
  return (
    <div>
      <button
        type="button"
        onClick={() => setShown(!shown)}
        className="text-xs text-muted transition-colors hover:text-ink"
      >
        {shown ? '▾' : '▸'} {label} ({text.length} chars)
      </button>
      {shown && (
        <pre className="mt-1 max-h-96 overflow-auto rounded border border-line bg-surface px-2 py-1.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
          {text}
        </pre>
      )}
    </div>
  )
}
