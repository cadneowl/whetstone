import { describe, expect, it } from 'vitest'
import type { ReviewListItem } from '@/api/client'
import { groupBySkill } from './ReviewsIndex'

function item(
  skillId: string,
  id: string,
  pending: number,
  extra: { stale_skill?: boolean; skill_known?: boolean } = {},
): ReviewListItem {
  return {
    summary: { id, skill_id: skillId, pending },
    stale_skill: false,
    skill_known: true,
    ...extra,
  } as ReviewListItem
}

/**
 * The cross-skill queue groups by skill because the group is the unit of work: ruling six findings
 * for one skill and then improving it is one sitting, and the same six spread over four skills is
 * four. The ordering claim — work waiting on a human before settled history — is the screen's whole
 * argument, so it is tested rather than left to the eye.
 */
describe('groupBySkill', () => {
  it('puts the skill with the most unruled findings first', () => {
    const groups = groupBySkill([
      item('quiet', 'r1', 0),
      item('busy', 'r2', 3),
      item('busy', 'r3', 2),
    ])

    expect(groups.map((g) => g.skillId)).toEqual(['busy', 'quiet'])
    expect(groups[0]!.pending).toBe(5)
    expect(groups[0]!.items).toHaveLength(2)
  })

  it('breaks a tie by name, so the list does not reshuffle between loads', () => {
    const groups = groupBySkill([item('b', 'r1', 1), item('a', 'r2', 1)])
    expect(groups.map((g) => g.skillId)).toEqual(['a', 'b'])
  })

  it('keeps the server ordering inside a group', () => {
    // The API returns newest first, and re-sorting here would silently contradict every other
    // review list in the console.
    const groups = groupBySkill([item('s', 'newest', 1), item('s', 'oldest', 1)])
    expect(groups[0]!.items.map((i) => i.summary.id)).toEqual(['newest', 'oldest'])
  })

  it('is empty for no reviews rather than inventing a group', () => {
    expect(groupBySkill([])).toEqual([])
  })

  it('does not count a stale review as work waiting on a human', () => {
    // Its findings describe guidance that is gone, so no ruling can finish them — counting them
    // here would sort a skill to the top of the queue for work nobody can do.
    const groups = groupBySkill([
      item('moved-on', 'r1', 4, { stale_skill: true }),
      item('live', 'r2', 1),
    ])

    expect(groups.map((g) => g.skillId)).toEqual(['live', 'moved-on'])
    expect(groups[1]!.pending).toBe(0)
    expect(groups[1]!.expired).toBe(1)
    // Still listed. The record is real evidence and the re-run starts from it.
    expect(groups[1]!.items).toHaveLength(1)
  })

  it('marks a group whose skill has left the registry, so the header stops linking', () => {
    const groups = groupBySkill([item('ghost', 'r1', 1, { skill_known: false })])
    expect(groups[0]!.known).toBe(false)
  })
})
