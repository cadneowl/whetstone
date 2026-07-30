import { Badge } from './primitives'

/**
 * The `human_signal` vocabulary, rendered.
 *
 * `domain/eval_model.py` keeps these strings closed and free of detail so they stay machine-
 * answerable. That makes them terse to the point of opacity — "merged clean" does not tell a
 * newcomer that it means *nobody commented, so this is a guess from silence*. This is where that
 * gets said, once, rather than in each screen that shows a candidate.
 *
 * The order is the builder's confidence order, so a legend reads as a ranking.
 */
export interface SignalMeta {
  /** Matches `SIGNAL_*` in `domain/eval_model.py`. */
  id: string
  /** For a narrow queue row, where the full name would push the confidence off the edge. */
  short: string
  tone: 'neutral' | 'accent' | 'good' | 'warn' | 'bad'
  /** What the signal actually claims — shown on hover and in the legend. */
  meaning: string
}

export const SIGNALS: SignalMeta[] = [
  {
    id: 'escaped defect',
    short: 'escaped',
    // Red because it is a failure, not because it is weak: review missed this and it shipped.
    tone: 'bad',
    meaning:
      'A defect that reached production. Review demonstrably missed it — the strongest recall evidence there is.',
  },
  {
    id: 'finding rejected',
    short: 'false positive',
    tone: 'warn',
    meaning:
      'The skill raised this and a person ruled it wrong. The least ambiguous negative there is — nothing is inferred from what anyone said. As a case it holds the reviewer to staying silent here.',
  },
  {
    id: 'suggestion applied',
    short: 'applied',
    tone: 'good',
    meaning: 'A reviewer proposed a change and the author took it. A confirmed catch.',
  },
  {
    id: 'finding confirmed',
    short: 'confirmed',
    tone: 'good',
    meaning:
      "The skill raised this and a person agreed. Ranked below a rejection because the case asserts the reviewer must say *this*, and until the semantic is rewritten *this* is the reviewer's own message.",
  },
  {
    id: 'suggested fix applied',
    short: 'the fix',
    tone: 'good',
    meaning:
      'The accepted replacement itself. Code endorsed twice over, so flagging it would be a false positive.',
  },
  {
    id: 'synthetic counterfactual',
    short: 'counterfactual',
    tone: 'neutral',
    meaning:
      "Generated, not observed: a real case's diff reversed, so this is the defect being removed. Flagging the fix for the very defect the parent documents would be a false positive.",
  },
  {
    id: 'suggestion declined',
    short: 'declined',
    tone: 'warn',
    meaning:
      'A reviewer proposed a change and the thread closed without it. A confirmed false alarm.',
  },
  {
    id: 'synthetic mutation',
    short: 'mutation',
    tone: 'neutral',
    meaning:
      "Generated, not observed: the parent case's defect wearing different names. If the reviewer catches the parent but misses this, the guidance memorized the incident, not the pattern.",
  },
  {
    id: 'reviewer comment resolved',
    short: 'resolved',
    tone: 'accent',
    meaning:
      'An inline comment whose thread was resolved. Something was raised here; whether code changed is not recorded.',
  },
  {
    id: 'reviewer comment left open',
    short: 'open',
    tone: 'neutral',
    meaning: 'An inline comment still unresolved — an argument in progress, not a verdict.',
  },
  {
    id: 'merged clean',
    short: 'no comments',
    tone: 'neutral',
    meaning:
      'Nobody commented on this merge request. Inferred from silence, which is the weakest evidence the builder produces — and silence is not the same as there being nothing to flag.',
  },
]

const BY_ID = new Map(SIGNALS.map((s) => [s.id, s]))

export function signalMeta(id: string | null | undefined): SignalMeta {
  return (
    BY_ID.get(id ?? '') ?? {
      id: id || 'hand-written',
      short: id || 'hand-written',
      tone: 'neutral',
      meaning: 'Written by hand rather than derived from review history.',
    }
  )
}

export function SignalBadge({ id, short }: { id: string | null | undefined; short?: boolean }) {
  const meta = signalMeta(id)
  return (
    <Badge tone={meta.tone} title={meta.meaning}>
      {short ? meta.short : meta.id}
    </Badge>
  )
}

type Provenance = { source?: string; ref?: string | null; human_signal?: string | null }

/**
 * Where a case came from — a merge request, a Jira defect, a live review — as a compact chip.
 *
 * The `human_signal` says what the evidence *claims* (SignalBadge); this says where it *came from*,
 * which is the thing a person scanning the improve loop wants: proof the cases are real MR/Jira/
 * review signal rather than a cryptic id. Reads the `ref` for the concrete reference (MR number,
 * issue key) so the chip is a pointer, not just a category.
 */
export function SourceBadge({ provenance }: { provenance?: Provenance | null }) {
  const s = sourceOf(provenance)
  if (!s) return null
  return (
    <Badge tone={s.tone} title={s.title}>
      {s.text}
    </Badge>
  )
}

function sourceOf(
  p?: Provenance | null,
): { text: string; tone: SignalMeta['tone']; title: string } | null {
  if (!p) return null
  const source = p.source ?? 'manual'
  const ref = p.ref ?? ''
  if (source.includes('issue')) {
    const key = ref.match(/^[A-Z][A-Z0-9]+-\d+/)?.[0]
    return {
      text: key ?? 'defect',
      tone: 'warn',
      title: `Escaped defect${ref ? ` — ${ref}` : ''}. Review missed it and it shipped — the strongest recall evidence there is.`,
    }
  }
  if (source === 'skill_review' || source === 'review_miss') {
    return {
      text: 'review',
      tone: 'accent',
      title: `From a live review${ref ? ` of ${ref}` : ''} — the skill's own output on a real change, ruled by a person.`,
    }
  }
  if (source.startsWith('gitlab') || source === 'merge_request') {
    const mr = ref.match(/![0-9]+/)?.[0]
    return {
      text: mr ? `MR ${mr}` : 'MR',
      tone: 'accent',
      title: `Mined from merge request${ref ? ` ${ref}` : ''}.`,
    }
  }
  if (source.startsWith('synthetic')) {
    return { text: 'synthetic', tone: 'neutral', title: 'Generated, not mined from review history.' }
  }
  if (source === 'manual') {
    return {
      text: 'manual',
      tone: 'neutral',
      title: 'Written by hand rather than derived from review history.',
    }
  }
  // An unrecognized source shows raw, so a new signal kind never silently disappears from a row.
  return { text: source, tone: 'neutral', title: ref || source }
}
