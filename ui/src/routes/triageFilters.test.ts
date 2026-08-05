import { describe, expect, it } from 'vitest'
import type { QueueItem } from '@/api/client'
import {
  NONE,
  NO_FILTER,
  applyFilters,
  commentersOf,
  facets,
  fromParams,
  mergeRequestOf,
  mrOf,
  offersChoice,
  openedBy,
  peopleOf,
  selectedIndex,
  stateOf,
  toParams,
  toggle,
  type TriageFilter,
} from './triageFilters'

type Opts = {
  ref?: string | null
  author?: string
  commenters?: string[]
  skill?: string | null
  kind?: 'should_catch' | 'should_not_flag'
  signal?: string
  title?: string
  state?: string
}

function item(id: string, opts: Opts = {}): QueueItem {
  return {
    entry: {
      candidate: {
        id,
        kind: opts.kind ?? 'should_catch',
        provenance: {
          source: 'gitlab_mr',
          ref: opts.ref === undefined ? 'acme/payments!812' : opts.ref,
          human_signal: opts.signal ?? 'suggestion applied',
        },
        suggested_skill: opts.skill === undefined ? 'rust-errors' : opts.skill,
        discussion: {
          mr_author: opts.author ?? '',
          mr_state: opts.state ?? '',
          mr_title: opts.title ?? '',
          comments: (opts.commenters ?? []).map((author) => ({ author, body: '…' })),
        },
      },
    },
  } as unknown as QueueItem
}

function filter(over: Partial<TriageFilter> = {}): TriageFilter {
  return { ...NO_FILTER, ...over }
}

const ids = (items: QueueItem[]) => items.map((i) => i.entry.candidate.id)

describe('mrOf', () => {
  it('reads a note ref and the merge request ref as one merge request', () => {
    // The suffix is where in the conversation, not which merge request. Two buckets here means
    // "go to !812" shows you half of it.
    expect(mrOf(item('a', { ref: 'acme/payments!812#note_44' }).entry.candidate)).toBe(
      'acme/payments!812',
    )
    expect(mrOf(item('b', { ref: 'acme/payments!812' }).entry.candidate)).toBe('acme/payments!812')
  })

  it('buckets a source that is not a merge request by whatever it does have', () => {
    // A Jira key, a review, a synthetic parent case — the bucket is "the thing this came from".
    expect(mrOf(item('a', { ref: 'HUB-45814' }).entry.candidate)).toBe('HUB-45814')
  })

  it('has a bucket for a candidate with no reference at all', () => {
    expect(mrOf(item('a', { ref: null }).entry.candidate)).toBe(NONE)
  })

  it('reduces a bare ref the same way, for the drift report that links here', () => {
    // Drift groups the recent stream by the *full* ref, so an uncovered row can name a note.
    // Linking that straight into `?mr=` would scope the queue to a value no bucket ever equals.
    expect(mergeRequestOf('acme/payments!812#note_44')).toBe('acme/payments!812')
    expect(mergeRequestOf('acme/payments!812')).toBe('acme/payments!812')
    expect(mergeRequestOf('')).toBe(NONE)
  })
})

describe('people on a candidate', () => {
  it('separates who opened it from who commented on it', () => {
    const candidate = item('a', { author: 'dana', commenters: ['alice', 'bo'] }).entry.candidate
    expect(openedBy(candidate)).toBe('dana')
    expect(commentersOf(candidate)).toEqual(['alice', 'bo'])
  })

  it('counts a person who commented twice once', () => {
    const candidate = item('a', { commenters: ['alice', 'bo', 'alice'] }).entry.candidate
    expect(commentersOf(candidate)).toEqual(['alice', 'bo'])
  })

  it('unions both roles under "any"', () => {
    const candidate = item('a', { author: 'dana', commenters: ['alice'] }).entry.candidate
    expect(peopleOf(candidate, 'any').sort()).toEqual(['alice', 'dana'])
  })

  it('does not call a candidate unattributable because one half of it is missing', () => {
    // The claim that decides what the unknown bucket means. A candidate mined before authors were
    // carried, which Alice argued about, is Alice's — filing it under "unknown" as well would turn
    // that bucket into "has a gap somewhere" instead of "there is nobody here to find".
    const candidate = item('a', { author: '', commenters: ['alice'] }).entry.candidate
    expect(peopleOf(candidate, 'any')).toEqual(['alice'])
  })

  it('is unattributable only when nobody at all is on it', () => {
    const candidate = item('a', { author: '', commenters: [] }).entry.candidate
    expect(peopleOf(candidate, 'any')).toEqual([NONE])
    expect(peopleOf(candidate, 'opened')).toEqual([NONE])
    expect(peopleOf(candidate, 'commented')).toEqual([NONE])
  })
})

