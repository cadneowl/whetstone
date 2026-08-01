"""Starter step folders, written correctly so nobody has to start from a blank file.

The request this answers was "provide instructions on how to write them, or propose writing them
yourself so it is done correctly". Prose in a docs folder is instructions somebody has to find,
read, and transcribe without typos. A generated folder that already runs is instructions they
cannot get wrong, so the templates below carry their own explanation: every knob is present with
its default and a comment saying what happens if you change it.

The values here are deliberately conservative. A scaffold is copied far more often than it is read,
so its defaults become the fleet's defaults, and a generous cap that nobody revisits is how a
hundred skills quietly start costing ten times what anyone budgeted.
"""

from __future__ import annotations

from pathlib import Path

from whetstone.steps import STEP_FILE

EVALUATE_STEP = """\
# How this skill is scored.
#
# Configuration only: there is no prompt and no program here. `whetstone eval run` and
# `whetstone eval gate` read this file and use it as their defaults, and any flag you pass on the
# command line overrides it.

description: Score this skill against its promoted eval cases.

# Reviewer passes per case. Raise to 2-3 for an unstable skill to measure variance; every
# increment multiplies the cost of every run and every gate by the same factor.
trials: 1

sample:
  # null scores every case. Set a number once the corpus outgrows what you can afford to run
  # whole — a few hundred is usually enough to move a score meaningfully.
  max_cases: null
  # Selection is a hash of this seed and the case id, so the same seed always draws the same
  # cases. Both sides of a gate use one draw, which is what keeps the comparison fair. Change it
  # only to deliberately re-roll which cases you are measuring on.
  seed: 0
  # Draw proportionally from each case kind. Turn this off and a sample of a corpus that is mostly
  # should_catch will sometimes contain no negative cases at all — and a false-positive rate
  # measured over zero negative cases is a flattering zero.
  stratify: true
  # Share of cases held out from the improve loop: still scored, never shown to the drafter,
  # reported separately. Train score climbing while holdout stalls is overfitting made visible —
  # the drafter memorizing its cases rather than learning the pattern. Membership is an unseeded
  # hash of the case id, so there is no knob to re-roll it. 0 disables the split.
  holdout_fraction: 0.2
  # How much of its proportional share an archived case keeps in a sampled draw. Cases marked
  # `tier: archive` in their case.yaml are lessons the skill has demonstrably internalized —
  # kept as regression insurance rather than deleted, but drawn lightly so the budget is spent
  # at the live edge. 1.0 ignores tiers; full runs (max_cases: null) always score everything.
  archive_weight: 0.1

inputs:
  wiki:
    # Repo context injected per review, when this skill has a wiki/ folder. Paid for on every case
    # of every trial on both sides of a gate, which is why these are small.
    max_pages: 4
    max_bytes: 24000
  precedents:
    # Precedent cases injected per review, when this skill has a committed case index
    # (`whetstone skills index`). Same cost discipline as the wiki caps.
    max_cases: 3
    max_bytes: 8000

judge:
  # Confidence under which a verdict is re-judged grounded in the case's own diff. The judge's
  # question — "same underlying issue at this location?" — is often undecidable from two sentences
  # alone, and the dangerous error is the spurious match that quietly makes a case pass on
  # anything. 0 disables the cascade: no extra calls, and the judge behaves exactly as it always
  # has. Enabling it changes the measurement instrument, so the run's judge identity records it —
  # expect a visible re-baseline seam in score history when you turn it on.
  escalate_below: 0.0
  # Diff shown to the grounded judge, capped like every other per-call input.
  max_diff_bytes: 2000
  # A distilled judge takes the tier-1 calls while tier 2 and the reviewer stay on the run's
  # backend — see judges/default/distill.md. The resolved model folds into the run's judge
  # identity, so a swap re-baselines trends instead of drawing through them.
  # tier1:
  #   llm: ollama
  #   model: judge-distilled

model:
  # Pin this skill to a particular backend. Anything set here is the default; a flag on the command
  # line still wins. Omit a key to inherit whatever the command resolved (--llm / --model /
  # WHETSTONE_LLM). Pinning to a local runner is how a skill stays off a metered API by default.
  # llm: ollama
  # model: qwen2.5-coder:7b
  # base_url: http://localhost:11434/v1
  # effort: high
"""

