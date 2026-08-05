import type { CandidateCase, QueueItem } from '@/api/client'
import { SIGNALS } from '@/components/signals'

/**
 * Narrowing the triage queue to the work in front of you.
 *
 * A queue mined from a month of merge requests is worked by *person* and by *merge request* at
 * least as often as by subject — "the MRs I opened", "everything Dana commented on", "finish !812"
 * — and none of those were reachable except by scrolling. The whole of this file is the answer to
 * that, kept out of the component so the claims below are testable rather than eyeballed.
 *
 * Two vocabularies, deliberately not merged. The facets here **include**: nothing picked means
 * everything, and picking narrows. The signal chips that were already on the screen **exclude**,
 * because the thing that made them worth building was one signal flooding the queue (see
 * `SignalFilter`), and inverting that would cost nine clicks to express. They are labelled as what
 * they each are rather than forced into one shape.
 */

/**
 * The bucket for a candidate nobody can be attributed to, and for one routed to no skill.
 *
 * A sentinel rather than the empty string, because these values go through the query string, where
 * an empty parameter is indistinguishable from an absent one. Asterisks appear in no forge username
 * and no skill id, so this cannot collide with a real one.
 */
export const NONE = '*none*'

export type PersonRole = 'any' | 'commented' | 'opened'

export type TriageFilter = {
  /** Merge requests to show, by `mrOf`. Empty means every one. */
  mrs: string[]
  /** Usernames to show. Empty means everyone. */
  people: string[]
  /** Which relationship a username has to have to the candidate. */
  role: PersonRole
  /** Target skills to show, `NONE` for unrouted. Empty means all. */
  skills: string[]
  /** Kinds to show. Empty means both. */
  kinds: string[]
  /** Merge request states to show — `opened`, `merged`, `NONE` for unrecorded. Empty means all. */
  states: string[]
  /** Signals to **hide** — the one exclusive dimension, and the pre-existing one. */
  hiddenSignals: string[]
}

export const NO_FILTER: TriageFilter = {
  mrs: [],
  people: [],
  role: 'any',
  skills: [],
  kinds: [],
  states: [],
  hiddenSignals: [],
}

export function isFiltered(filter: TriageFilter): boolean {
  return (
    filter.mrs.length > 0 ||
    filter.people.length > 0 ||
    filter.skills.length > 0 ||
    filter.kinds.length > 0 ||
    filter.states.length > 0 ||
    filter.hiddenSignals.length > 0
  )
}

/**
 * The merge request a candidate belongs to.
 *
 * A rule's ref points at a discussion note (`acme/payments!812#note_44`) and a case's at the merge
 * request (`acme/payments!812`); the suffix is *where in the conversation*, not which merge
 * request. Same reduction `deadrules._mr_of` makes on the Python side, and for the same reason —
 * two candidates from one MR must land in one bucket or "go to !812" shows you half of it.
 *
 * Sources with no merge request behind them group the same way and are none the worse for it: a
 * Jira key, a review id, a synthetic parent case. The bucket is "the thing this came from".
 */
export function mrOf(candidate: CandidateCase): string {
  return mergeRequestOf(candidate.provenance?.ref ?? '')
}

/**
 * The same reduction applied to a bare ref, for callers that hold one rather than a candidate.
 *
 * The drift report is the caller that matters: it groups the recent stream by the *full* ref, so
 * an uncovered row can name a note. Linking that straight into `?mr=` would scope the queue to a
 * value no candidate's bucket ever equals, and land you on an empty list.
 */
export function mergeRequestOf(ref: string): string {
  return ref.split('#')[0] || NONE
}

/** Who opened the merge request, or `NONE` — unknown, which is not the same as nobody. */
export function openedBy(candidate: CandidateCase): string {
  return candidate.discussion?.mr_author || NONE
}

/** Everyone who said something in the thread, in order, without repeats. */
export function commentersOf(candidate: CandidateCase): string[] {
  const seen: string[] = []
  for (const comment of candidate.discussion?.comments ?? []) {
    if (comment.author && !seen.includes(comment.author)) seen.push(comment.author)
  }
  return seen
}

/**
 * The usernames a candidate answers to, under one reading of "involved".
 *
 * `any` is the union of the two and falls back to `NONE` only when *nobody at all* is attributable
 * — not when one half is missing. A candidate whose author was never mined but which Dana argued
 * about is Dana's, and listing it under "unknown" as well would make that bucket mean "has a gap
 * somewhere" instead of "there is nobody here to find".
 */
