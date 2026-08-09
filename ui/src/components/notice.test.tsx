// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { emptyDraftNotice, Notice } from '@/components/ImproveWorkspace'

/**
 * The colour was the defect, so the colour is what this asserts.
 *
 * Every notice on this screen rendered `border-good`. One of the three that land there is a dead
 * end — "the drafter proposed no change" — and it appeared in the success colour beside a scorer
 * reporting every case failing. A reader scanning for red found none and concluded the improve had
 * nothing to do, which is exactly backwards.
 *
 * `role="alert"` as well as the class, because that is the half a screen reader gets.
 */
afterEach(cleanup)

describe('Notice', () => {
  it('renders nothing when there is nothing to say', () => {
    const { container } = render(<Notice notice={null} />)
    expect(container.innerHTML).toBe('')
  })

  it('shows a good outcome in the good colour, and does not alert', () => {
    render(<Notice notice={{ text: 'Scored: recall 0.80' }} />)

    const line = screen.getByText(/Scored/)
    expect(line.className).toContain('border-good')
    expect(line.getAttribute('role')).toBeNull()
  })

  it('carries the serversentence for an empty draft, rather than the bland one', () => {
    const stalled =
      'This run proposed nothing, and the reason is not the guidance: the reviewer never ' +
      'opened payments/.agents/context.md.'

    expect(emptyDraftNotice({ stalled })).toEqual({ text: stalled, bad: true })
  })

  it('falls back to the old wording, still as a dead end', () => {
    // An older server, or a cause the harness could not establish. The fallback is the weaker
    // sentence — never the weaker colour, which is what made this invisible.
    expect(emptyDraftNotice({})).toEqual({ text: 'The drafter proposed no change.', bad: true })
  })

  it('shows a dead end in the bad colour, and alerts', () => {
    render(<Notice notice={{ text: 'This run proposed nothing.', bad: true }} />)

    const line = screen.getByRole('alert')
    expect(line.textContent).toBe('This run proposed nothing.')
    expect(line.className).toContain('border-bad')
    expect(line.className).not.toContain('border-good')
  })
})
