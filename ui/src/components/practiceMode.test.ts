import { describe, expect, it } from 'vitest'

import { onThisMachine } from './LaunchButton'

/**
 * The browser's copy of practice mode's one rule, used to warn *before* the confirm click rather
 * than to decide anything: the server refuses independently. It still has to agree with
 * `preflight.on_this_machine`, or the banner promises a launch the server then turns away.
 */
describe('onThisMachine', () => {
  it('accepts every shape of loopback, which is what the console demo needs', () => {
    // The demo serves its stub as `demo-stub` on 127.0.0.1 — a name no preset knows, so the
    // address is the only thing that can tell practice mode it is free to run.
    expect(onThisMachine('http://127.0.0.1:8789/v1')).toBe(true)
    expect(onThisMachine('http://localhost:11434/v1')).toBe(true)
    expect(onThisMachine('http://127.0.0.2:9000/v1')).toBe(true)
    expect(onThisMachine('http://[::1]:8080/v1')).toBe(true)
  })

  it('rejects anything that leaves the machine', () => {
    expect(onThisMachine('https://api.anthropic.com')).toBe(false)
    expect(onThisMachine('http://10.0.0.5:8000/v1')).toBe(false)
    expect(onThisMachine('http://gateway.acme.internal/v1')).toBe(false)
  })

  it('treats absent or unparseable as not local, the safe way round', () => {
    expect(onThisMachine(null)).toBe(false)
    expect(onThisMachine(undefined)).toBe(false)
    expect(onThisMachine('')).toBe(false)
    expect(onThisMachine('not a url')).toBe(false)
  })
})
