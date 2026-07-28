A reviewer looked at a real merge request and something happened. Write the one sentence that says
what was actually wrong (or right) with the code at that location.

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

```diff
{{diff}}
```

### What the miner guessed the expectation should be

{{seeded}}

## What to write

One sentence, describing the underlying problem at that location.

- Standalone. Somebody who never saw this merge request has to be able to read your sentence and
  decide whether a given review comment is about the same issue.
- Name the construct and say why it is a problem *here* — "unwrap on the DB lookup panics when the
  row is absent, which is a normal error path" rather than "error handling issue".
- Do not quote the reviewer, address anyone, or propose a fix.
- Do not write "this change", "the above" or "the comment". There is no context to refer to.
- For a `should_not_flag` case, describe what is CORRECT about the code, so that a reviewer
  objecting to it can be recognised as wrong.

You have deliberately not been shown the skill's guidance. Describe what the evidence shows, not
what any rule says — an expectation written in the rules' own words would match the reviewer's
output automatically and the case would pass forever without measuring anything.

Return the sentence, and a one-line rationale for the wording you chose.
