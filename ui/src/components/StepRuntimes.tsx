import type { components } from '@/api/schema'

type Runtime = components['schemas']['StepRuntime']

/**
 * How each of a skill's steps actually runs, on the page rather than in a YAML file.
 *
 * `agent:` decides whether a skill is *run* or *pasted* — the largest single difference in what a
 * model sees — and it appeared on no screen. The symptom when it was off was a drafting prompt
 * quietly carrying a whole folder: 162,972 characters of companion pages, discoverable only by
 * expanding a collapsed panel and reading a size table. You had to already know the setting existed
 * to go looking for it.
 *
 * So it sits under the title, always, for every skill. A single-file skill on one prompt is a
 * perfectly good answer and says so plainly; the point is that the answer is visible either way.
 */
export function StepRuntimes({ steps }: { steps: Runtime[] }) {
  const shown = steps.filter((s) => s.present)
  if (shown.length === 0) return null

  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs">
      <span className="text-muted">runs as</span>
      {shown.map((step) => (
        <Row key={step.kind} step={step} />
      ))}
    </div>
  )
}

function Row({ step }: { step: Runtime }) {
  const broken = Boolean(step.problem)
  // `bad`, not `danger`: the palette is canvas/surface/line/ink/muted/good/bad/warn/accent, and
  // Tailwind emits a utility only for a token that exists. `text-danger` compiled to nothing, so
  // the one row that most needed to look like a warning rendered in body colour.
  return (
    <span className="flex items-baseline gap-1.5" title={step.problem || step.note}>
      <span className="font-mono text-muted">{step.kind}</span>
      <span className={broken ? 'font-medium text-bad' : 'font-medium'}>
        {broken ? '⚠ ' : ''}
        {label(step)}
      </span>
      {step.note && !broken && <span className="text-muted">· {step.note}</span>}
      {broken && <span className="text-bad">· {step.mode === 'none' ? 'see the file' : 'refuses to run'}</span>}
    </span>
  )
}

/**
 * Plain words, not the YAML key. "prompt" is what someone reading `agent: enabled: false` has to
 * infer; "one pasted prompt" is the thing that is actually happening to their skill.
 */
function label(step: Runtime): string {
  switch (step.mode) {
    case 'agent':
      return 'an agent'
    case 'prompt':
      return 'one pasted prompt'
    case 'program':
      return 'your program'
    case 'task':
      return 'a task agent'
    default:
      // A step file that would not parse has no mode, and "not configured" would be a lie about a
      // file that is right there and broken.
      return step.problem ? 'a broken step file' : 'not configured'
  }
}
