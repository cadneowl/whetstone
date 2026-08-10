/**
 * Pan and zoom, as arithmetic.
 *
 * Separate from `Canvas` and free of React for the reason `graphLayout.ts` is: these three functions
 * decide where every dot on screen ends up once a reader has moved the picture, and a mistake in one
 * of them is a graph that drifts under the cursor or a drag that drops a node somewhere it was never
 * pointed at. That is worth pinning without mounting a component — the more so because the browser
 * geometry a canvas test would need (`getScreenCTM`) does not exist in jsdom, so the *only* place
 * this can be checked honestly is here.
 */

/** A view of the layout box: `k` of 1 at the origin is the whole picture, drawn to fit. */
export interface View {
  x: number
  y: number
  k: number
}

export interface ViewPoint {
  x: number
  y: number
}

/** The untransformed view — everything, at the scale the layout was computed for. */
export const WHOLE: View = { x: 0, y: 0, k: 1 }

/** How far in and out a reader may go. Past these the picture stops being the same picture. */
export const MIN_ZOOM = 0.4
export const MAX_ZOOM = 12

export function clampZoom(k: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k))
}

/**
 * Zoom by `factor` while holding `anchor` — a point in the SVG's own coordinates — still.
 *
 * Holding a point fixed is what separates zooming from resizing: without it the thing you were
 * looking at slides away exactly when you lean in to read it, and on a three-hundred-node graph you
 * lose it entirely. The anchor is the pointer for a wheel and the centre of the frame for a button,
 * which is the only difference between the two.
 */
export function zoomAt(view: View, anchor: ViewPoint, factor: number): View {
  const k = clampZoom(view.k * factor)
  return {
    k,
    x: anchor.x - ((anchor.x - view.x) / view.k) * k,
    y: anchor.y - ((anchor.y - view.y) / view.k) * k,
  }
}

/** Where a point in the SVG's coordinates sits in the layout's — which dot is under the pointer. */
export function toLayout(point: ViewPoint, view: View): ViewPoint {
  return { x: (point.x - view.x) / view.k, y: (point.y - view.y) / view.k }
}

/** The view that has been dragged from `from` to `to`, both in the SVG's own coordinates. */
export function panBy(start: View, from: ViewPoint, to: ViewPoint): View {
  return { k: start.k, x: start.x + (to.x - from.x), y: start.y + (to.y - from.y) }
}

/** Whether a reader has moved this view at all — what the reset control exists for. */
export function isWhole(view: View): boolean {
  return view.k === WHOLE.k && view.x === WHOLE.x && view.y === WHOLE.y
}
