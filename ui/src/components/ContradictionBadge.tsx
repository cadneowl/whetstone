import { type Contradiction } from '@/api/client'
import { Badge } from '@/components/primitives'

/**
 * How strong the evidence behind one contradicting pair is — measured, or wording alone.
 *
 * Shared by the Eval cases tab and the health panel so the same flag never carries two
 * explanations: when this copy is refined, both surfaces say the new thing.
 */
export function ContradictionBadge({ pair }: { pair: Contradiction }) {
  if (pair.from_history) {
    return (
      <Badge tone="warn" title={`Measured across ${pair.runs} run(s)`}>
        measured
      </Badge>
    )
  }
  return (
    <Badge tone="neutral" title="Flagged on wording alone — no run has scored them together yet">
      wording only
    </Badge>
  )
}
