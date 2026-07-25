import { describe, expect, it } from 'vitest'
import { parseDiff } from './parse'

/**
 * The only number that matters here is the new-file line number: expectation regions and finding
 * locations are both expressed in it, and the triage screen turns a drag over these rows into a
 * range the server validates. A parser that invents or misnumbers a row silently produces eval
 * cases anchored to lines that do not exist.
 */

const HUNK = '@@ -40,3 +40,4 @@'

function diff(...lines: string[]): string {
  return ['diff --git a/a.rs b/a.rs', '--- a/a.rs', '+++ b/a.rs', ...lines].join('\n')
}

function shape(text: string) {
  return parseDiff(text)[0]!.lines.map((l) => `${l.kind}:${l.newLine ?? '-'}`)
}

describe('parseDiff', () => {
  it('numbers context and added lines, skipping removed ones', () => {
    const [file] = parseDiff(
      diff(HUNK, ' fn charge() {', '-    let row = db.get(id);', '+    let row = try_get(id)?;', ' }'),
    )
    expect(file!.path).toBe('a.rs')
    expect(file!.lines.map((l) => [l.kind, l.newLine])).toEqual([
      ['hunk', null],
      ['context', 40],
      ['del', null], // a removed line has no new-file number and must not advance the counter
      ['add', 41],
      ['context', 42],
    ])
  })

  it('does not invent a trailing line for a diff ending in a newline', () => {
    // `split('\n')` yields a trailing "" that is not a line of the file. Rendering it produced a
    // phantom row that took a line number and could be dragged into a region.
    const withNewline = diff(HUNK, ' fn charge() {', '+    x();', ' }', '')
    const withoutNewline = diff(HUNK, ' fn charge() {', '+    x();', ' }')
    expect(shape(withNewline)).toEqual(shape(withoutNewline))
    expect(shape(withNewline)).toEqual(['hunk:-', 'context:40', 'add:41', 'context:42'])
  })

  it('keeps genuinely blank lines inside a hunk', () => {
    // A blank line in the middle is real content and must keep its number.
    const [file] = parseDiff(diff('@@ -1,3 +1,3 @@', ' one', ' ', ' three', ''))
    expect(file!.lines.map((l) => [l.kind, l.newLine, l.content])).toEqual([
      ['hunk', null, '@@ -1,3 +1,3 @@'],
      ['context', 1, 'one'],
      ['context', 2, ''],
      ['context', 3, 'three'],
    ])
  })

  it('restarts numbering per file', () => {
    const files = parseDiff(
      [
        '--- a/a.rs', '+++ b/a.rs', '@@ -1,1 +1,2 @@', ' one', '+two',
        '--- a/b.rs', '+++ b/b.rs', '@@ -10,1 +10,2 @@', ' ten', '+eleven', '',
      ].join('\n'),
    )
    expect(files.map((f) => f.path)).toEqual(['a.rs', 'b.rs'])
    expect(files[0]!.lines.map((l) => l.newLine)).toEqual([null, 1, 2])
    expect(files[1]!.lines.map((l) => l.newLine)).toEqual([null, 10, 11])
  })

  it('honours each hunk header, including gaps between hunks', () => {
    const [file] = parseDiff(
      diff('@@ -1,1 +1,1 @@', ' one', '@@ -50,1 +80,2 @@', ' eighty', '+eightyone', ''),
    )
    expect(file!.lines.map((l) => l.newLine)).toEqual([null, 1, null, 80, 81])
  })

  it('treats a single-line hunk header as count 1', () => {
    const [file] = parseDiff(diff('@@ -5 +7 @@', '+seven', ''))
    expect(file!.lines.map((l) => [l.kind, l.newLine])).toEqual([
      ['hunk', null],
      ['add', 7],
    ])
  })

  it('does not count the no-newline marker as a line', () => {
    const [file] = parseDiff(diff(HUNK, '+last', '\\ No newline at end of file', ''))
    expect(file!.lines.map((l) => [l.kind, l.newLine])).toEqual([
      ['hunk', null],
      ['add', 40],
      ['meta', null],
    ])
  })

  it('strips the a/ and b/ prefixes', () => {
    expect(parseDiff(diff(HUNK, '+x', ''))[0]!.path).toBe('a.rs')
  })

  it('returns nothing for empty input', () => {
    expect(parseDiff('')).toEqual([])
    expect(parseDiff('\n')).toEqual([])
  })

  it('ignores index and mode metadata', () => {
    const [file] = parseDiff(
      ['diff --git a/a.rs b/a.rs', 'index abc123..def456 100644', '--- a/a.rs', '+++ b/a.rs',
       HUNK, '+x', ''].join('\n'),
    )
    expect(file!.lines.map((l) => l.kind)).toEqual(['hunk', 'add'])
  })
})
