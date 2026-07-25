/**
 * Unified-diff parsing, mirroring `domain/change.py:parse_hunk_added_lines`.
 *
 * The only number that matters downstream is the **new-file line number**: expectation regions and
 * finding locations are both expressed in it, so getting this wrong silently misplaces every
 * overlay. Removed lines do not advance the counter; context and added lines do.
 */

export type DiffLineKind = 'add' | 'del' | 'context' | 'hunk' | 'meta'

export interface DiffLine {
  kind: DiffLineKind
  /** Line number in the new file, or null for removed/meta lines that have none. */
  newLine: number | null
  content: string
}

export interface DiffFile {
  path: string
  lines: DiffLine[]
}

const HUNK = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/

export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = []
  let current: DiffFile | null = null
  let newLine = 0

  // A diff ends with a newline, so splitting yields a trailing "" that is not a line of the file.
  // Treating it as context invented a phantom row at the end of every diff — one that took a line
  // number and could be selected as part of a region.
  const lines = text.split('\n')
  if (lines[lines.length - 1] === '') lines.pop()

  for (const raw of lines) {
    if (raw.startsWith('diff --git') || raw.startsWith('index ')) continue
    if (raw.startsWith('--- ')) continue
    if (raw.startsWith('+++ ')) {
      current = { path: stripPrefix(raw.slice(4).trim()), lines: [] }
      files.push(current)
      newLine = 0
      continue
    }
    if (current === null) continue

    const hunk = HUNK.exec(raw)
    if (hunk) {
      newLine = Number(hunk[1])
      current.lines.push({ kind: 'hunk', newLine: null, content: raw })
      continue
    }
    if (raw.startsWith('\\')) {
      current.lines.push({ kind: 'meta', newLine: null, content: raw })
      continue
    }
    if (raw.startsWith('+')) {
      current.lines.push({ kind: 'add', newLine, content: raw.slice(1) })
      newLine += 1
      continue
    }
    if (raw.startsWith('-')) {
      current.lines.push({ kind: 'del', newLine: null, content: raw.slice(1) })
      continue
    }
    current.lines.push({
      kind: 'context',
      newLine,
      content: raw.startsWith(' ') ? raw.slice(1) : raw,
    })
    newLine += 1
  }

  return files
}

function stripPrefix(path: string): string {
  return path.startsWith('a/') || path.startsWith('b/') ? path.slice(2) : path
}