IMPROVE_STEP = """\
# How a guidance change is drafted from this skill's failures.
#
# Run with: whetstone skills improve --skill <this skill folder>
#
# Whetstone assembles a bounded digest of the last run's failures and renders it into prompt.md.
# The step never reads eval_cases/ itself, which is what keeps this affordable at a corpus of any
# size: it sees representatives of the failure *kinds*, never the failures.

description: Draft a guidance change from the failures of the last run.

inputs:
  failures:
    # How many failures reach the prompt. These are cluster representatives, not the first N —
    # 12 here means twelve different kinds of failure, largest group first.
    max: 12
    # What counts as the same kind of failure. Merging is lossy — only the representative's diff
    # and problem statement reach the prompt, the rest arrive as "and N more like it" — so a
    # grouping is only ever applied where there is real evidence of a shared cause.
    #   rule        the rule id the reviewer cited (default). A miss cites nothing, so a failure
    #               with no rule is its own cluster rather than being merged with every other one.
    #   expectation what the expectation asserts, compared as text
    #   path        the top-level directory, i.e. roughly the subsystem
    #   none        no clustering; representatives are individual failures
    cluster_by: rule
    # Diff shown per failure, in bytes. Raise if your rules need wide context to judge.
    max_diff_bytes: 2000
    # Learn from misses, from false positives, or both.
    outcomes: [fn, fp]
  wiki:
    max_pages: 4
    max_bytes: 24000

# The instructions given to the model. Whetstone supplies the output structure (a complete
# guidance body, a rationale, and the eval case ids the change should fix), so this file only has
# to say how to think about the change.
prompt: prompt.md

# model:
#   llm: ollama
#   model: qwen2.5-coder:7b
#   effort: high

# Instead of `prompt:`, a step may set `run:` to have Whetstone invoke your own program: the
# digest arrives as JSON on stdin, and it must print {"body": ..., "rationale": ...,
# "targeted_cases": [...]} on stdout. Use it when a prompt genuinely will not do — the declarative
# form above is what most skills want.
#
# run: ["python", "run.py"]
"""

IMPROVE_PROMPT = """\
You are tightening the review guidance for `{{skill_id}}`.

Its current recall is {{recall}} and its false-positive rate is {{fp_rate}}, measured over
{{cases_scored}} of {{cases_total}} eval cases. Those cases are real code review outcomes: a human
either flagged this code, or deliberately did not.

The reviewer got {{failure_count}} things wrong. Below are {{shown_count}} of them, one per kind of
failure, largest group first.

{{failures}}

## Current guidance — SKILL.md

{{guidance}}

## Current guidance — companion pages

These are part of the same guidance and reach the reviewer verbatim, under the paths shown. If a
rule you need to change lives here, change it here.

{{pages}}

## Repo context

{{wiki}}

## What to do

Rewrite the guidance so those failures would not recur.

{{instruction}}

- Keep every rule that is already working. You are seeing a sample of failures, not the whole
  picture, and a rule you have no evidence about is still load-bearing.
- Prefer sharpening an existing rule over adding a new one. Guidance that grows a rule per failure
  becomes a checklist no model can apply consistently.
- A false positive usually means a rule needs a stated exception, not deletion.
- A miss usually means a rule is too abstract to recognise the pattern in a diff. Say what the code
  looks like.
- Write rules that a reader can apply to a diff without access to the rest of the repository.
- Fix a rule in the file that holds it. Restating a page's rule in `SKILL.md` leaves two copies to
  disagree with each other, and the reviewer is sent both.

Return the complete new `SKILL.md` body, the complete new text of any companion page you changed
keyed by its path, the rationale for the change, and the ids of the eval cases it is meant to fix.

`{{instruction}}` above is whatever was passed to `--instruction` on this run, and is empty on a
plain run. Move it wherever you want it read; delete it and a passed instruction is appended at the
end instead, so it is never silently dropped.
"""