export function peopleOf(candidate: CandidateCase, role: PersonRole): string[] {
  const opened = candidate.discussion?.mr_author || ''
  const commented = commentersOf(candidate)
  if (role === 'opened') return [opened || NONE]
  if (role === 'commented') return commented.length ? commented : [NONE]
  const union = [opened, ...commented].filter(Boolean)
  return union.length ? [...new Set(union)] : [NONE]
}

export function skillOf(item: QueueItem): string {
  return item.entry.candidate.suggested_skill || NONE
}

/**
 * Where the merge request stands — `opened`, `merged`, or unrecorded.
 *
 * Unrecorded is not the same as merged even though every candidate mined before the walk could
 * reach an open branch came from one. Asserting it here would put a fact in the bar that nothing
 * checked, on exactly the rows a re-pull is about to correct.
 */
export function stateOf(item: QueueItem): string {
  return item.entry.candidate.discussion?.mr_state || NONE
}

export function signalOf(item: QueueItem): string {
  return item.entry.candidate.provenance?.human_signal ?? ''
}

/**
 * One dimension of the filter: how to test a candidate against it, and how to switch it off.
 *
 * Named and separable because the facet counts need exactly that — the count beside `dana` must be
 * how many candidates picking `dana` would leave, which means measuring with the *person* dimension
 * disabled and every other one still applied. Computing it against the fully filtered queue makes
 * every unpicked chip read 0 the moment anything is picked, which is a filter bar that lies.
 */
type Dimension = keyof typeof DIMENSIONS

const DIMENSIONS = {
  mr: {
    off: { mrs: [] },
    holds: (item: QueueItem, f: TriageFilter) =>
      f.mrs.length === 0 || f.mrs.includes(mrOf(item.entry.candidate)),
  },
  person: {
    off: { people: [] },
    holds: (item: QueueItem, f: TriageFilter) =>
      f.people.length === 0 ||
      peopleOf(item.entry.candidate, f.role).some((who) => f.people.includes(who)),
  },
  skill: {
    off: { skills: [] },
    holds: (item: QueueItem, f: TriageFilter) =>
      f.skills.length === 0 || f.skills.includes(skillOf(item)),
  },
  kind: {
    off: { kinds: [] },
    holds: (item: QueueItem, f: TriageFilter) =>
      f.kinds.length === 0 || f.kinds.includes(item.entry.candidate.kind),
  },
  state: {
    off: { states: [] },
    holds: (item: QueueItem, f: TriageFilter) =>
      f.states.length === 0 || f.states.includes(stateOf(item)),
  },
  signal: {
    off: { hiddenSignals: [] },
    holds: (item: QueueItem, f: TriageFilter) => !f.hiddenSignals.includes(signalOf(item)),
  },
} satisfies Record<
  string,
  { off: Partial<TriageFilter>; holds: (i: QueueItem, f: TriageFilter) => boolean }
>

export function matches(item: QueueItem, filter: TriageFilter): boolean {
  return Object.values(DIMENSIONS).every((d) => d.holds(item, filter))
}

/** The queue, narrowed. Order is the server's — confidence first — and is never re-sorted here. */
export function applyFilters(items: QueueItem[], filter: TriageFilter): QueueItem[] {
  return isFiltered(filter) ? items.filter((item) => matches(item, filter)) : items
}

/** Everything except one dimension, which is what that dimension's own counts are measured over. */
function without(items: QueueItem[], filter: TriageFilter, dimension: Dimension): QueueItem[] {
  const relaxed = { ...filter, ...DIMENSIONS[dimension].off }
  return items.filter((item) => matches(item, relaxed))
}

export type FacetOption = {
  value: string
  label: string
  /** The second line — an MR's title, a signal's meaning. Never load-bearing. */
  detail?: string
  count: number
}

export type PersonOption = FacetOption & { commented: number; opened: number }

export type Facets = {
  mrs: FacetOption[]
  people: PersonOption[]
  skills: FacetOption[]
  kinds: FacetOption[]
  states: FacetOption[]
  signals: FacetOption[]
}

const KIND_LABEL: Record<string, string> = {
  should_catch: 'should catch',
  should_not_flag: 'should not flag',
}

/**
 * Every value present in the queue, with the number of candidates picking it would leave.
 *
 * Facets are built from the candidates themselves rather than from a fixed vocabulary, so a person
 * or a merge request cannot be missing from the bar while its candidates are in the list. The one
 * exception is the signal row, which follows the builder's confidence order where it can, because
 * that row doubles as the legend.
 */
