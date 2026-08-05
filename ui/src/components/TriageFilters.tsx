import { useMemo, useState } from 'react'
import type { QueueItem } from '@/api/client'
import {
  NONE,
  NO_FILTER,
  applyFilters,
  facets,
  isFiltered,
  offersChoice,
  toggle,
  type FacetOption,
  type PersonOption,
  type PersonRole,
  type TriageFilter,
} from '@/routes/triageFilters'
import { signalMeta } from './signals'

/**
 * Narrowing the queue to the work in front of you.
 *
 * A month of merge requests is worked by person and by merge request at least as often as by
 * subject — "finish !812", "everything Dana commented on", "the ones I opened" — and until now the
 * only way to reach any of those was to scroll until you recognised something.
 *
 * In the queue pane rather than across the top of the screen: the three panes are one viewport tall
 * by design, and a full-width filter bar takes that height out of the diff and the promote button
 * for the sake of a control that belongs to the list it filters.
 *
 * The menus expand inline rather than floating. The pane is fifteen rem wide and already scrolls,
 * so a popover would be positioned against a scrolling container and clipped by it — and pushing
 * the queue down while you choose is a truthful thing for it to do, since choosing is what changes
 * the queue.
 */
export function TriageFilters({
  items,
  filter,
  onChange,
}: {
  items: QueueItem[]
  filter: TriageFilter
  onChange: (next: TriageFilter) => void
}) {
  const options = useMemo(() => facets(items, filter), [items, filter])
  const shown = useMemo(() => applyFilters(items, filter).length, [items, filter])
  const set = (over: Partial<TriageFilter>) => onChange({ ...filter, ...over })

  const showing = {
    mrs: offersChoice(options.mrs, filter.mrs),
    people: offersChoice(options.people, filter.people),
    skills: offersChoice(options.skills, filter.skills),
    kinds: offersChoice(options.kinds, filter.kinds),
    signals: offersChoice(options.signals, filter.hiddenSignals),
  }
  // Nothing to filter by is not the same as a filter bar with nothing in it: one merge request and
  // one person in the queue means every control here is a no-op, and a row of them would be
  // furniture. A filter that *is* on always keeps the bar, whatever the facets collapsed to —
  // otherwise the queue is narrowed with nothing on screen saying so or undoing it.
  if (!isFiltered(filter) && !Object.values(showing).some(Boolean)) return null
  // Each heading earns its place separately. A queue from one merge request by one person still
  // has signals worth hiding, and a bare "Show only" over nothing is a control that went missing.
  const narrowing = showing.mrs || showing.people || showing.skills || showing.kinds

  return (
    <div className="shrink-0 space-y-2 border-b border-line pb-3">
      {narrowing && (
        <Row label="Show only">
          {showing.mrs && (
            <Menu
              label="Merge request"
              noneLabel="no reference"
              noneHint="Nothing in this candidate's provenance names where it came from."
              searchable
              options={options.mrs}
              selected={filter.mrs}
              onToggle={(value) => set({ mrs: toggle(filter.mrs, value) })}
            />
          )}
          {showing.people && (
            <PersonMenu
              options={options.people}
              selected={filter.people}
              role={filter.role}
              onRole={(role) => set({ role })}
              onToggle={(value) => set({ people: toggle(filter.people, value) })}
            />
          )}
          {showing.skills && (
            <Menu
              label="Skill"
              noneLabel="unrouted"
              noneHint="The miner could not guess a target skill — choose one on the form before promoting."
              options={options.skills}
              selected={filter.skills}
              onToggle={(value) => set({ skills: toggle(filter.skills, value) })}
            />
          )}
          {showing.kinds && (
            <Menu
              label="Kind"
              options={options.kinds}
              selected={filter.kinds}
              onToggle={(value) => set({ kinds: toggle(filter.kinds, value) })}
            />
          )}
        </Row>
      )}

      {showing.signals && (
        <Row
          label="Hide"
          hint="These exclude rather than include: a comment-free merge yields a candidate per changed file, and the queue is mostly those. Struck through means hidden."
        >
          <div className="flex flex-wrap gap-1">
            {options.signals.map((option) => (
              <SignalChip
                key={option.value}
                option={option}
                hidden={filter.hiddenSignals.includes(option.value)}
                onToggle={() => set({ hiddenSignals: toggle(filter.hiddenSignals, option.value) })}
              />
            ))}
          </div>
        </Row>
      )}

      {isFiltered(filter) && (
        <p className="flex items-baseline gap-2 text-[11px] text-muted">
          <span className="tabular">
            {shown} of {items.length} shown
          </span>
          <button
            type="button"
            onClick={() => onChange(NO_FILTER)}
            className="underline hover:text-ink"
          >
            clear
          </button>
        </p>
      )}
    </div>
  )
}