describe('applyFilters', () => {
  const queue = [
    item('one', { ref: 'acme/payments!812#note_1', author: 'dana', commenters: ['alice'] }),
    item('two', { ref: 'acme/payments!812', author: 'dana', commenters: ['bo'] }),
    item('three', { ref: 'acme/billing!91', author: 'alice', commenters: [] }),
  ]

  it('returns the queue untouched when nothing is picked', () => {
    // Including the order, which is the server's — strongest signal first — and is not this
    // module's to re-decide.
    expect(applyFilters(queue, NO_FILTER)).toBe(queue)
  })

  it('narrows to one merge request, note refs and all', () => {
    expect(ids(applyFilters(queue, filter({ mrs: ['acme/payments!812'] })))).toEqual(['one', 'two'])
  })

  it('takes more than one merge request at a time', () => {
    const picked = filter({ mrs: ['acme/billing!91', 'acme/payments!812'] })
    expect(ids(applyFilters(queue, picked))).toEqual(['one', 'two', 'three'])
  })

  it('matches a person who opened it or commented on it', () => {
    // alice opened !91 and commented on one of the !812 candidates. Both are hers.
    expect(ids(applyFilters(queue, filter({ people: ['alice'] })))).toEqual(['one', 'three'])
  })

  it('narrows to what a person actually commented on', () => {
    expect(ids(applyFilters(queue, filter({ people: ['alice'], role: 'commented' })))).toEqual([
      'one',
    ])
  })

  it('narrows to what a person opened', () => {
    expect(ids(applyFilters(queue, filter({ people: ['alice'], role: 'opened' })))).toEqual([
      'three',
    ])
  })

  it('finds the candidates nobody can be attributed to', () => {
    const orphan = item('orphan', { ref: 'acme/x!1', author: '', commenters: [] })
    expect(ids(applyFilters([...queue, orphan], filter({ people: [NONE] })))).toEqual(['orphan'])
  })

  it('intersects dimensions rather than adding them up', () => {
    const picked = filter({ mrs: ['acme/payments!812'], people: ['bo'] })
    expect(ids(applyFilters(queue, picked))).toEqual(['two'])
  })

  it('still hides a signal, which is the one dimension that excludes', () => {
    const noisy = [
      item('quiet', { signal: 'merged clean' }),
      item('loud', { signal: 'suggestion applied' }),
    ]
    expect(ids(applyFilters(noisy, filter({ hiddenSignals: ['merged clean'] })))).toEqual(['loud'])
  })

  it('separates a routed candidate from an unrouted one', () => {
    const mixed = [item('routed', { skill: 'rust-errors' }), item('loose', { skill: null })]
    expect(ids(applyFilters(mixed, filter({ skills: [NONE] })))).toEqual(['loose'])
  })

  it('narrows by kind', () => {
    const mixed = [item('catch'), item('quiet', { kind: 'should_not_flag' })]
    expect(ids(applyFilters(mixed, filter({ kinds: ['should_not_flag'] })))).toEqual(['quiet'])
  })
})

