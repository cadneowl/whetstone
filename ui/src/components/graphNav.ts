import type { GraphNode } from '@/api/client'

/**
 * Moving around the sidecar graph: where a position comes from, and where it can go.
 *
 * Pure and separate from the component for the reason `graphLayout.ts` is — these are the rules
 * that decide what clicking something does, and they are worth pinning without mounting React.
 *
 * The model is deliberately small. A *position* is a query, and the tree part of that query is a
 * `folder:` term, so every navigation action here is "produce the query that means where I want to
 * be". That keeps one language for a typed query, a clicked node and a breadcrumb, instead of a
 * navigation state that has to be kept in sync with a search box.
 */

/** The three things that make up a position in the graph, all of them shareable via the URL. */
export interface GraphParams {
  q: string | null
  hops: number | null
  node: string | null
}

/** How far out the graph reaches by default when the URL does not say. */
export const DEFAULT_HOPS = 1

/**
 * Hops as a number the server will accept, whatever the URL says.
 *
 * The absent case is checked before the numeric one, and that is the whole of it: `Number(null)`
 * and `Number('')` are both `0`, both finite, so a "is it a number?" test alone silently makes the
 * *default view* zero-hops — a graph of bare matches with none of their context, on every first
 * visit, for a reason no one would think to look for in a parse function.
 */
export function clampHops(raw: string | null): number {
  if (raw === null || raw.trim() === '') return DEFAULT_HOPS
  const value = Number(raw)
  if (!Number.isFinite(value)) return DEFAULT_HOPS
  return Math.max(0, Math.min(3, Math.trunc(value)))
}

/**
 * The `folder:` term of a query, or null when it has none.
 *
 * Read back out of the query rather than tracked beside it, so a hand-typed `folder:payments` and
 * a clicked breadcrumb produce the same position — and so a pasted URL knows where it is.
 */
export function folderOf(query: string): string | null {
  const match = /(?:^|\s)folder:(\S+)/.exec(query)
  return match ? (match[1] ?? null) : null
}

/**
 * The folder one level up, `''` for a top-level folder, or null when there is nowhere to go.
 *
 * `''` and `null` are different answers and callers render them differently: the first is "the
 * whole tree", the second is a node with no place in the tree at all — a rule or a reference.
 */
export function parentOf(path: string): string | null {
  if (!path || path === '.') return null
  const cut = path.lastIndexOf('/')
  return cut === -1 ? '' : path.slice(0, cut)
}

/** The query that means "one level up from `path`". Empty is the whole tree, which is a position. */
export function parentQuery(path: string): string {
  const parent = parentOf(path)
  return parent ? `folder:${parent}` : ''
}

/**
 * The query that centres the graph on one node.
 *
 * A folder or file becomes its subtree; a rule becomes the claims that except it; a reference
 * becomes its own label, which is what the field shorthands already mean. Claims are the
 * exception: their text is a sentence rather than a handle, so they centre on the folder they
 * belong to — which is what someone clicking a claim in a picture is asking to see anyway.
 */
export function focusQuery(node: GraphNode): string {
  if (node.kind === 'folder' || node.kind === 'file') return `folder:${node.path}`
  if (node.kind === 'claim') return `folder:${node.path}`
  if (node.kind === 'rule') return `excepts:${node.label}`
  return node.label
}

/** The path segments a breadcrumb should offer, given a position. `[]` means the whole tree. */
export function crumbsFor(query: string, focusedPath: string | null): string[] | null {
  const path = folderOf(query) ?? focusedPath
  if (path === null || path === undefined) return null
  if (path === '' || path === '.') return []
  return path.split('/').filter(Boolean)
}