function Row({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div>
      <p
        className="mb-1 text-[10px] tracking-wide text-muted uppercase"
        title={hint}
        style={hint ? { cursor: 'help' } : undefined}
      >
        {label}
      </p>
      <div className="flex flex-wrap gap-1">{children}</div>
    </div>
  )
}

/**
 * One dimension: a button that says what is picked, and a list of what else could be.
 *
 * The count beside each value is what picking it would leave, measured with this dimension's own
 * selection lifted and every other one applied — so it never reads 0 beside everything the moment
 * anything is picked, and never promises rows that another filter has already excluded.
 */
function Menu({
  label,
  options,
  selected,
  onToggle,
  searchable,
  noneLabel = 'unknown',
  noneHint,
}: {
  label: string
  options: FacetOption[]
  selected: string[]
  onToggle: (value: string) => void
  searchable?: boolean
  noneLabel?: string
  noneHint?: string
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const shown = matching(options, query, noneLabel)

  return (
    <div className={open ? 'w-full' : undefined}>
      <Trigger label={label} count={selected.length} open={open} onClick={() => setOpen(!open)} />
      {open && (
        <div className="mt-1 rounded border border-line bg-canvas p-1.5">
          {searchable && (
            <input
              value={query}
              autoFocus
              onChange={(e) => setQuery(e.target.value)}
              placeholder={`filter ${options.length} …`}
              className="mb-1 w-full rounded border border-line bg-surface px-1.5 py-1 text-xs outline-none focus:border-accent/60"
            />
          )}
          <ul className="max-h-52 space-y-0.5 overflow-y-auto">
            {shown.map((option) => (
              <li key={option.value}>
                <Choice
                  checked={selected.includes(option.value)}
                  onToggle={() => onToggle(option.value)}
                  name={option.label || noneLabel}
                  muted={option.value === NONE}
                  title={option.value === NONE ? noneHint : option.detail}
                  detail={option.detail}
                  count={option.count}
                />
              </li>
            ))}
            {shown.length === 0 && (
              <li className="px-1 py-1 text-[11px] text-muted italic">nothing matches “{query}”</li>
            )}
          </ul>
        </div>
      )}
      {!open && selected.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {selected.map((value) => (
            <li key={value}>
              <Picked label={value === NONE ? noneLabel : value} onRemove={() => onToggle(value)} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * People, with the two relationships kept apart.
 *
 * "The merge requests Dana opened" and "the ones Dana commented on" are different questions, and
 * only the second is answerable from a thread — which is why the merge request author had to start
 * being recorded for this to exist at all. Each name carries both numbers so the choice is visible
 * before it is made; the count that decides the queue is the one for the role in force.
 */
function PersonMenu({
  options,
  selected,
  role,
  onRole,
  onToggle,
}: {
  options: PersonOption[]
  selected: string[]
  role: PersonRole
  onRole: (role: PersonRole) => void
  onToggle: (value: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const shown = matching(options, query, 'unattributed') as PersonOption[]

  return (
    <div className={open ? 'w-full' : undefined}>
      <Trigger label="Person" count={selected.length} open={open} onClick={() => setOpen(!open)} />
      {open && (
        <div className="mt-1 rounded border border-line bg-canvas p-1.5">
          <div className="mb-1 flex gap-1">
            {(['any', 'commented', 'opened'] as PersonRole[]).map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={role === option}
                onClick={() => onRole(option)}
                title={ROLE_HINT[option]}
                className={`rounded border px-1.5 py-0.5 text-[11px] transition-colors ${
                  role === option
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-line text-muted hover:text-ink'
                }`}
              >
                {option}
              </button>
            ))}
          </div>
          <input
            value={query}
            autoFocus
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`filter ${options.length} …`}
            className="mb-1 w-full rounded border border-line bg-surface px-1.5 py-1 text-xs outline-none focus:border-accent/60"
          />
          <ul className="max-h-52 space-y-0.5 overflow-y-auto">
            {shown.map((option) => (
              <li key={option.value}>
                <Choice
                  checked={selected.includes(option.value)}
                  onToggle={() => onToggle(option.value)}
                  name={option.label || 'unattributed'}
                  muted={option.value === NONE}
                  title={
                    option.value === NONE
                      ? 'Nobody is recorded on these — no author, no comments. Candidates mined before merge request authors were carried show up here until `whetstone corpus pull --refresh` rewrites them.'
                      : `${option.commented} commented on · ${option.opened} opened`
                  }
                  detail={
                    option.value === NONE
                      ? undefined
                      : `${option.commented} commented · ${option.opened} opened`
                  }
                  count={option.count}
                />
              </li>
            ))}
            {shown.length === 0 && (
              <li className="px-1 py-1 text-[11px] text-muted italic">nothing matches “{query}”</li>
            )}
          </ul>
        </div>
      )}
      {!open && selected.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {selected.map((value) => (
            <li key={value}>
              <Picked
                label={value === NONE ? 'unattributed' : value}
                note={role === 'any' ? undefined : role}
                onRemove={() => onToggle(value)}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

const ROLE_HINT: Record<PersonRole, string> = {
  any: 'Opened it or said something on it.',
  commented: 'Left a comment on the thread — what they reviewed, not what they wrote.',
  opened: 'Opened the merge request. Blank for candidates mined before this was recorded.',
}

function Trigger({
  label,
  count,
  open,
  onClick,
}: {
  label: string
  count: number
  open: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={open}
      className={`rounded border px-2 py-0.5 text-xs transition-colors ${
        count > 0
          ? 'border-accent/60 bg-accent/10 text-accent'
          : 'border-line text-muted hover:border-accent/50 hover:text-ink'
      }`}
    >
      {label}
      {count > 0 && <span className="ml-1 tabular">{count}</span>}
      <span className="ml-1 text-[9px]">{open ? '▴' : '▾'}</span>
    </button>
  )
}

function Choice({
  checked,
  onToggle,
  name,
  detail,
  count,
  muted,
  title,
}: {
  checked: boolean
  onToggle: () => void
  name: string
  detail?: string
  count: number
  muted?: boolean
  title?: string
}) {
  return (
    <label
      title={title}
      className="flex cursor-pointer items-baseline gap-1.5 rounded px-1 py-0.5 text-xs hover:bg-surface"
    >
      <input type="checkbox" checked={checked} onChange={onToggle} className="shrink-0" />
      <span className={`min-w-0 flex-1 truncate ${muted ? 'text-muted italic' : ''}`}>
        <span className="font-mono">{name}</span>
        {detail && <span className="ml-1.5 text-[10px] text-muted">{detail}</span>}
      </span>
      {/* A picked value that another filter has emptied stays on the list showing the 0 it earned,
          because it is the only control for unpicking itself. */}
      <span className={`shrink-0 tabular ${count === 0 ? 'text-muted/50' : 'text-muted'}`}>
        {count}
      </span>
    </label>
  )
}

/** What is picked, while the menu it came from is shut. */
function Picked({ label, note, onRemove }: { label: string; note?: string; onRemove: () => void }) {
  return (
    <span className="flex items-baseline gap-1 rounded border border-accent/40 bg-accent/5 px-1.5 py-0.5 text-[11px]">
      <span className="min-w-0 flex-1 truncate font-mono" title={label}>
        {label}
      </span>
      {note && <span className="shrink-0 text-muted">{note}</span>}
      <button
        type="button"
        onClick={onRemove}
        aria-label={`stop filtering by ${label}`}
        className="shrink-0 text-muted hover:text-bad"
      >
        ×
      </button>
    </span>
  )
}

/**
 * The signal chips, which hide rather than show — kept as they were.
 *
 * A comment-free merge yields one `merged clean` candidate per changed file, so a repo that reviews
 * by talking produces a queue that is mostly those, and they are the weakest thing the builder
 * makes. Getting rid of them is one click here and nine in an include-only bar, which is why this
 * row keeps its own vocabulary instead of being folded into the others.
 *
 * Doubles as the legend: every chip carries the signal's meaning on hover.
 */
function SignalChip({
  option,
  hidden,
  onToggle,
}: {
  option: FacetOption
  hidden: boolean
  onToggle: () => void
}) {
  const meta = signalMeta(option.value)
  return (
    <button
      type="button"
      title={meta.meaning}
      aria-pressed={!hidden}
      onClick={onToggle}
      className={`rounded-full border px-2 py-0.5 text-[11px] transition-colors ${
        hidden
          ? 'border-line text-muted line-through opacity-50 hover:opacity-80'
          : 'border-line hover:border-accent/50'
      }`}
    >
      {meta.short} <span className="tabular text-muted">{option.count}</span>
    </button>
  )
}

/** Substring match over the value and whatever is written beside it — a merge request title. */
function matching(options: FacetOption[], query: string, noneLabel: string): FacetOption[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return options
  return options.filter((option) =>
    `${option.label || noneLabel} ${option.detail ?? ''}`.toLowerCase().includes(needle),
  )
}
