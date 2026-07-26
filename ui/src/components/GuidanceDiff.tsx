import { useMemo } from 'react'

/**
 * What a guidance edit actually changes, line by line.
 *
 * The editor used to show a textarea whose entire contents had been replaced by a drafted proposal
 * and a rendered preview of the result. Both are true and neither answers the only question worth
 * asking at that moment: *what is different?* A rewrite that quietly dropped four working rules
 * looked exactly like one that tightened a fifth.
 *
 * Computed here rather than server-side because it has to keep up with typing. The algorithm is a
 * plain longest-common-subsequence over lines — guidance files are tens of lines, so the quadratic
 * table is free, and the alternative is a dependency for something worth forty lines.
 */
export function GuidanceDiff({ before, after }: { before: string; after: string }) {
  const rows = useMemo(() => diffLines(before, after), [before, after])
  const added = rows.filter((r) => r.kind === 'add').length
  const removed = rows.filter((r) => r.kind === 'del').length

  if (added === 0 && removed === 0) {
    return (
      <p className="px-3 py-2 text-sm text-muted italic">
        Identical to what is staged — nothing to publish.
      </p>
    )
  }

  return (
    <div>
      <p className="mb-2 text-xs text-muted">
        <span className="text-good">+{added}</span> <span className="text-bad">−{removed}</span>{' '}
        line{added + removed === 1 ? '' : 's'}
        {removed > added * 3 && removed > 5 && (
          <span className="ml-2 text-warn">
            ⚠ this removes far more than it adds — check nothing working was dropped
          </span>
        )}
      </p>
      <div className="overflow-x-auto rounded-lg border border-line bg-bg font-mono text-xs">
        {collapse(rows).map((row, i) =>
          row.kind === 'gap' ? (
            <div key={i} className="px-3 py-1 text-muted select-none">
              ⋯ {row.text}
            </div>
          ) : (
            <div key={i} className={`flex gap-2 px-3 ${TONE[row.kind]}`}>
              <span className="w-3 shrink-0 select-none opacity-60">{SIGN[row.kind]}</span>
              <span className="break-all whitespace-pre-wrap">{row.text || ' '}</span>
            </div>
          ),
        )}
      </div>
    </div>
  )
}

type Kind = 'add' | 'del' | 'same' | 'gap'
interface Row {
  kind: Kind
  text: string
}

const TONE: Record<Kind, string> = {
  add: 'bg-good/10 text-good',
  del: 'bg-bad/10 text-bad',
  same: 'text-muted',
  gap: '',
}

const SIGN: Record<Kind, string> = { add: '+', del: '−', same: ' ', gap: '' }

/** Unchanged lines kept around each change, so a hunk reads in context. */
const CONTEXT = 2

export function diffLines(before: string, after: string): Row[] {
  const a = before.split('\n')
  const b = after.split('\n')

  // LCS lengths, built bottom-up so the walk below can emit in source order.
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  )
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i]![j] =
        a[i] === b[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!)
    }
  }

  const rows: Row[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      rows.push({ kind: 'same', text: a[i]! })
      i++
      j++
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      rows.push({ kind: 'del', text: a[i]! })
      i++
    } else {
      rows.push({ kind: 'add', text: b[j]! })
      j++
    }
  }
  while (i < a.length) rows.push({ kind: 'del', text: a[i++]! })
  while (j < b.length) rows.push({ kind: 'add', text: b[j++]! })
  return rows
}

/** Drop long runs of unchanged lines, keeping a little context around each change. */
function collapse(rows: Row[]): Row[] {
  const keep = new Set<number>()
  rows.forEach((row, i) => {
    if (row.kind === 'same') return
    for (let k = Math.max(0, i - CONTEXT); k <= Math.min(rows.length - 1, i + CONTEXT); k++) {
      keep.add(k)
    }
  })

  const out: Row[] = []
  let hidden = 0
  rows.forEach((row, i) => {
    if (keep.has(i)) {
      if (hidden > 0) {
        out.push({ kind: 'gap', text: `${hidden} unchanged line${hidden === 1 ? '' : 's'}` })
        hidden = 0
      }
      out.push(row)
    } else {
      hidden++
    }
  })
  if (hidden > 0) {
    out.push({ kind: 'gap', text: `${hidden} unchanged line${hidden === 1 ? '' : 's'}` })
  }
  return out
}