export function facets(items: QueueItem[], filter: TriageFilter): Facets {
  const byKind = without(items, filter, 'kind')
  return {
    mrs: byCount(
      keeping(
        tally(without(items, filter, 'mr'), (item) => [mrOf(item.entry.candidate)], {
          detail: (item) => item.entry.candidate.discussion?.mr_title ?? '',
        }),
        filter.mrs,
      ),
    ),
    people: people(without(items, filter, 'person'), filter),
    skills: byCount(
      keeping(
        tally(without(items, filter, 'skill'), (item) => [skillOf(item)]),
        filter.skills,
      ),
    ),
    kinds: ['should_catch', 'should_not_flag']
      .map((kind) => ({
        value: kind,
        label: KIND_LABEL[kind] ?? kind,
        count: byKind.filter((i) => i.entry.candidate.kind === kind).length,
      }))
      .filter((option) => option.count > 0 || filter.kinds.includes(option.value)),
    // Open before merged, and unrecorded last: the ordering the queue is worked in, since a live
    // branch is the one where saying something still changes the outcome.
    states: byStateOrder(
      keeping(
        tally(without(items, filter, 'state'), (item) => [stateOf(item)], {
          detail: (item) => STATE_DETAIL[stateOf(item)] ?? '',
        }),
        filter.states,
      ),
    ),
    signals: bySignalOrder(
      keeping(
        tally(without(items, filter, 'signal'), (item) => [signalOf(item)]),
        filter.hiddenSignals,
      ),
    ),
  }
}

const STATE_ORDER = ['opened', 'merged']

const STATE_DETAIL: Record<string, string> = {
  opened: 'still being reviewed',
  merged: 'landed',
}

function byStateOrder(options: FacetOption[]): FacetOption[] {
  return options.sort(
    (a, b) =>
      Number(a.value === NONE) - Number(b.value === NONE) ||
      (STATE_ORDER.indexOf(a.value) + 1 || STATE_ORDER.length + 1) -
        (STATE_ORDER.indexOf(b.value) + 1 || STATE_ORDER.length + 1) ||
      a.value.localeCompare(b.value),
  )
}

/**
 * A value already picked stays on the list even when nothing is left behind it.
 *
 * Everything else drops out, which is the point of a facet bar — pick a person and the merge
 * request list becomes that person's merge requests, not a wall of zeroes. But a *selected* value
 * that vanishes takes the only control for unselecting it with it, leaving an empty queue and no
 * visible reason for it. So it stays, showing the 0 it has honestly earned.
 */
function keeping(options: FacetOption[], selected: string[]): FacetOption[] {
  const present = new Set(options.map((o) => o.value))
  return [
    ...options,
    // Labelled exactly as `tally` would have. `NONE` is a sentinel for the query string and never a
    // word to show anyone — the menu prints its own ("unrouted", "unattributed") when the label is
    // blank, so re-adding a row with the sentinel in it puts `*none*` on the screen.
    ...selected
      .filter((value) => !present.has(value))
      .map((value) => ({ value, label: value === NONE ? '' : value, count: 0 })),
  ]
}

/**
 * Whether a facet is worth putting on screen.
 *
 * One value and nothing picked is furniture: the control cannot change what you are looking at.
 * One value that *is* the picked one is the opposite — the chip that unpicks it lives inside this
 * menu, so hiding it strands the filter with no visible way back. That is the failure `keeping`
 * exists to prevent, and gating the menu on the option count alone reintroduced it one layer up.
 */
export function offersChoice(options: FacetOption[], selected: string[]): boolean {
  return options.length > 1 || selected.length > 0
}

function tally(
  items: QueueItem[],
  values: (item: QueueItem) => string[],
  extra: { detail?: (item: QueueItem) => string } = {},
): FacetOption[] {
  const counts = new Map<string, FacetOption>()
  for (const item of items) {
    for (const value of values(item)) {
      const option = counts.get(value)
      if (option) {
        option.count += 1
        // Filled from whichever candidate has it rather than only the first. Every candidate of a
        // merge request carries the same title, but a candidate minted some other way carries none,
        // and one of those arriving first would leave the row unlabelled for good.
        option.detail = option.detail || extra.detail?.(item) || undefined
      } else {
        counts.set(value, {
          value,
          label: value === NONE ? '' : value,
          detail: extra.detail?.(item) || undefined,
          count: 1,
        })
      }
    }
  }
  return [...counts.values()]
}

