import { describe, expect, it } from 'vitest'
import { draftSummary, patchFilename } from './ImproveWorkspace'

/**
 * An improve produces two things and the panel counted one of them.
 *
 * A claim is deliberately not in the diff — it is a patch against a repository the console holds no
 * credentials for — so "a change to 1 file" was true and read as the whole output. On a real run
 * the drafter filed a claim and rewrote `SKILL.md`, and the operator read the diff, saw one file,
 * and asked why nothing had been routed. It had been; the sentence did not say so.
 */
describe('draftSummary', () => {
  it('counts files alone when nothing was routed', () => {
    expect(draftSummary(1, 0)).toBe('a change to 1 file')
    expect(draftSummary(2, 0)).toBe('a change to 2 files')
  })

  it('names the claims alongside the files, because they are not in the diff', () => {
    expect(draftSummary(1, 1)).toBe('a change to 1 file, and 1 claim beside the code')
    expect(draftSummary(2, 3)).toBe('a change to 2 files, and 3 claims beside the code')
  })

  it('says so when every lesson went to the code and the guidance did not move', () => {
    // The best outcome this loop has, and the one that used to read as an empty draft.
    expect(draftSummary(0, 1)).toBe('1 claim beside the code and no change to the guidance')
  })
})

describe('patchFilename', () => {
  it('flattens the target path so the download survives a filesystem', () => {
    expect(patchFilename('payments/gateway/.agents/context.md')).toBe(
      'payments_gateway_.agents_context.patch',
    )
  })

  it('handles a windows-shaped path the same way', () => {
    expect(patchFilename('payments\\.agents\\arch.md')).toBe('payments_.agents_arch.patch')
  })
})
