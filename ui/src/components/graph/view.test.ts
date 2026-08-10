import { describe, expect, it } from 'vitest'
import {
  clampZoom,
  isWhole,
  MAX_ZOOM,
  MIN_ZOOM,
  panBy,
  toLayout,
  WHOLE,
  zoomAt,
} from '@/components/graph/view'

/**
 * The arithmetic behind moving the picture.
 *
 * Checked here rather than through the component because the browser geometry a canvas test would
 * need (`getScreenCTM`) does not exist in jsdom — so a component test could only assert that
 * nothing threw, which is the kind of test that passes while the graph drifts out from under the
 * cursor.
 */

describe('zoomAt', () => {
  it('holds the anchor still, which is the whole difference from resizing', () => {
    const view = { x: 0, y: 0, k: 1 }
    const anchor = { x: 300, y: 200 }
    const zoomed = zoomAt(view, anchor, 2)
    // Whatever was under the pointer before is under the pointer after.
    expect(toLayout(anchor, zoomed)).toEqual(toLayout(anchor, view))
  })

  it('holds it still from an already-moved view too', () => {
    const view = { x: -120, y: 40, k: 2.5 }
    const anchor = { x: 512, y: 77 }
    const before = toLayout(anchor, view)
    const after = toLayout(anchor, zoomAt(view, anchor, 1 / 1.4))
    expect(after.x).toBeCloseTo(before.x, 9)
    expect(after.y).toBeCloseTo(before.y, 9)
  })

  it('stops at the ends rather than zooming to a single pixel or a molecule', () => {
    expect(zoomAt({ x: 0, y: 0, k: MIN_ZOOM }, { x: 0, y: 0 }, 0.01).k).toBe(MIN_ZOOM)
    expect(zoomAt({ x: 0, y: 0, k: MAX_ZOOM }, { x: 0, y: 0 }, 100).k).toBe(MAX_ZOOM)
    expect(clampZoom(0)).toBe(MIN_ZOOM)
  })
})

describe('toLayout', () => {
  it('inverts the transform the canvas draws with', () => {
    const view = { x: 33, y: -14, k: 3 }
    const layout = toLayout({ x: 90, y: 10 }, view)
    // Drawing is `translate(x y) scale(k)`, so this has to be its exact inverse or a dragged node
    // lands somewhere the pointer never was.
    expect(layout.x * view.k + view.x).toBeCloseTo(90, 9)
    expect(layout.y * view.k + view.y).toBeCloseTo(10, 9)
  })
})

describe('panBy', () => {
  it('measures from where the drag started, so moves cannot accumulate error', () => {
    const start = { x: 10, y: 10, k: 2 }
    const from = { x: 100, y: 100 }
    const once = panBy(start, from, { x: 160, y: 130 })
    expect(once).toEqual({ x: 70, y: 40, k: 2 })
    // Two events in one gesture are both measured against `from`, not against each other.
    expect(panBy(start, from, { x: 130, y: 115 })).toEqual({ x: 40, y: 25, k: 2 })
  })

  it('leaves the scale alone', () => {
    expect(panBy({ x: 0, y: 0, k: 4 }, { x: 0, y: 0 }, { x: 5, y: 5 }).k).toBe(4)
  })
})

describe('isWhole', () => {
  it('is the test the reset control appears on', () => {
    expect(isWhole(WHOLE)).toBe(true)
    expect(isWhole({ ...WHOLE, k: 1.0001 })).toBe(false)
    expect(isWhole({ ...WHOLE, x: -1 })).toBe(false)
  })
})