UPDATE_STEP = """\
# How this skill's repo context is regenerated.
#
# Run with: whetstone skills update --skill <this skill folder> --repo <path to the source repo>
#
# Whetstone does not summarize repositories. This step invokes the generator you already run, then
# indexes what it produces so retrieval can be deterministic. The wiki is part of skill_hash, so a
# refresh that changes any page retracts a passing gate and the skill must be re-gated before it
# can be proposed.

description: Regenerate the repo wiki from the openwiki generator.

# Substituted before the command runs:
#   {{repo}}      the source repository passed with --repo
#   {{out_dir}}   a temporary directory your generator must write into
#   {{skill_id}}  this skill's id
#
# A list of arguments, never a string: nothing is re-split on spaces and no shell is involved, so
# a path with a space in it works and a value from your config can never become two arguments.
run: ["openwiki", "build", "--repo", "{{repo}}", "--out", "{{out_dir}}"]

# Seconds before the generator is killed. Summarizing a large repo is slow; this is not.
timeout_s: 900

# What your generator must leave in {{out_dir}}:
#
#   pages/<name>.md    one markdown file per subject. The first `# heading` becomes its title.
#   index.yaml         which source paths each page describes (see below).
#
# If your generator writes index.yaml itself, delete the `index:` block below — the tool that knows
# which files a page describes is the right place for that mapping to live. If it only writes
# pages, declare the mapping here and Whetstone will write index.yaml for you.
#
# Globs are matched with `**` meaning "any depth" and `*` meaning "one segment", so `src/auth/*`
# does NOT match src/auth/nested/thing.rs. A change retrieves the pages whose globs cover the files
# it touches, ranked by how many of them each page covers.

index:
  - page: example
    paths:
      - "src/**"
"""


TRIAGE_STEP = """\
# How a mined signal becomes an expectation this skill can be judged against.
#
# Used by the console's "Draft it" button beside the Semantic field in Triage.
#
# The miner seeds `semantic` from whatever text sat nearest the signal — the first review comment,
# a tracker summary, or the skill's own finding. Rewriting that into a standalone description of
# the problem is the one irreducible human step in triage, and the one that does not scale. This
# drafts it; a person still accepts, edits or discards every result.

description: Draft an eval case's expectation from the review evidence.

inputs:
  draft:
    # The review thread, capped. Enough to see what was actually objected to; not so much that one
    # unusually chatty merge request costs more than the rest of the queue combined.
    max_comments: 6
    max_comment_chars: 1200
    # The diff for the file the expectation is about, in bytes.
    max_diff_bytes: 2000

prompt: prompt.md

# model:
#   llm: ollama
#   model: qwen2.5-coder:7b
#   effort: medium

# Instead of `prompt:`, set `run:` and Whetstone invokes your own program: the bounded evidence
# arrives as JSON on stdin, and it must print {"semantic": ..., "rationale": ...} on stdout.
#
# run: ["python", "draft.py"]
"""

TRIAGE_PROMPT = """\
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
"""


def scaffold_files() -> dict[str, str]:
    """The starter step folders, as relative path → contents.

    Nothing is interpolated: `{{skill_id}}` and friends stay as literal placeholders, because the
    prompt is rendered per run against a live digest, not frozen at scaffold time.
    """
    return {
        f"evaluate/{STEP_FILE}": EVALUATE_STEP,
        f"improve/{STEP_FILE}": IMPROVE_STEP,
        "improve/prompt.md": IMPROVE_PROMPT,
        f"triage/{STEP_FILE}": TRIAGE_STEP,
        "triage/prompt.md": TRIAGE_PROMPT,
        f"update/{STEP_FILE}": UPDATE_STEP,
    }


def write_scaffold(skill_dir: str | Path, *, force: bool = False) -> list[str]:
    """Write the starter steps into a skill folder. Returns the paths written.

    Existing files are never overwritten without `force`: these are edited by hand after the first
    generation, and a scaffold command that silently reverted someone's improvement prompt would be
    a very expensive convenience.
    """
    root = Path(skill_dir)
    written: list[str] = []
    for relative, content in scaffold_files().items():
        path = root / relative
        if path.exists() and not force:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    return written