describe('facets', () => {
  const queue = [
    item('one', {
      ref: 'acme/payments!812',
      author: 'dana',
      commenters: ['alice'],
      title: 'retry',
    }),
    item('two', { ref: 'acme/payments!812', author: 'dana', commenters: ['bo'] }),
    item('three', { ref: 'acme/billing!91', author: 'alice', commenters: [] }),
  ]

  it('counts a facet against every filter except its own', () => {
    // The claim that makes the bar usable: after picking !812, the other merge requests must still
    // show what picking them instead would leave. Counting against the filtered queue would read 0
    // beside every unpicked chip — a bar that tells you everything else is empty.
    const picked = filter({ mrs: ['acme/payments!812'] })
    const mrs = facets(queue, picked).mrs
    expect(mrs.map((o) => [o.value, o.count])).toEqual([
      ['acme/payments!812', 2],
      ['acme/billing!91', 1],
    ])
  })

  it('drops a facet value the other dimensions have emptied', () => {
    // Pick dana and the merge request list becomes dana's merge requests. Listing billing!91 at
    // zero would be offering a click that leads nowhere.
    const picked = filter({ people: ['dana'], role: 'opened' })
    const mrs = facets(queue, picked).mrs
    expect(mrs.map((o) => [o.value, o.count])).toEqual([['acme/payments!812', 2]])
  })

  it('keeps a value that is picked, even once it leads nowhere', () => {
    // The exception, and the reason the rule above is safe: a selected value that vanished would
    // take the only control for unselecting it, leaving an empty queue and nothing to undo.
    const picked = filter({ mrs: ['acme/billing!91'], people: ['dana'], role: 'opened' })
    const mrs = facets(queue, picked).mrs
    expect(mrs.find((o) => o.value === 'acme/billing!91')?.count).toBe(0)
    expect(mrs.find((o) => o.value === 'acme/payments!812')?.count).toBe(2)
  })

  it('labels a kept-but-emptied bucket the way the rest of its facet is labelled', () => {
    // `*none*` is a sentinel for the query string, never a thing to show a person. The menu prints
    // its own word for the empty label — "unrouted", "unattributed" — and a row re-added by
    // `keeping` was arriving with the sentinel as its label, so it read `*none*` on screen.
    const mixed = [item('loose', { skill: null }), item('routed', { skill: 'rust-errors' })]
    const picked = filter({ skills: [NONE], kinds: ['should_not_flag'] })
    expect(facets(mixed, picked).skills.find((o) => o.value === NONE)).toMatchObject({
      count: 0,
      label: '',
    })
  })

  it('keeps a picked name listed after the role stops reaching it', () => {
    const picked = filter({ people: ['dana'], role: 'commented' })
    const dana = facets(queue, picked).people.find((r) => r.value === 'dana')
    expect(dana).toMatchObject({ count: 0, opened: 2 })
  })

  it('carries the merge request title, so the list is readable', () => {
    const found = facets(queue, NO_FILTER).mrs.find((o) => o.value === 'acme/payments!812')
    expect(found?.detail).toBe('retry')
  })

  it('splits each name into what they commented on and what they opened', () => {
    const rows = facets(queue, NO_FILTER).people
    const alice = rows.find((r) => r.value === 'alice')
    expect(alice).toMatchObject({ commented: 1, opened: 1, count: 2 })
    const dana = rows.find((r) => r.value === 'dana')
    expect(dana).toMatchObject({ commented: 0, opened: 2, count: 2 })
  })

  it('counts a name by the role that is actually selected, so it cannot contradict the click', () => {
    const rows = facets(queue, filter({ role: 'commented' })).people
    expect(rows.find((r) => r.value === 'dana')?.count).toBe(0)
    expect(rows.find((r) => r.value === 'alice')?.count).toBe(1)
  })

  it('puts the most work first and the unattributable bucket last', () => {
    const orphan = item('orphan', { ref: null, author: '', commenters: [] })
    const rows = facets([...queue, orphan], NO_FILTER).people
    expect(rows.map((r) => r.value)).toEqual(['alice', 'dana', 'bo', NONE])
  })

  it('drops a kind nothing in the queue is, rather than offering an empty click', () => {
    expect(facets(queue, NO_FILTER).kinds.map((k) => k.value)).toEqual(['should_catch'])
  })

  it('orders signals by the builder confidence order, because that row is also the legend', () => {
    const mixed = [
      item('a', { signal: 'merged clean' }),
      item('b', { signal: 'escaped defect' }),
      item('c', { signal: 'hand rolled' }),
    ]
    expect(facets(mixed, NO_FILTER).signals.map((s) => s.value)).toEqual([
      'escaped defect',
      'merged clean',
      'hand rolled',
    ])
  })

  it('counts a hidden signal as what unhiding it would restore', () => {
    const mixed = [item('a', { signal: 'merged clean' }), item('b', { signal: 'merged clean' })]
    const rows = facets(mixed, filter({ hiddenSignals: ['merged clean'] })).signals
    expect(rows.find((s) => s.value === 'merged clean')?.count).toBe(2)
  })
})

describe('merge request state', () => {
  const queue = [
    item('live', { ref: 'acme/p!900', state: 'opened', commenters: ['alice'] }),
    item('landed', { ref: 'acme/p!812', state: 'merged', commenters: ['alice'] }),
    item('legacy', { ref: 'acme/p!700', state: '', commenters: ['alice'] }),
  ]

  it('reads the state off the candidate', () => {
    expect(stateOf(queue[0]!)).toBe('opened')
    expect(stateOf(queue[1]!)).toBe('merged')
  })

  it('does not call an unrecorded state merged', () => {
    // Every candidate mined before the walk could reach an open branch did come from a merged one,
    // but saying so here would put a fact in the bar that nothing checked — on exactly the rows a
    // re-pull is about to correct.
    expect(stateOf(queue[2]!)).toBe(NONE)
  })

  it('narrows to what is still being reviewed', () => {
    expect(ids(applyFilters(queue, filter({ states: ['opened'] })))).toEqual(['live'])
  })

  it('narrows to what has landed', () => {
    expect(ids(applyFilters(queue, filter({ states: ['merged'] })))).toEqual(['landed'])
  })

  it('can single out the ones a re-pull has not reached', () => {
    expect(ids(applyFilters(queue, filter({ states: [NONE] })))).toEqual(['legacy'])
  })

  it('offers open before merged, and unrecorded last', () => {
    // The order the queue is worked in: a live branch is where saying something still changes the
    // outcome.
    expect(facets(queue, NO_FILTER).states.map((o) => o.value)).toEqual(['opened', 'merged', NONE])
  })

  it('survives the query string', () => {
    const picked = filter({ states: ['opened', NONE] })
    expect(fromParams(toParams(picked)).states).toEqual(['opened', NONE])
  })
})

