import { describe, expect, it } from 'vitest'
import type { GraphNode } from '@/api/client'
import { clampHops, crumbsFor, focusQuery, folderOf, parentOf, parentQuery } from './graphNav'

function node(partial: Partial<GraphNode> & Pick<GraphNode, 'kind'>): GraphNode {
  return {
    id: 'x',
    label: '',
    path: '',
    text: '',
    sidecar: '',
    section: '',
    line: 0,
    status: '',
    excepts: '',
    cited: true,
    claims: 0,
    confirmed: 0,
    contradicted: 0,
    evidence: '',
    missing: false,
    degree: 0,
    ...partial,
  } as GraphNode
}

describe('folderOf', () => {
  it('reads a position back out of the query, so typing and clicking agree', () => {
    expect(folderOf('folder:payments/gateway')).toBe('payments/gateway')
    expect(folderOf('kind:claim folder:payments uncited:true')).toBe('payments')
  })

  it('is null when the query names no folder', () => {
    expect(folderOf('')).toBeNull()
    expect(folderOf('rule:R1')).toBeNull()
    // Not a `folder:` term — a substring that merely contains the word.
    expect(folderOf('subfolder:payments')).toBeNull()
  })
})

describe('parentOf', () => {
  it('walks one level up, and reaches the whole tree', () => {
    expect(parentOf('payments/gateway')).toBe('payments')
    expect(parentOf('payments')).toBe('')
  })

  it('distinguishes "the whole tree" from "nowhere to go"', () => {
    // '' is a position; null means this node is not in the tree at all — a rule or a reference.
    expect(parentOf('payments')).toBe('')
    expect(parentOf('')).toBeNull()
    expect(parentOf('.')).toBeNull()
  })

  it('round-trips through a query', () => {
    expect(parentQuery('payments/gateway')).toBe('folder:payments')
    expect(parentQuery('payments')).toBe('')
    expect(folderOf(parentQuery('a/b/c'))).toBe('a/b')
  })
})

describe('focusQuery', () => {
  it('centres a folder or file on its subtree', () => {
    expect(focusQuery(node({ kind: 'folder', path: 'payments' }))).toBe('folder:payments')
    expect(focusQuery(node({ kind: 'file', path: 'payments/stripe.py' }))).toBe(
      'folder:payments/stripe.py',
    )
  })

  it('centres a claim on its folder, because its text is a sentence and not a handle', () => {
    const claim = node({ kind: 'claim', path: 'payments', label: 'The ledger is append-only.' })
    expect(focusQuery(claim)).toBe('folder:payments')
  })

  it('centres a rule on the claims that except it', () => {
    expect(focusQuery(node({ kind: 'rule', label: 'R7' }))).toBe('excepts:R7')
  })

  it('centres a reference on everything citing it', () => {
    expect(focusQuery(node({ kind: 'ref', label: 'ADR-22' }))).toBe('ADR-22')
  })

  it('always produces a query the box could have been typed into', () => {
    // The whole point of one language for typed, clicked and breadcrumbed positions.
    for (const kind of ['folder', 'file', 'claim', 'rule', 'ref'] as const) {
      const out = focusQuery(node({ kind, path: 'a/b', label: 'L' }))
      expect(out.length).toBeGreaterThan(0)
      expect(out).not.toContain('\n')
    }
  })
})

describe('crumbsFor', () => {
  it('gives the segments of the current folder', () => {
    expect(crumbsFor('folder:payments/gateway', null)).toEqual(['payments', 'gateway'])
  })

  it('falls back to the selected node when the query names no folder', () => {
    expect(crumbsFor('', 'payments/reconciliation')).toEqual(['payments', 'reconciliation'])
  })

  it('is empty — not absent — at the root, so the crumb bar still renders', () => {
    expect(crumbsFor('', '')).toEqual([])
    expect(crumbsFor('', '.')).toEqual([])
  })

  it('is null when there is no position to show', () => {
    expect(crumbsFor('', null)).toBeNull()
    expect(crumbsFor('rule:R1', null)).toBeNull()
  })
})

describe('clampHops', () => {
  it('defaults to 1 for anything the URL could carry', () => {
    expect(clampHops(null)).toBe(1)
    expect(clampHops('')).toBe(1)
    expect(clampHops('nonsense')).toBe(1)
  })

  it('bounds what a hand-edited URL can ask for', () => {
    // The server clamps too; this keeps the <select> from rendering a value it has no option for.
    expect(clampHops('99')).toBe(3)
    expect(clampHops('-4')).toBe(0)
    expect(clampHops('2')).toBe(2)
    expect(clampHops('2.7')).toBe(2)
  })
})
