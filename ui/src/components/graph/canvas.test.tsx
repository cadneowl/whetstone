// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Canvas } from '@/components/graph/Canvas'
import type { GraphPalette } from '@/components/graph/types'

/**
 * What clicking a dot does.
 *
 * The complaint that produced this file was "when I click on a circle I get nothing", and it was
 * two separate failures wearing one face: the dot was under five other dots (fixed in
 * `graphLayout`), and nothing on the picture acknowledged the click. So the acknowledgements are
 * pinned here — the selection reaching the caller, the neighbourhood staying lit while the rest
 * dims, the label appearing on what you picked and on what it is attached to.
 *
 * The gestures that need real geometry (pan, wheel zoom, dragging a node) are not testable in
 * jsdom, which implements no `getScreenCTM`; their arithmetic is pinned in `view.test.ts` instead.
 * The zoom *buttons* need no geometry, so they are here.
 */

const PALETTE: GraphPalette = {
  colour: { file: 'var(--color-accent)', rule: 'var(--color-ink)' },
  help: {},
  hollow: 'unresolved',
  anchors: [],
  edge: { contains: { opacity: 0.3 } },
  edgeHelp: { contains: 'holds this' },
  edgeRelation: { contains: { out: 'holds', in: 'lives in' } },
}

const nodes = [
  { id: 'file', kind: 'file', label: 'SKILL.md', degree: 1, missing: false },
  { id: 'R1', kind: 'rule', label: 'R1', degree: 1, missing: false },
  { id: 'far', kind: 'rule', label: 'R9', degree: 0, missing: false },
]
const edges = [{ source: 'file', target: 'R1', kind: 'contains' }]
const positions = new Map([
  ['file', { x: 100, y: 100 }],
  ['R1', { x: 200, y: 100 }],
  ['far', { x: 700, y: 400 }],
])

afterEach(cleanup)

function draw(overrides: Partial<Parameters<typeof Canvas>[0]> = {}) {
  const onSelect = vi.fn()
  const onFocus = vi.fn()
  const props: Parameters<typeof Canvas>[0] = {
    nodes,
    edges,
    positions,
    box: { width: 900, height: 460 },
    matched: new Set(),
    selected: null,
    palette: PALETTE,
    rings: () => [],
    flag: () => false,
    nodeTitle: (node) => node.label,
    edgeTitle: (edge) => edge.kind,
    ariaLabel: 'test graph',
    onSelect,
    onFocus,
    ...overrides,
  }
  const result = render(<Canvas {...props} />)
  const groupFor = (label: string) =>
    [...result.container.querySelectorAll('g')].find(
      (g) => g.querySelector(':scope > title')?.textContent === label,
    )
  return { onSelect, onFocus, groupFor, props, ...result }
}

