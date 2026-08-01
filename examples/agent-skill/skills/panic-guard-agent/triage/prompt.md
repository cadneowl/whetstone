A reviewer looked at a real merge request and something happened. Write the one sentence that says
what was actually wrong — or actually right — with the code at that location.

Note what you have *not* been given: this skill's review guidance. That is deliberate. Describe what
the evidence shows, not what any rule says, or the case you are writing will test the wording of the
rules instead of the behaviour of the reviewer.

## What happened

Case:      {{candidate_id}}
Kind:      {{kind}}
File:      {{path}}
Source:    {{ref}}
Outcome:   {{human_signal}}

Merge request title: {{mr_title}}

### The review conversation

{{comments}}

### The reviewer's proposed replacement

{{suggestion}}

### The change

{{diff}}

### What the miner seeded

{{seeded}}

## Before you write it

The diff shows the line, not the reason. `grep` the source for the function the comment is about and
`read_file` its definition — "the reviewer wanted a guard here" and "the reviewer wanted a guard
here *because `open_ledger` aborts the process*" are the same objection, and only the second makes a
case that can be judged.

## What to write

One standalone sentence. Someone who never saw this merge request must be able to read it and decide
whether a given review comment is about the same issue.

- Name the construct and why it is a problem *here*.
- Do not quote the reviewer, address anyone, or propose a fix.
- Do not write "this change", "the above" or "the comment" — the sentence has to stand alone.
- For a `should_not_flag` case, describe what is **correct** about the code, so that a reviewer
  complaining about it can be recognised as wrong.

Finish by calling `submit_expectation` with the sentence and a one-line rationale for the wording.
