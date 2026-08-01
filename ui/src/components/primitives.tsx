import type { ReactNode } from 'react'
import type { Outcome } from '@/api/client'

/** Colour carries meaning here, so every use is paired with a word or a title. */
const OUTCOME_STYLE: Record<Outcome, string> = {
  tp: 'text-good border-good',
  tn: 'text-good border-good',
  fn: 'text-bad border-bad',
  fp: 'text-bad border-bad',
}

export const OUTCOME_TITLE: Record<Outcome, string> = {
  tp: 'caught, as expected',
  fn: 'missed — the reviewer should have flagged this',
  fp: 'falsely flagged — the reviewer should have stayed quiet',
  tn: 'correctly silent',
}

export function OutcomeChip({ outcome }: { outcome: Outcome }) {
  return (
    <span
      title={OUTCOME_TITLE[outcome]}
      className={`rounded border px-1 font-mono text-[11px] uppercase ${OUTCOME_STYLE[outcome]}`}
    >
      {outcome}
    </span>
  )
}

export function Badge({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode
  tone?: 'neutral' | 'accent' | 'good' | 'warn' | 'bad'
  title?: string
}) {
  const tones = {
    neutral: 'text-muted border-line',
    accent: 'text-accent border-accent/40',
    good: 'text-good border-good/50',
    warn: 'text-warn border-warn/50',
    bad: 'text-bad border-bad/50',
  }
  return (
    <span
      title={title}
      className={`rounded-full border px-2 py-px text-xs whitespace-nowrap ${tones[tone]}`}
    >
      {children}
    </span>
  )
}

export function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-28 rounded-lg border border-line bg-surface px-3 py-2">
      <div className="text-[11px] tracking-wide text-muted uppercase">{label}</div>
      <div className="tabular text-xl">{value}</div>
    </div>
  )
}

/**
 * A recall trend, oldest to newest. Deliberately unlabelled and small — it answers "is this getting
 * better or worse?" at a glance, and the runs table answers everything more precise.
 */
export function Sparkline({ values, className = '' }: { values: number[]; className?: string }) {
  if (values.length < 2) return <span className="text-xs text-muted">—</span>
  const width = 64
  const height = 18
  const step = width / (values.length - 1)
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - v * (height - 2) - 1).toFixed(1)}`)
    .join(' ')
  const last = values[values.length - 1] ?? 0
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className={`h-[18px] w-16 overflow-visible ${className}`}
      role="img"
      aria-label={`recall trend, latest ${last.toFixed(2)}`}
    >
      <polyline
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-muted italic">{children}</p>
}

/**
 * What this screen is, and what you do on it. One or two sentences, on every screen.
 *
 * Whetstone's pipeline is not guessable from its nouns: nothing about the word "Triage" tells you
 * that its output is a git branch, and "Reviews" and "Runs" both sound like "results" while one is
 * a queue of decisions and the other is history. The alternative to a line of prose per screen is
 * an operator who has to be told once, verbally, by someone who already knows — which does not
 * scale past the person who wrote it.
 *
 * Kept to `what it is` + `what you do here`. Anything longer stops being read, and a rubric nobody
 * reads is worse than none because it looks like the documentation exists.
 */
export function Intro({ children }: { children: ReactNode }) {
  return <p className="mt-1 max-w-3xl text-sm text-muted">{children}</p>
}

export function Loading() {
  return <p className="py-8 text-center text-sm text-muted">Loading…</p>
}

export function ErrorNote({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error)
  const path = (error as { problem?: { path?: string } })?.problem?.path
  return (
    <div className="rounded-lg border border-bad/40 bg-bad/5 px-4 py-3 text-sm">
      {/* Kept as written: these messages explain a refusal and name the fix, sometimes over more
          than one paragraph, and a `<p>` collapsing the newlines buries the second half. */}
      <p className="text-bad break-words whitespace-pre-wrap">{message}</p>
      {path && <p className="mt-1 font-mono text-xs text-muted">{path}</p>}
    </div>
  )
}

/** Severity crosses the wire as an IntEnum (10/20/30); never show the number. */
export function severityName(value: number): string {
  return value >= 30 ? 'error' : value >= 20 ? 'warning' : 'info'
}

export function score(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

export function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