/** Most work first, ties by name, and the unattributable bucket always last. */
function byCount(options: FacetOption[]): FacetOption[] {
  return options.sort(
    (a, b) =>
      Number(a.value === NONE) - Number(b.value === NONE) ||
      b.count - a.count ||
      a.value.localeCompare(b.value),
  )
}

/**
 * Usernames with both counts, because the two are different questions asked of the same name.
 *
 * `count` is what the active role would actually leave, so the number beside a name never
 * contradicts what clicking it does — the split beside it says where that number comes from.
 */
function people(items: QueueItem[], filter: TriageFilter): PersonOption[] {
  const rows = new Map<string, PersonOption>()
  const row = (name: string) => {
    const existing = rows.get(name)
    if (existing) return existing
    const created: PersonOption = {
      value: name,
      label: name === NONE ? '' : name,
      count: 0,
      commented: 0,
      opened: 0,
    }
    rows.set(name, created)
    return created
  }
  for (const item of items) {
    const candidate = item.entry.candidate
    for (const who of new Set(commentersOf(candidate))) row(who).commented += 1
    const opened = candidate.discussion?.mr_author || ''
    if (opened) row(opened).opened += 1
    for (const who of peopleOf(candidate, filter.role)) row(who).count += 1
  }
  // A name only reachable under another role stays listed at zero rather than vanishing: the bar
  // is also how you find out that Dana opened things here but never commented on any of them.
  for (const who of filter.people) row(who)
  return byCount([...rows.values()]) as PersonOption[]
}

function bySignalOrder(options: FacetOption[]): FacetOption[] {
  const rank = new Map(SIGNALS.map((s, i) => [s.id, i]))
  return options.sort(
    (a, b) =>
      (rank.get(a.value) ?? SIGNALS.length) - (rank.get(b.value) ?? SIGNALS.length) ||
      a.value.localeCompare(b.value),
  )
}

// --- the query string ------------------------------------------------------------------

const REPEATED: [keyof TriageFilter, string][] = [
  ['mrs', 'mr'],
  ['people', 'who'],
  ['skills', 'skill'],
  ['kinds', 'kind'],
  ['states', 'state'],
  ['hiddenSignals', 'hide'],
]

const ROLES: PersonRole[] = ['any', 'commented', 'opened']

/**
 * The filter lives in the URL, so a narrowed queue is a link.
 *
 * That is what lets the health panel's uncovered-MR rows point at a merge request's whole set
 * rather than one candidate from it, and what makes "here, look at these four" a message someone
 * can send. Back and forward work for free.
 */
export function fromParams(params: URLSearchParams): TriageFilter {
  const role = params.get('role') ?? ''
  return {
    mrs: params.getAll('mr'),
    people: params.getAll('who'),
    role: (ROLES as string[]).includes(role) ? (role as PersonRole) : 'any',
    skills: params.getAll('skill'),
    kinds: params.getAll('kind'),
    states: params.getAll('state'),
    hiddenSignals: params.getAll('hide'),
  }
}

/**
 * The filter written back over whatever else the URL is carrying — `focus`, most importantly,
 * which is how a link lands on one candidate inside a filtered queue.
 */
export function toParams(filter: TriageFilter, base?: URLSearchParams): URLSearchParams {
  const params = new URLSearchParams(base)
  for (const [, key] of REPEATED) params.delete(key)
  params.delete('role')
  for (const [field, key] of REPEATED) {
    for (const value of filter[field] as string[]) params.append(key, value)
  }
  // Only when it changes something. A `role` on a URL with nobody picked is noise that survives
  // every later edit of the query string.
  if (filter.role !== 'any' && filter.people.length > 0) params.set('role', filter.role)
  return params
}

/** Toggle one value of a multi-select dimension. */
export function toggle(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((v) => v !== value) : [...values, value]
}

/**
 * Where the cursor lands after the list changes underneath it.
 *
 * Keyed on the candidate rather than on its position: the queue is filtered while someone is
 * part-way through it, and an index kept across that change silently moves the selection to a
 * different candidate — the form re-seeds, so nothing is written wrongly, but the row you were
 * reading is gone and nothing says so. Falling back to the top of the list is the honest answer.
 */
export function selectedIndex(items: QueueItem[], selectedId: string): number {
  const at = items.findIndex((item) => item.entry.candidate.id === selectedId)
  return at >= 0 ? at : 0
}