describe('clicking a dot', () => {
  it('tells the caller which one, so the card below can be about it', () => {
    const { onSelect, groupFor } = draw()
    fireEvent.click(groupFor('R1')!)
    expect(onSelect).toHaveBeenCalledWith('R1')
  })

  it('clears the selection when the same one is clicked again', () => {
    const { onSelect, groupFor } = draw({ selected: 'R1' })
    fireEvent.click(groupFor('R1')!)
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('centres the graph on a double click without also selecting twice', () => {
    const { onFocus, groupFor } = draw()
    fireEvent.doubleClick(groupFor('R1')!)
    expect(onFocus).toHaveBeenCalledWith(expect.objectContaining({ id: 'R1' }))
  })
})

describe('clicking a dot in a real browser, where dragging captures the pointer', () => {
  // In a browser every press on a dot starts a capture-backed drag gesture, and pointer capture
  // retargets the `click` that follows at the svg — so the `<g>`'s own onClick never fires and, for
  // months, clicking a dot did nothing. The selection is therefore decided on pointer-up, and this
  // block is that path pinned, with the geometry jsdom does not implement stubbed in.
  function withGeometry(container: HTMLElement) {
    const element = container.querySelector('svg') as SVGSVGElement
    Object.assign(element, {
      getScreenCTM: () => ({ inverse: () => ({}) }),
      setPointerCapture: () => {},
      releasePointerCapture: () => {},
    })
    vi.stubGlobal(
      'DOMPoint',
      class {
        constructor(
          public x: number,
          public y: number,
        ) {}
        matrixTransform() {
          return { x: this.x, y: this.y }
        }
      },
    )
    return element
  }

  afterEach(() => vi.unstubAllGlobals())

  it('selects on press-and-release, with no click event ever reaching the dot', () => {
    const { onSelect, groupFor, container } = draw()
    const svg = withGeometry(container)
    const dot = groupFor('R1')!
    fireEvent.pointerDown(dot, { button: 0, clientX: 200, clientY: 100, pointerId: 1 })
    fireEvent.pointerUp(svg, { clientX: 200, clientY: 100, pointerId: 1 })
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith('R1')
    // The click the browser retargets after a capture must not answer the same press again —
    // whether it lands on the svg or, in an environment that does not retarget, on the dot.
    fireEvent.click(dot)
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('does not select at the end of a drag, and does not move a dot during a click', () => {
    const { onSelect, groupFor, container } = draw()
    const svg = withGeometry(container)
    const dot = groupFor('R1')!
    fireEvent.pointerDown(dot, { button: 0, clientX: 200, clientY: 100, pointerId: 1 })
    fireEvent.pointerMove(svg, { clientX: 260, clientY: 100, pointerId: 1 })
    fireEvent.pointerUp(svg, { clientX: 260, clientY: 100, pointerId: 1 })
    expect(onSelect).not.toHaveBeenCalled()
    expect(groupFor('R1')!.getAttribute('transform')).toBe('translate(260 100)')

    // A tremble within the slop is still a click, and the dot has not moved to show for it.
    fireEvent.pointerDown(dot, { button: 0, clientX: 260, clientY: 100, pointerId: 1 })
    fireEvent.pointerMove(svg, { clientX: 262, clientY: 100, pointerId: 1 })
    fireEvent.pointerUp(svg, { clientX: 262, clientY: 100, pointerId: 1 })
    expect(onSelect).toHaveBeenCalledWith('R1')
    expect(groupFor('R1')!.getAttribute('transform')).toBe('translate(260 100)')
  })

  it('treats a second quick press on the same dot as the double click that centres', () => {
    const { onSelect, onFocus, groupFor, container } = draw()
    const svg = withGeometry(container)
    const dot = groupFor('R1')!
    for (let press = 0; press < 2; press += 1) {
      fireEvent.pointerDown(dot, { button: 0, clientX: 200, clientY: 100, pointerId: 1 })
      fireEvent.pointerUp(svg, { clientX: 200, clientY: 100, pointerId: 1 })
    }
    expect(onFocus).toHaveBeenCalledWith(expect.objectContaining({ id: 'R1' }))
    // The first press selected; the second centred rather than toggling the selection back off.
    expect(onSelect).toHaveBeenCalledTimes(1)
  })
})

describe('what a selection does to the picture', () => {
  it('keeps the neighbourhood lit and dims only the strangers', () => {
    // Dimming everything but the node — which is what this used to do — hides the answer along with
    // the noise: what a dot is attached to is the entire reason to click it.
    const { groupFor } = draw({ selected: 'R1' })
    expect(groupFor('R1')!.getAttribute('opacity')).toBe('1')
    expect(groupFor('SKILL.md')!.getAttribute('opacity')).toBe('1')
    expect(groupFor('R9')!.getAttribute('opacity')).toBe('0.25')
  })

  it('names the selected node and its neighbours', () => {
    const { container } = draw({
      selected: 'R1',
      // Enough nodes that labels are not handed out for free.
      nodes: [
        ...nodes,
        ...Array.from({ length: 30 }, (_, i) => ({
          id: `x${i}`,
          kind: 'rule',
          label: `X${i}`,
          degree: 0,
          missing: false,
        })),
      ],
      positions: new Map([
        ...positions,
        ...Array.from({ length: 30 }, (_, i) => [`x${i}`, { x: 10 * i, y: 400 }] as const),
      ]),
    })
    const labels = [...container.querySelectorAll('text')].map((t) => t.textContent)
    expect(labels).toContain('R1')
    expect(labels).toContain('SKILL.md')
    expect(labels).not.toContain('X7')
  })
})

describe('the zoom controls', () => {
  it('scales the drawing and offers a way back only once something has moved', () => {
    const { container } = draw()
    const transform = () => container.querySelector('svg > g')!.getAttribute('transform')
    expect(transform()).toBe('translate(0 0) scale(1)')
    expect(screen.queryByLabelText(/put every dot back/i)).toBeNull()

    fireEvent.click(screen.getByLabelText('Zoom in'))
    expect(transform()).not.toBe('translate(0 0) scale(1)')

    fireEvent.click(screen.getByLabelText(/put every dot back/i))
    expect(transform()).toBe('translate(0 0) scale(1)')
  })

  it('names every dot once you have zoomed in far enough to have room for the names', () => {
    // On a big graph nothing is labelled at rest, so without this the only way to learn what a dot
    // is called is to hover it, one at a time. Zooming is the reader asking for exactly this.
    const many = [
      ...nodes,
      ...Array.from({ length: 80 }, (_, i) => ({
        id: `x${i}`,
        kind: 'rule',
        label: `X${i}`,
        degree: 0,
        missing: false,
      })),
    ]
    const { container } = draw({
      nodes: many,
      positions: new Map([
        ...positions,
        ...Array.from({ length: 80 }, (_, i) => [`x${i}`, { x: 10 * i, y: 400 }] as const),
      ]),
    })
    const named = () => [...container.querySelectorAll('text')].map((t) => t.textContent)
    expect(named()).not.toContain('X7')

    // 1.4³ clears the 2.5× threshold.
    for (let i = 0; i < 3; i += 1) fireEvent.click(screen.getByLabelText('Zoom in'))
    expect(named()).toContain('X7')
  })
})

describe('the size of a label', () => {
  it('holds at ten on-screen pixels however big the layout box is', () => {
    // `boxFor` grows the box with the node count and the svg scales it down to fit the panel, so a
    // fixed "10px" shrank on screen as the graph grew — about four actual pixels at the 400-node
    // cap, which is not a label. Twice the box must mean twice the layout units: same size on
    // screen.
    const { container } = draw({ box: { width: 1800, height: 920 } })
    const text = container.querySelector('text')!
    expect(text.style.fontSize).toBe('20px')
  })
})

describe('at the size the server caps a query at', () => {
  const many = Array.from({ length: 200 }, (_, i) => ({
    id: `n${i}`,
    kind: 'rule',
    label: `node ${i}`,
    degree: 0,
    missing: false,
  }))
  const spread = new Map(many.map((node, i) => [node.id, { x: (i % 20) * 44, y: 40 * (i / 20) }]))

  it('gives every dot a target bigger than the dot, because the dot is three pixels', () => {
    // The visible circle is what the eye reads; it is not what the pointer has to hit. At the cap a
    // low-degree dot draws with a radius of about three pixels, and a seven-pixel target is an
    // attempt at a click rather than a click.
    const { container } = draw({ nodes: many, positions: spread })
    const first = container.querySelectorAll('g[transform^="translate"] > circle')
    const hit = Number(first[0]!.getAttribute('r'))
    const dot = Number(first[1]!.getAttribute('r'))
    expect(first[0]!.getAttribute('fill')).toBe('transparent')
    expect(hit).toBeGreaterThan(dot)
  })

  it('does not throw away the reader’s place when the same graph arrives again', () => {
    // Skills are read from disk per request and the console refetches on window focus, so an
    // unchanged graph comes back as a new array every time somebody alt-tabs. Keying the reset on
    // object identity lost the zoom and every dragged dot each time they did.
    const { container, rerender, props } = draw({ nodes: many, positions: spread })
    fireEvent.click(screen.getByLabelText('Zoom in'))
    const zoomed = container.querySelector('svg > g')!.getAttribute('transform')
    expect(zoomed).not.toBe('translate(0 0) scale(1)')

    rerender(<Canvas {...props} nodes={many.map((node) => ({ ...node }))} positions={spread} />)
    expect(container.querySelector('svg > g')!.getAttribute('transform')).toBe(zoomed)

    // A genuinely different graph is a different question, and does reset.
    rerender(<Canvas {...props} nodes={many.slice(0, 50)} positions={spread} />)
    expect(container.querySelector('svg > g')!.getAttribute('transform')).toBe(
      'translate(0 0) scale(1)',
    )
  })
})
