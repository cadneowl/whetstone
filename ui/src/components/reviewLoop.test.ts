import { describe, expect, it } from 'vitest'

import { improveLink } from './reviews'

/**
 * The link that closes the loop: a case minted from a review has to arrive in the improve
 * workspace *already selected*, or the operator is back to hunting for it among everything else
 * promoted. `ImproveWorkspace.selectionFrom` reads this `cases` param, so the two must agree about
 * the separator and about what "no ids" means.
 */
describe('improveLink', () => {
  it('lands on the improve tab with the minted cases ticked', () => {
    expect(improveLink('rust-errors', ['a', 'b'])).toBe('/skills/rust-errors?tab=improve&cases=a,b')
  })

  it('omits the param entirely when there are no ids', () => {
    // Not `cases=`, which the workspace reads as a deliberately empty selection. Absent means
    // "everything", which is the honest state for a link that names nothing in particular.
    expect(improveLink('rust-errors')).toBe('/skills/rust-errors?tab=improve')
    expect(improveLink('rust-errors', [])).toBe('/skills/rust-errors?tab=improve')
  })

  it('encodes a skill id and case ids that need it', () => {
    expect(improveLink('a/b', ['c d'])).toBe('/skills/a%2Fb?tab=improve&cases=c%20d')
  })
})