describe('a facet that is down to one value', () => {
  const queue = [
    item('one', { ref: 'acme/payments!812#note_1', author: 'dana', commenters: ['alice'] }),
    item('two', { ref: 'acme/payments!812', author: 'dana', commenters: ['bo'] }),
    item('three', { ref: 'acme/billing!91', author: 'alice', commenters: [] }),
  ]
  const picked = filter({ mrs: ['acme/payments!812'], people: ['bo'], role: 'commented' })

  it('really does collapse to the one value picked', () => {
    // bo commented on nothing outside !812, so relaxing the merge request dimension still leaves
    // exactly one — the one already picked.
    expect(facets(queue, picked).mrs.map((o) => o.value)).toEqual(['acme/payments!812'])
  })

  it('stays on screen anyway, because it is still filtering', () => {
    // The failure this guards: the picked chip and its × live inside the facet's own menu, so a
    // menu hidden for having nothing to choose between takes the only control for undoing itself.
    // That is exactly what `keeping` exists to prevent, reintroduced one layer up.
    expect(offersChoice(facets(queue, picked).mrs, picked.mrs)).toBe(true)
  })

  it('is hidden when it is not filtering, because there is nothing to choose', () => {
    const single = [item('one'), item('two')] // both from the same merge request
    expect(offersChoice(facets(single, NO_FILTER).mrs, [])).toBe(false)
  })
})

describe('the query string', () => {
  it('round-trips every dimension', () => {
    const picked = filter({
      mrs: ['acme/payments!812'],
      people: ['alice', 'dana'],
      role: 'commented',
      skills: ['rust-errors'],
      kinds: ['should_catch'],
      hiddenSignals: ['merged clean'],
    })
    expect(fromParams(toParams(picked))).toEqual(picked)
  })

  it('keeps what else the url is carrying', () => {
    // `focus` is how a link lands on one candidate inside a filtered queue.
    const base = new URLSearchParams({ focus: 'cand-7' })
    const params = toParams(filter({ mrs: ['acme/payments!812'] }), base)
    expect(params.get('focus')).toBe('cand-7')
    expect(params.get('mr')).toBe('acme/payments!812')
  })

  it('drops the previous filter rather than appending to it', () => {
    const base = toParams(filter({ mrs: ['old!1'], people: ['bo'] }))
    const params = toParams(filter({ mrs: ['new!2'] }), base)
    expect(params.getAll('mr')).toEqual(['new!2'])
    expect(params.getAll('who')).toEqual([])
  })

  it('leaves no role behind when nobody is picked', () => {
    // Otherwise it survives every later edit of the query string as an instruction about nothing.
    expect(toParams(filter({ role: 'opened' })).get('role')).toBeNull()
  })

  it('ignores a role nobody wrote', () => {
    expect(fromParams(new URLSearchParams('role=badger')).role).toBe('any')
  })

  it('reads an empty url as no filter', () => {
    expect(fromParams(new URLSearchParams())).toEqual(NO_FILTER)
  })
})

describe('selectedIndex', () => {
  const queue = [item('one'), item('two'), item('three')]

  it('follows the candidate rather than its position', () => {
    expect(selectedIndex(queue, 'three')).toBe(2)
    expect(selectedIndex(queue.slice(1), 'three')).toBe(1)
  })

  it('falls to the top when the selection is filtered out', () => {
    // The honest answer. Keeping the index would leave the cursor on a different candidate with
    // nothing saying so.
    expect(selectedIndex(queue, 'gone')).toBe(0)
  })
})

describe('toggle', () => {
  it('adds and removes', () => {
    expect(toggle(['a'], 'b')).toEqual(['a', 'b'])
    expect(toggle(['a', 'b'], 'a')).toEqual(['b'])
  })
})
