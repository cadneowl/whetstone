// @vitest-environment jsdom
import { MemoryRouter } from 'react-router-dom'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { NotInTheDiff, type Draft } from '@/components/ImproveWorkspace'

/**
 * An improve produces two things, and for the whole of this feature's life the panel rendered one.
 *
 * `sidecar_claims`, `misrouted` and `disputed_claims` were all on the wire — the server comments
 * even said *"Surfaced on the draft"* and *"Shown over the diff"* — of fields no component read.
 * The job log carried them, which is a different screen, and the one an operator scrolls past. On
 * a real run the drafter filed a claim and rewrote `SKILL.md`, and the panel said "a change to 1
 * file", so the routing looked like it had never happened.
 *
 * Rendered rather than asserted on a helper, because the failure was never in the logic: every
 * value was correct and none of it was on screen. The claims this file makes are that each finding
 * is *visible*, and that each one ends in something the reader can do — a panel that reports a
 * problem and offers no way out is the thing being fixed.
 */
const PATCH = 'diff --git a/payments/.agents/context.md b/payments/.agents/context.md\n+ a claim\n'

function draft(over: Partial<Draft> = {}): Draft {
  return {
    body: '# Rules',
    pages: {},
    rationale: '',
    selectedMissing: [],
    removedRules: [],
    claims: [],
    disputes: [],
    misrouted: [],
    duplicated: [],
    baseline: { body: '', pages: {} },
    ...over,
  }
}

const CLAIM = {
  path: 'payments/.agents/context.md',
  folder: 'payments',
  claim: 'Requests here are authenticated by the gateway.',
  excepts: '',
  because: 'true of this folder only',
  patch: PATCH,
  creates_file: true,
}

function show(d: Draft) {
  return render(
    <MemoryRouter>
      <NotInTheDiff draft={d} editorSearch="?tab=edit" />
    </MemoryRouter>,
  )
}

afterEach(cleanup)

describe('NotInTheDiff', () => {
  it('renders nothing when the draft is only a guidance change', () => {
    // `jest-dom` matchers are not installed here, so this asserts on the DOM directly.
    const { container } = show(draft())
    expect(container.innerHTML).toBe('')
  })

  it('shows a routed claim, where it goes, and why it is local', () => {
    show(draft({ claims: [CLAIM] }))

    expect(screen.getByText('payments/.agents/context.md')).toBeTruthy()
    expect(screen.getByText(/authenticated by the gateway/)).toBeTruthy()
    expect(screen.getByText(/true of this folder only/)).toBeTruthy()
    // The one sentence that stops "Apply" being read as delivering the claim too.
    expect(screen.getByText(/Nothing was written/)).toBeTruthy()
  })

  it('marks an exception with the rule it narrows', () => {
    show(draft({ claims: [{ ...CLAIM, excepts: 'R4' }] }))
    expect(screen.getByText('Excepts R4')).toBeTruthy()
  })

  it('hands the patch over, because the console is the only place it exists', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    show(draft({ claims: [CLAIM] }))

    fireEvent.click(screen.getByRole('button', { name: 'Copy patch' }))

    expect(writeText).toHaveBeenCalledWith(PATCH)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy())
  })

  it('offers a download too, so a refused clipboard is not a dead end', () => {
    show(draft({ claims: [CLAIM] }))
    expect(screen.getByRole('button', { name: 'Download .patch' })).toBeTruthy()
  })

  it('calls out one lesson filed in both homes, and names the two ways out', () => {
    show(draft({ claims: [CLAIM], misrouted: ['payments'], duplicated: ['payments'] }))

    const warning = screen.getByText(/The same lesson is in both homes/).closest('div')!
    expect(within(warning).getByText(/One home per lesson/)).toBeTruthy()
    // Both choices stated, and the local one is a link that goes somewhere.
    expect(within(warning).getByRole('link', { name: /delete the paragraph/ })).toBeTruthy()
    expect(within(warning).getByText(/keep it central/)).toBeTruthy()
  })

  it('does not report a duplicate twice as a plain misrouting', () => {
    show(draft({ claims: [CLAIM], misrouted: ['payments'], duplicated: ['payments'] }))
    // The weaker wording would send the reader to judge a question already settled.
    expect(screen.queryByText(/the old one did not/)).toBeNull()
  })

  it('reports a misrouting on its own when nothing was routed there', () => {
    show(draft({ misrouted: ['payments'] }))

    expect(screen.getByText(/The new guidance names payments/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'edit it out' })).toBeTruthy()
  })

  it('shows a dispute with its evidence and where it went', () => {
    show(
      draft({
        disputes: [
          {
            path: 'payments/.agents/context.md',
            claim: 'Retries are capped at 3.',
            evidence: 'svc.py:9',
          },
        ],
      }),
    )

    expect(screen.getByText(/Retries are capped at 3/)).toBeTruthy()
    expect(screen.getByText(/svc.py:9/)).toBeTruthy()
    expect(screen.getByText(/Filed to the ledger/)).toBeTruthy()
  })
})
