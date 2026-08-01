import { describe, expect, it } from 'vitest'
import { draftedFiles } from './ImproveWorkspace'

/**
 * A skill is a folder, and the improve step edits it as one — so the draft review has to show every
 * file it touched, not just `SKILL.md`. The failure this guards is silent in the worst way: a panel
 * that rendered only the body would drop a rule the drafter fixed in `references/x.md`, and the
 * operator would click Apply believing they had read the whole change.
 */
const BASELINE = {
  body: '# Rules\n\nThe list is in ./references/http.md.\n',
  pages: {
    'references/http.md': '- R-503 client errors are not logged at ERROR.\n',
    'references/ledger.md': '- R-202 writes go through LedgerService.\n',
  },
}

describe('draftedFiles', () => {
  it('lists SKILL.md alone when only the body moved', () => {
    const files = draftedFiles({ body: '# Rules\n\nRewritten.\n', pages: {} }, BASELINE)

    expect(files.map((f) => f.path)).toEqual(['SKILL.md'])
  })

  it('lists every companion page the drafter rewrote, alongside the body', () => {
    const files = draftedFiles(
      {
        body: '# Rules\n\nRewritten.\n',
        pages: {
          'references/ledger.md': '- R-202 sharpened.\n',
          'references/http.md': '- R-503 sharpened.\n',
        },
      },
      BASELINE,
    )

    expect(files.map((f) => f.path)).toEqual([
      'SKILL.md',
      'references/http.md',
      'references/ledger.md',
    ])
  })

  it('shows a page rewrite even when the body did not change at all', () => {
    // The commonest shape for a folder-shaped skill: the rule lives in a page, so that is where it
    // gets fixed. Keying the panel off the body would show "no change" for a real edit.
    const files = draftedFiles(
      { body: BASELINE.body, pages: { 'references/http.md': '- R-503 sharpened.\n' } },
      BASELINE,
    )

    expect(files.map((f) => f.path)).toEqual(['references/http.md'])
  })

  it('diffs each page against its own on-disk text, not against the body', () => {
    const files = draftedFiles(
      { body: BASELINE.body, pages: { 'references/ledger.md': '- R-202 sharpened.\n' } },
      BASELINE,
    )

    expect(files).toHaveLength(1)
    expect(files[0]?.before).toBe(BASELINE.pages['references/ledger.md'])
    expect(files[0]?.after).toBe('- R-202 sharpened.\n')
  })

  it('treats a page the skill does not have yet as an addition rather than crashing', () => {
    const files = draftedFiles({ body: BASELINE.body, pages: { 'references/new.md': 'x\n' } }, BASELINE)

    expect(files[0]?.before).toBe('')
  })

  it('reports nothing when the draft changes nothing, so Apply can be disabled', () => {
    expect(draftedFiles({ body: BASELINE.body, pages: {} }, BASELINE)).toEqual([])
  })

  it('ignores whitespace-only movement in the body', () => {
    const files = draftedFiles({ body: `\n${BASELINE.body}  `, pages: {} }, BASELINE)

    expect(files).toEqual([])
  })

  it('orders files stably, so a re-render does not reshuffle what you are reading', () => {
    const pages = { 'z.md': 'z', 'a.md': 'a', 'm.md': 'm' }
    const one = draftedFiles({ body: BASELINE.body, pages }, BASELINE)
    const shuffled = { 'm.md': 'm', 'z.md': 'z', 'a.md': 'a' }
    const two = draftedFiles({ body: BASELINE.body, pages: shuffled }, BASELINE)

    expect(one.map((f) => f.path)).toEqual(['a.md', 'm.md', 'z.md'])
    expect(two.map((f) => f.path)).toEqual(one.map((f) => f.path))
  })

  it('still lists what was written after apply, when disk has moved on', () => {
    // The trap behind snapshotting `Draft.baseline`. Applying invalidates the skill, so the live
    // on-disk query refetches to the *new* text — and a diff computed against that shows nothing
    // changed. The review would go blank at the moment it becomes the record of what was written.
    const draft = { body: '# Rules\n\nRewritten.\n', pages: {} }
    const afterWrite = { body: draft.body, pages: BASELINE.pages }

    expect(draftedFiles(draft, BASELINE).map((f) => f.path)).toEqual(['SKILL.md'])
    expect(draftedFiles(draft, afterWrite)).toEqual([])
  })
})
