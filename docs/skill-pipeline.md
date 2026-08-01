# The skill pipeline: `evaluate/`, `improve/`, `update/`

Every skill can carry its own scripts for keeping itself sharp. They live in the skill folder, next
to the guidance they serve:

```
skills/<id>/
  SKILL.md           the rules the reviewer applies
  meta.yaml          owner, references, rule → signal provenance
  eval_cases/        the promoted cases that constrain the rules
  wiki/              repo context the reviewer reads at review time   (generated)
  evaluate/step.yaml how this skill is scored
  improve/step.yaml  how a guidance change is drafted from failures
  update/step.yaml   how the wiki is regenerated
```

`whetstone skills scaffold --skill skills/<id>` writes correct starter versions of all three. The
generated files carry their own documentation — every setting is present with its default and a
comment saying what changing it costs — so the fastest way to learn this format is to generate it
and read it.

None of the three folders is required. A skill without them behaves exactly as it did before they
existed.

---

## Why the host owns the budget

A skill mining a real MR stream does not stay small. At ten thousand promoted cases a single
`eval run` is twenty thousand model calls, and the failures from one run would not fit in any
prompt. The naive fix — hand the improve step the first N failures — is worse than useless, because
the first N alphabetically are usually N instances of the same problem.

So the division is:

- **The skill declares policy.** How to cluster failures, how many to look at, what to say to the
  model, which generator produces its wiki.
- **Whetstone enforces the budget.** It assembles the digest, clusters it, truncates the diffs,
  caps the wiki, and only then renders the prompt.

A step never walks `eval_cases/` because it is never given the chance to. That is what makes this
work identically at forty cases and at a hundred thousand — and it means a step author cannot get
the scaling wrong, because the scaling is not theirs to get wrong.

Nothing is dropped silently. The digest always reports the true failure count alongside the number
shown, and the preflight reports any wiki page the caps excluded.

---

## Before anything spends money

Every command that reaches a model prints what it is about to do and asks:

```
This step will launch LLM interactions, which might involve cost based on your configuration.

  step      eval gate
  backend   anthropic (this backend bills per call)
  model     claude-opus-4-8
  estimate  up to 12 LLM call(s)
            3 case(s) x 1 trial(s) x (1 review + up to 1.0 judge calls); judging stops at the
            first match, so real runs usually cost less
  note      both base and candidate are scored, so this is doubled
  warning   wiki caps omit 2 matching page(s) on at least one case (payments, ledger)

Proceed? [Y/n]:
```

- The estimate is an **upper bound**. Judging short-circuits at the first matching finding, so real
  runs usually come in under it.
- "Billed" is three-state: `local` for Ollama and friends, `billed` for Anthropic and OpenAI, and
  `custom endpoint — Whetstone cannot tell` for anything else. Guessing "free" for the third case
  is the guess that costs money.
- A local backend skips the prompt entirely; nothing can bill.
- **`--yes` skips the confirmation.** CI needs it. Without a confirmation and without `--yes`, the
  command aborts rather than assuming consent.
- The banner goes to stderr, so `--json` on stdout stays machine-readable.

`[runs] max_llm_calls_per_run` in `whetstone.toml` adds a warning line when the estimate exceeds it.
It warns rather than refuses, because the estimate is an upper bound and refusing on it would block
runs that would have come in under budget.

---

## `evaluate/step.yaml` — how this skill is scored

Configuration, with one seam. There is no `prompt:` here — the reviewer prompt is the harness's, not
yours — but a skill that needs a reviewer the harness cannot be can name its own program under
`run:` (see [Bring your own reviewer](#bring-your-own-reviewer)). Everything else is settings.
`whetstone eval run` and `whetstone eval gate` read this file and use it as their defaults; any flag
on the command line wins.

```yaml
description: Score this skill against its promoted eval cases.

trials: 1                 # reviewer passes per case; multiplies the cost of every run and gate

sample:
  max_cases: null         # null scores everything; a number caps it
  seed: 0                 # selection is a hash of this and the case id
  stratify: true          # draw proportionally from each case kind
  holdout_fraction: 0.2   # share of cases scored but never shown to the improve drafter
  archive_weight: 0.1     # share of its proportional draw an archived (tier: archive) case keeps

inputs:
  wiki:
    max_pages: 4          # repo context per review, when the skill has a wiki/
    max_bytes: 24000

judge:
  escalate_below: 0.0     # 0 = off. >0 re-judges low-confidence verdicts grounded in the case diff
  max_diff_bytes: 2000

model:
  llm: ollama             # pin this skill to a backend; omit a key to inherit the command's
  model: qwen2.5-coder:7b
  effort: high
```

### Skills that produce work, not findings

Everything above scores a skill by what it *reports* about a change. A skill that writes tests,
drafts a migration or refactors a module produces **work**, and is graded by checking that work —
running the tests, applying the migration — not by asking a judge whether two sentences mean the
same thing. Turn that on with `task:`:

```yaml
task:
  enabled: true
  max_steps: 20
  verify:                                        # how every case is graded, by default
    command: ["{python}", "-m", "pytest", "-q"]
```

The corpus moves from `eval_cases/` to `task_cases/`, laid out the same way:

```
task_cases/covers-refund-error/
  case.yaml      id, instruction, and this case's own `verify:` (overrides the default)
  files/         the workspace this case starts from — real source, not YAML-embedded strings
  change.diff    optional: the change the task is about
```

Each case runs in a **fresh workspace** seeded from `files/`. The agent gets `write_file`,
`read_workspace_file` and `list_workspace` on top of its usual tools, and finishes by calling
`submit_work`. Files it did not write do not exist — describing a change is not making one.

**Grading.** `command:` runs in the workspace and its exit code is the verdict. `run:` instead hands
grading to a program the skill ships, for quality an exit code cannot express (stdin gets the case,
workspace path and output; stdout returns `{passed, score, metrics, detail}`).

`{python}` expands to the interpreter Whetstone is running under, and `{workspace}` to the case
directory — a committed command line otherwise depends on whatever `python` happens to mean on the
machine.

**Partial credit matters.** A command may print a trailing JSON object (`{"score": 0.8, "metrics":
{…}}`) to report *degree*. The exit code still owns the verdict, so a grader cannot pass itself by
printing `1.0` — but without a degree a gate can only see whole cases flip, and most real progress
is smaller than that.

**Scoring and gating.** `pass_rate` is what a person asks; `mean_score` is what the gate compares,
with the same discipline as the review gate — a tolerance, and `targeted` cases that must actually
be fixed rather than merely not broken. A case whose *executor* crashes is recorded with its error
and scores zero, so one malformed case cannot lose a corpus of two hundred; a case whose *grader*
crashes stops the run, because scoring it as a failure would blame the skill for a broken grader.

**Running one.** Task skills have their own commands, because the review path cannot score them:

```
whetstone eval task --skill skills/test-writer            # score; --keep to inspect workspaces
whetstone eval task-gate --base <dir> --candidate <dir>   # regression gate, --targeted <case-id>
```

`whetstone eval run` **refuses** a task skill rather than scoring its (empty) `eval_cases/` — that
path would report a flawless run over nothing.

In the console, a task skill grows a **Tasks** tab: the cases with what each starts from and how it
is graded, buttons to run or gate them, and the run history. It names both instruments separately —
the agent that does the work and the verifier that grades it — because a task score means nothing
without both. The inbox routes such a skill to its own actions rather than to the review gate, and
the header's *Run evals* is suppressed there: it could only ever be refused.

**Records.** A task run lands in `.whetstone/task-runs/` and a task gate in `.whetstone/task-gates/`
— beside the review ones, never among them, so two incomparable kinds of score never share a
listing. The gate record is what C6 reads before a task skill's change may be published, computed
by the same verdict function the review gates use. `whetstone skills trend --skill <id>` reads both.

`task:` and `agent:` are mutually exclusive — a skill is scored one way.

### Run the skill as an agent

A skill is a folder, and `SKILL.md` refers to the rest of it the way a person would — *"see
[principles.md](references/principles.md)"*. The built-in reviewer cannot follow a link, so it
concatenates: every markdown file in the folder goes into one prompt, on every case, whatever the
change touches. Turn this on instead and the folder is **run**:

```yaml
agent:
  enabled: true
  max_steps: 12                                     # investigation budget per review
  source: { env: SERVICE_REPO, required: true }     # optional: read the code too
  tools:                                            # optional: whatever else it needs
    - name: jira_issue
      description: "Fetch a Jira issue by key."
      run: ["python", "tools/jira.py"]
      input_schema:
        type: object
        properties: { key: { type: string } }
```

What changes:

- **`SKILL.md` becomes instructions**, not prompt filler.
- **The other pages are read on demand.** The agent is told they exist and fetches one with
  `read_skill_file` when its own instructions point at it. A `README.md` written for humans stops
  being fed to the model as rules.
- **`source:` adds `read_file`, `grep` and `list_dir`**, read-only and sandboxed to that root —
  every path resolved and checked to be inside it, symlinks included. A root that is set but is not
  a directory is refused at the plan, because every tool would answer "no such file" and the agent
  would review having opened nothing while the run looked normal. `grep` prunes dot-directories and
  the usual vendor/build trees (`node_modules`, `target`, `dist`, `__pycache__`, …), skips binaries
  and files over 1 MB, and caps the walk — an unpruned search of a real checkout took 42s per call.
- **`tools:` are programs the skill brings.** Whetstone offers them to the model and runs them on
  request; it never learns what any of them do. They get `{"arguments": …, "context": …}` on stdin
  and their stdout goes back to the model. A tool that fails returns its error *to the model*,
  which can then try something else.

**Configuration: the `context:` block.** Anything a skill needs and Whetstone knows nothing about —
a token, an API base URL, a schema file — is declared alongside `agent:` (or `task:`) and resolved
the same three ways as a reviewer program's context (`env:` / `file:` / a literal):

```yaml
agent:
  enabled: true
  tools:
    - { name: jira_issue, description: "Fetch an issue.", run: ["python", "tools/jira.py"] }
context:
  jira_token: { env: JIRA_TOKEN, required: true }   # never committed; never shown to the model
  jira_base: https://jira.internal
```

The resolved bag reaches the skill's **tools** on stdin, secrets included — that is what a script
calling Jira needs. What the *model* is shown is the redacted view, so `jira_token` appears in the
prompt as `<env:JIRA_TOKEN>` and the credential never enters a prompt or a transcript. A required
variable that is unset is reported at the plan, before anything is spent.

**It cannot get stuck waiting for a person.** A skill written for interactive use will say things
like "ask clarifying questions"; there is nobody to ask. Four guards, each independently tested: no
tool exists for asking, the runtime preamble says so, a turn that calls no tool costs a step and
gets nudged, and at `max_steps` the answer is *forced* with only the terminal tool on offer. The
agent finishes by calling `submit_findings` — the sole way to end — so nothing has to parse prose.

**Nor can one bad case lose a run.** A reviewer that cannot answer a case at all — a model that
refuses even when forced, a backend that rejects tools — is recorded as an *unscorable case* and the
run continues. It contributes no confusion counts, so recall is computed over the cases that were
actually measured; `errors` and `scorable` report how many of each there were.

Three rules keep that from becoming a lie, because an empty confusion reads as `recall 1.0` — the
right convention for "there was nothing to catch here" and the worst possible one for "we never
found out":

- an unscorable case never counts as **passed**, so a `--targeted` case that could not be run is
  reported as unfixed rather than as fixed, and a case that stopped being scorable reads as a
  regression;
- the gate refuses outright when *either* side scored no cases at all, rather than comparing two
  perfect-looking scores computed over nothing;
- the gate blocks a candidate that produced more unscorable cases than its base.

**Cost.** Unlike a reviewer program, an agent spends *your* backend: up to `max_steps` investigation
calls per review **plus one forced answer**, so the plan states `(max_steps + 1) × cases × trials ×
sides` as a ceiling. Agents normally answer well short of it. Those calls are counted on the run and
on the gate record, not just estimated.

**Determinism.** An agent is a less fixed instrument than one call. Both gate sides share one
instance, and each run records its **trajectory** — what it actually opened. `whetstone runs show`
prints it, a gate prints both sides, and when they differ (`trace_diverged`, on the record and in
the console) the delta is flagged as not purely the guidance.

`agent:` and `run:` are mutually exclusive: one runs the skill inside Whetstone, the other hands
reviewing to your program.

### The same everywhere: improve and triage run as agents too

`agent:` is **not** an evaluate-step setting. It is valid on every step whose work is the skill
thinking — `evaluate`, `improve` and `triage` — and it means the same thing on each: `SKILL.md` is
the instructions, the folder's other pages are fetched on demand, `source:` and `tools:` are in
reach, and the step finishes by calling a terminal tool.

This matters most on `improve`. The drafter is asked to rewrite guidance so a set of failures stops
recurring, and until it could run as an agent it was a single call that had never read the code
those failures were about, could not open the ticket the case came from, and was handed every
companion page pasted into its prompt — the exact treatment `agent:` exists to replace. A skill
that is a folder on one step and a wall of text on the next is two different things.

```yaml
# improve/step.yaml — the prompt is still required, and now means something different
prompt: prompt.md
agent:
  enabled: true
  source: { env: SERVICE_REPO, required: true }
  tools:
    - { name: jira_issue, description: "Fetch an issue.", run: ["python", "tools/jira.py"] }
context:
  jira_token: { env: JIRA_TOKEN, required: true }
```

**The prompt is the task; the skill body is the instructions.** Same split the reviewer has: its
`SKILL.md` becomes the system prompt, and the step's rendered prompt — the failure digest, or the
candidate's diff — is what it is being asked to do. So an agent step still needs its `prompt:`.

| step | terminal tool | what it returns |
| --- | --- | --- |
| `evaluate` | `submit_findings` | located findings, judged as always |
| `evaluate` + `task:` | `submit_work` | files in a workspace, graded by a verifier |
| `improve` | `submit_guidance` | the complete new body, plus any pages it rewrote |
| `triage` | `submit_expectation` | the expectation a candidate should become |

`update` is the one exclusion: it invokes the wiki generator your deployment owns via `run:`, so
there is no skill judgement in it to give tools to.

Everything downstream of the answer is unchanged — the same echo-stripping, the same dropping of
targeted case ids the drafter was never shown, the same page filtering. What changes is how the
answer was reached.

**Cost.** An agent improve step costs up to `max_steps + 1` calls instead of one, and every plan
says so before you spend. `agent:` is opt-in, so a step without it keeps the single call it has
today.

### Bring your own reviewer

The built-in reviewer is one structured model call: guidance and wiki in, findings out, no tools. It
cannot open a file. For most skills that is the right shape — but a code-review skill over a large
repository often cannot decide anything from the diff alone, because whether a call is dangerous
depends on the *called* function, which is not in the change. Such a skill can replace the reviewer
with a program of its own:

```yaml
run: ["python", "reviewer.py"]     # instead of the built-in reviewer; no `prompt:` here

context:                            # whatever your program needs — Whetstone forwards, not interprets
  source_root: { env: HUB_REPO_ROOT, required: true }   # machine-local: commit the name, not the path
  source_ref:  { env: HUB_REPO_REF, pin: true }         # a pinned sha: recorded in full, and hashable
  db_schema:   { file: ./references/schema.sql }        # committed material, read and hashed by content
  project: hub-backend                                  # a literal (scalar, list, or plain mapping)
```

**Whetstone does not give a model filesystem access — it gives a *program* a folder path.** Your
program is where the agent lives: it reads `context.source_root`, opens whichever files it needs,
runs its own model with its own tools, and answers. Whetstone keeps doing what it did before:
picking cases, judging findings, scoring, and gating.

One review is one invocation. The payload arrives as JSON on stdin, the working directory is the
step folder, `run:` is an argv list (no shell), and `timeout_s` is a hard cap:

```jsonc
{ "skill_id": "…", "guidance": "…SKILL.md body…", "pages": { "…": "…" },
  "change": { "repo": "…", "base_ref": "…", "head_ref": "…", "files": [ … ] },
  "diff": "…numbered unified diff…",
  "context": { "source_root": "/…/hub-backend", "project": "hub-backend", … },
  "wiki": "…retrieved pages, if any…", "limits": { "timeout_s": 900 } }
```

Print the same findings contract the built-in reviewer produces, on stdout:

```json
{ "findings": [ { "path": "…", "line": 123, "severity": "warning",
                  "message": "…", "rule_id": "R1", "confidence": 0.8 } ] }
```

`line` is a line in the **new** file. Scoring is unchanged from the built-in reviewer's: a finding
only satisfies an expectation if it is structurally eligible first — same file, inside the
expectation's `line_range` if it declares one, meeting `severity_min` if it declares one — and only
then is it put to the judge. So a program that reports the right problem at the wrong line scores as
a miss, exactly as the built-in reviewer would. A crash, a non-zero exit, unparseable output, or a
timeout **fails the run** — a score computed with cases the reviewer silently errored on is not a
measurement.

**The context bag has three value forms**, and the difference between them is about what is safe to
commit, print, and hash:

| Form | Resolved from | In a plan / record | Hashed |
|---|---|---|---|
| `{ env: NAME }` | the environment | `<env:NAME>` — never the value | no |
| `{ env: NAME, pin: true }` | the environment | shown in full | **yes** |
| `{ file: ./path }` | a file in the skill folder | `<file:./path>` | **yes**, by content |
| a bare literal | the declaration itself | shown in full | **yes** |

`required: true` on an `env:` form makes the console refuse the run **at the plan** when the variable
is unset, rather than three cases in. Machine-local paths are deliberately not hashed, so a shared
gate survives a teammate whose checkout lives elsewhere; a pinned ref *is*, because it determines
what the reviewer reads. Values from the environment never reach a stored record — only their names.

**Where it runs.** Everywhere the reviewer runs: `eval`, both sides of a `gate`, the saturation
probe, and a live `review` — from the console and the CLI alike, resolved once so the two cannot
disagree about which reviewer a skill uses.

**What it costs.** Whetstone makes no review call at all, so the estimate counts only judge calls.
It cannot price your program's calls, so it counts them instead: the plan states how many times your
program will be invoked (cases × trials × gate sides), and the cost is that many invocations at
whatever each one spends.

**Determinism.** A gate blames a score difference on the guidance, which holds only if everything
else the reviewer saw was identical on both sides — and a reviewer reading a *moving* source can
break that. One reviewer instance serves both sides, so guidance is genuinely the only variable on
Whetstone's side; but the source is not yet part of `skill_hash` (see ADR-022), so the plan **warns**
when a custom reviewer is gated. Pin the snapshot with `{ env: …, pin: true }` and have your program
read that ref.

Worked, runnable example: `examples/agentic-reviewer/` (`uv run python examples/agentic-reviewer/serve.py`).

### The judge cascade

The judge decides whether a reviewer finding and an expectation describe the same underlying
issue, and from two sentences alone that is often undecidable — the dangerous error being the
spurious match, which silently turns a case into one that passes on anything. With
`escalate_below` set, a verdict whose confidence falls under the threshold is re-judged with the
case's own diff in the prompt: the code both sentences point at, frozen inside the case, so both
gate sides still see identical context. The drill-down records both tiers ("tier 1 first said…"),
and the cost banner's judge share doubles to stay an honest upper bound — real runs escalate only
the unsure minority.

Enabling, disabling, or re-tuning the cascade changes the measurement instrument, so it folds
into the run's recorded judge identity: expect the score-history seam the console draws at any
judge change. It deliberately does **not** enter `skill_hash` — steps configure how things run,
not what the reviewer reads — so editing the threshold retracts no gate.

The `model:` block is how a skill stays off a metered API by default. `eval run`, `eval gate` and
`skills improve` all read it, and `--llm` / `--model` / `--base-url` / `--effort` override it per
command — the skill sets the default, the operator running it gets the last word.

### Sampling, and why a sampled gate is still legitimate

Selection is `sha256(seed:case_id)`, never a random draw and never iteration order. Three things
follow:

- **Base and candidate see one identical draw.** The gate samples the *union* of both sides' cases
  once and hands the same set to each, so a score difference is still attributable to the guidance.
- **Re-running reproduces the result.** The same corpus and seed always draw the same cases, on any
  machine and any Python build.
- **`--targeted` cases are exempt.** A change asserting it fixes case X is always scored on X,
  whatever the draw says. If you name more targeted cases than the budget allows, all of them are
  scored anyway — silently dropping one would fail the gate for a reason nobody could see.

`stratify: true` allocates the budget across `should_catch` / `should_not_flag` proportionally.
Turn it off and a sample of a corpus that is 90% positive will sometimes contain no negative cases
at all — and a false-positive rate measured over zero negative cases is a flattering zero.

Override per run: `--sample 200 --sample-seed 7`.

### The holdout partition

`skills improve` reads failures from the same corpus the gate then scores — train equals test,
structurally — so over many improve cycles the score is guaranteed to climb faster than real
capability: the drafter is shown the answers. `holdout_fraction` holds a slice of cases out of
that loop: they are **still scored on every run and gate**, but their failures never reach an
improve digest (the digest says how many were withheld), a proposal may not name them in
`targeted_cases`, and the gate refuses `--targeted` ids that fall in the partition. Scores are
reported per partition, and train running ahead of holdout is the overfitting alarm — the moment
to promote fresh cases rather than polish further.

Membership is an unseeded hash of the case id: stable forever, on every machine, with deliberately
no way to re-roll it — a seed would offer exactly the workaround the partition exists to prevent.

**The exam is the graduated corpus.** A case waiting under `promoted_cases/` is on the train side
while it waits, whatever its id hashes to. That folder is where an operator decides whether a mined
case has earned a place — scoring it, sharpening against it, rewriting its expectation — and every
one of those is a use the blindfold forbids. Applying the hash there meant that a fifth of
everything ever mined could never be sharpened against, permanently and at random, when sharpening
against it is the entire reason to promote it. The only escape was setting `holdout_fraction: 0`,
which switches the alarm off for the whole skill to unblock one case.

**A case the drafter has read is recorded as such.** When an improve step is actually shown a case
whose id hashes to the holdout, `partition: train` is written into its `case.yaml`. That survives
graduation, which is a folder move — without it, a case the model has read would go back to the
hash on graduation and be counted as an exam question it passed unseen, *flattering* the one number
whose job is to be unflattering. You can write the same line by hand to say "this case is for
teaching": it is the one exception to a partition nobody can re-roll, and it is deliberately a
visible line in a reviewable diff rather than a setting. It is also one-way in practice — nothing
un-reads a case.

`partition` is excluded from `skill_hash`: both sides of a gate score a case whichever side it is
on, so it changes nothing a gate measures, and including it would revoke every stored gate verdict
the moment the field landed.

### Case tiers: active and archive

A corpus mined from a live MR stream only grows, and deterministic sampling gives every case an
equal draw forever — so an ever-larger slice of each run re-verifies what the skill demonstrably
internalized long ago, while the aggregate score gets more flattering. A case can carry
`tier: archive` in its `case.yaml` (absent means `active`): archived cases stay in the corpus as
regression insurance but draw at `archive_weight` of their proportional share in sampled runs.
Full-corpus runs (`max_cases: null`) score everything at full weight regardless — that is the
posture for a periodic everything-still-holds pass.

Nothing archives a case automatically. The console proposes retirement when a case has passed its
last ten gate appearances across several skill versions, with the evidence spelled out; a person
confirms, and the flip lands as a one-line commit on the skill's staging branch. Because a
rewritten case changes `skill_hash`, the archived corpus needs a fresh passing gate before it can
be proposed — de-weighting a case can move the score, and a moved score gets re-proven.

Cases can also *arrive* archived. The triage screen compares each candidate against the skill's
existing cases (same source MR, same file, overlapping expectation words — lexical on purpose,
and computed only at triage load, never near the review path) and surfaces the resemblance with
the two expectations side by side. When similars exist the promote button gains a third
disposition: **promote to archive** — for the duplicate that is still worth counting as
regression insurance rather than re-verified at full weight forever, or thrown away. Nothing is
auto-rejected: the ninth unwrap case in a new subsystem may be exactly the promotion you want.

### The saturation probe

A case can stop discriminating two ways the pass-rate cannot tell apart: the guidance genuinely
internalized the lesson (good — retire it), or the expectation is so loose anything matches (bad —
the case is dead but looks alive). `whetstone eval baseline` separates them: it scores every
active case through the normal harness with the guidance stripped — no body, no pages, no wiki. A
`should_catch` case the *naked* model passes never measured the guidance either way, and the
console flags it for a human to tighten or retire.

The record is stored as a `baseline` variant: queryable history, but excluded from every default
listing — a deliberately-blinded run must never surface as "the latest run" in a trend, an inbox
row, or an improve digest. The health panel's Discrimination section reads the newest probe
("N of M active catch cases still measure the guidance"), each case page shows its verdict, and
flagged cases appear in the inbox as curation proposals. A diagnostic sweep, not a gate — run it
monthly, on a local model if you have one.

### The drift probe

The one rot vector nothing above can see: the holdout catches overfitting *to the corpus*, the
saturation probe catches dead cases *in the corpus* — neither notices when the codebase moves on
and the entire corpus tests last year's idioms. `whetstone corpus drift` measures it directly: it
embeds the diffs of the skill's active cases and the diffs of the recent merge-request stream
(read from the candidate queue that `corpus pull` and the watcher already maintain — no forge
round-trips), and reports two numbers. **Coverage** is the actionable one: the fraction of recent
MRs with an active case within a similarity radius, and the uncovered list names exactly which
MRs look like nothing the skill is tested on — each links into triage, because promoting a
candidate from one is what moves the number. Centroid distance is the trend companion: the middle
of the stream moving away from the middle of the corpus.

Embeddings are banned from scoring (the review path must be a pure function of the diff) and
allowed here, deliberately: drift runs after the fact, feeds no reviewer, and produces evidence
for a human. It needs an embedding model — a local one is the intended backend:

```toml
[drift]
embed_provider = "ollama"          # any OpenAI-compatible preset
embed_model = "nomic-embed-text"   # `ollama pull nomic-embed-text` first
```

Vectors are cached by content hash under the drift directory, so a re-probe embeds only what
changed. Reports are stored; the health panel's Drift section shows the latest with its trend,
and when the uncovered fraction crosses the threshold the inbox says so — "corpus drift: 40% of
recent MRs look like nothing in the corpus" — ranked below failing cases, above housekeeping.
Quarterly is plenty; it is also a button on the Health tab.

### Synthetic cases: counterfactuals and mutation probes

`whetstone corpus synthesize --skill skills/<id> [--counterfactual] [--mutate]` grows the corpus
without waiting for the next incident — into the **triage queue, never the corpus directly**: a
person rules on every synthetic candidate exactly as on a mined one.

**`--counterfactual`** attacks the corpus's structural positive-heaviness. A corpus mined from
defects has few negatives, and an fp_rate over zero negative cases is a flattering zero.
Reversing a `should_catch` case's diff yields the defect being *removed* — the highest-grade
negative obtainable, since flagging the fix for the very defect the parent documents is a false
positive on the exact pattern the rule targets. Mechanical; no model call.

**`--mutate`** attacks instance-memorization, which the holdout cannot see: a rule that names
variables from one incident passes that incident forever while missing every recurrence. A model
drafts the same defect wearing different names — identifiers renamed, context restructured,
defect preserved — and every draft is validated before it may enter the queue: it must parse as
a diff, must add lines the parent's expectation can anchor to, and must actually differ from the
parent. Invalid drafts are skipped and reported, never queued.

Both carry `provenance.source: synthetic-counterfactual | synthetic-mutation` with `ref` pointing
at the parent case, so every corpus statistic can tell them apart: the composition block counts
synthetic vs mined, the precision-evidence mix gives them their own bucket (generated evidence
never reads as human-confirmed), and the drift probe excludes them from the recent-MR stream.
Synthetic parents are refused — the chain always stays one step from real evidence. Both
generators are buttons on the Health tab's Corpus section, and both jobs answer with what was
written and what was skipped, with reasons.

### The case index: precedents at review time

Without retrieval, every lesson the corpus holds must pass through improve cycles into guidance
prose — a lossy distillation with one-full-loop latency, and the direct cause of guidance bloat.
`whetstone skills index --skill skills/<id>` inverts it: every active case's diff is embedded with
a **pinned** model and committed as `index/manifest.yaml` + `index/vectors.json`. At review time
the incoming change is embedded with that same model and the nearest cases are injected after the
guidance and wiki, labelled as **precedent, not rules** — both kinds, because a stay-silent
precedent teaches restraint that prose is notoriously bad at encoding. A case promoted this
morning sharpens this afternoon's reviews with zero improve cycles.

Why this is admissible when `wiki.py` bans embeddings from retrieval: the objection is that
retrieval must be a pure function of the diff so both gate sides see identical context. A pinned
model over a versioned, committed index satisfies exactly that — and the manifest's digest folds
into `skill_hash`, so rebuilding the index **retracts gate evidence** exactly as a wiki refresh
does (C6): re-gate before proposing. The console's rebuild job stages the index on the skill's
branch, never the working tree. A skill without an index hashes and reviews exactly as before the
feature existed.

Guardrails: a case is never its own precedent (at eval time the query diff *is* the case diff, and
retrieval would otherwise hand the reviewer the answer key); the saturation probe strips the index
along with the guidance; caps live in `evaluate/step.yaml` under `inputs.precedents` (same
discipline as the wiki caps) and the preflight names the per-case embedding cost. Each live
review's record lists which precedents it saw — findings become explainable as "flagged like
case-X was" — and the Health tab's index card shows the pinned model, the case count, and which
active cases are not yet retrievable (promoted or edited since the last build).

### Distilling the judge

Judge calls are the largest cost line — they scale as cases × trials × both gate sides — and a
near-free tier 1 is what makes the *unsampled* full-corpus run affordable weekly instead of
quarterly. `whetstone judge export` writes every recorded verdict as training triples (finding,
expectation, grounding diff → verdict), filtered to one judge identity so the student never
learns from an instrument nobody ran; escalated verdicts carry the tier-1 call the grounded
teacher corrected — the hard negatives worth oversampling. The fine-tune happens outside
Whetstone (`judges/default/distill.md` is the recipe); validation is `whetstone judge eval --llm
ollama --model <distilled>` against the ratcheted bar, and a model that has not cleared it does
not deploy.

Deployment is two lines in `evaluate/step.yaml`:

```yaml
judge:
  escalate_below: 0.8
  tier1:
    llm: ollama
    model: judge-distilled
```

The student takes the bulk pairwise calls on its own client; the reviewer and the grounded tier 2
stay on the run's backend, and unsure verdicts still escalate to the teacher. The resolved tier-1
model folds into every run's `judge_hash` — a swapped judge is a different instrument, and trends
break at the seam instead of drawing through it. The Judge page shows the escalation rate over
recent runs: the number a distilled tier 1 has to keep honest. Rollback is deleting the block.

### The operating cadence

Everything above fires on evidence — a failing case, an uncovered MR, a saturated expectation.
Entropy is the exception: improve cycles add rules weekly and nothing else ever removes one, so
the passes that manage it run on a clock. Four clocks per skill, visible on the Health tab and in
`whetstone cadence status --skill skills/<id>`:

- **Guidance distill pass** (monthly) — `skills improve --instruction "consolidate; merge
  redundant rules, remove subsumed ones, shorten"`, no targeted cases, gated over the full corpus
  *including the archive at full weight* (`max_cases: null` for the occasion) — archived cases
  are the tripwires against over-aggressive deletion. The one hand-marked clock (`whetstone
  cadence done --skill …`, or the button on the Health tab), because a distill is an ordinary
  improve run and nothing in its record distinguishes it.
- **Saturation probe** (monthly) and **drift probe** (quarterly) — reset themselves; the probes
  record their own runs.
- **Full-corpus anchor run** (quarterly) — sampled scores are estimates, and estimates need
  ground-truthing. Read from the run store: the newest real run whose draw covered every
  currently-active case. Judged against the corpus *as it is now*, so a promotion restarts the
  clock — which is the right reading.

When a clock is overdue and nothing evidence-backed is pending, the inbox says so ("guidance
distill pass due — last done 47 days ago") — a cadence that lives in a document is a cadence that
dies in the document. A clock that has never fired starts counting from the skill's first real
run, so a day-one skill owes nothing.

Before a distill pass, read the dead-rule report (`whetstone skills rules --skill skills/<id>`,
or the Health tab): it crosses `meta.yaml` rule→signal provenance with the corpus and lists rules
that could vanish without any case going red — the guidance no longer mentions them, their
supporting cases are all archived, or no case carries their evidence at all. That list is what
turns distillation from "the model shortened some prose" into an evidenced removal list.

---

## `improve/step.yaml` — drafting a guidance change

```yaml
description: Draft a guidance change from the failures of the last run.

inputs:
  failures:
    max: 12               # cluster representatives, not the first 12
    cluster_by: rule      # rule | expectation | path | none
    max_diff_bytes: 2000  # diff shown per failure
    outcomes: [fn, fp]    # learn from misses, false positives, or both
  wiki:
    max_pages: 4
    max_bytes: 24000

prompt: prompt.md
```

Run it:

```bash
uv run whetstone skills improve --skill skills/<id> --apply     # stage it, ready to gate
uv run whetstone skills improve --skill skills/<id> --dry-run   # see the prompt; no model call
uv run whetstone skills improve --skill skills/<id> \
  --instruction "focus on false positives in test files"
```

It reads the skill's most recent stored run (`--run <id>` to pick another), builds the digest, and
returns a complete rewritten guidance body, a rationale, and the eval case ids the change is meant
to fix.

### `--apply`, and why you want it

`--apply` stages the proposal on `whetstone/skill/<id>` — the same branch the console's guidance
editor writes to, through the same `prepare_guidance` path. That means the frontmatter is preserved,
the version is bumped once per proposal, the result is validated by loading it back, and your
working tree is untouched. It then prints a gate command you can run **as printed**:

```
staged v2 on whetstone/skill/code-review-rust-error-handling (5ae78876bb)

gate it, then Propose MR in the console unlocks:
  whetstone eval gate --repo . --skill-path skills/code-review-rust-error-handling \
    --base-ref main --candidate-ref whetstone/skill/code-review-rust-error-handling \
    --targeted unwrap-in-handler
```

Without `--apply` you get the raw body on stdout (or in `--out`) and the job of splicing it into a
`SKILL.md` yourself. **That is a body, not a file.** Overwriting `SKILL.md` with it drops `id`,
`version` and `triggers` — the id then falls back to the folder name, and a gate run on that folder
records its evidence under a skill that does not exist, which C6 can never match. The command says
so when you use `--out`, but `--apply` is the path that cannot go wrong.

### The run has to describe the skill you have

If the skill has been edited since the run was scored, `skills improve` refuses:

```
run 2026…-6d01a7 scored a different version of this skill (33d4959ad7, now 68c51c5159).
Its failures describe a reviewer that no longer exists — the guidance, the eval cases or
the wiki changed since. Re-run `whetstone eval run` first, or pass --stale-ok.
```

Improving from stale failures produces a confident proposal aimed at a problem that may already be
fixed. `--stale-ok` proceeds anyway when you know better.

A run with no failures at all does not spend a call either — there is nothing to learn from. Pass
`--instruction` if you want the guidance rewritten regardless.

### Clustering is the whole point

Failures are grouped by cause and one representative is taken per group, largest group first. Twelve
failures chosen that way are twelve *different things wrong with the guidance*; twelve chosen by
slicing are usually one thing said twelve times. Cluster size is also the best available proxy for
what a rule change is worth, which is why the biggest group is what the model reads first.

Merging is lossy: only the representative's diff and problem statement reach the prompt, and the
rest of its group arrives as "(and N more like it)" — no diff, no statement, not even an id. So a
key may only ever be something that *means the same thing across cases*. Where no such thing is
available the failure stands alone, because inventing a shared cause hides real evidence.

That rule is why `rule` no longer falls back to the expectation id. Expectation ids are per-case
ordinals — `promote.prepare` writes exactly one expectation per triage case and always names it
`e1` — and a miss cites no rule, so the fallback keyed every promoted case in a corpus to the
constant `fn:e1` and folded them into one cluster. Ten curated cases handed to a drafter produced
one diff and a claim that the other nine were "like it".

`cluster_by` options:

| value | groups by | use when |
|---|---|---|
| `rule` | the rule id the reviewer cited; a failure citing none is its own cluster | default; rules are the thing you are editing |
| `expectation` | what the expectation asserts, compared as text | expectations map cleanly to distinct behaviours |
| `path` | the top-level directory | failures track subsystems more than rules |
| `none` | nothing; representatives are individual failures | you want every failure shown, whatever the cause |

### Template variables

`prompt.md` is rendered with `{{name}}` substitution. An unknown placeholder is an **error**, not an
empty string — a prompt saying `{{failurs}}` would otherwise render as literal text and the model
would cheerfully improve a skill it was shown nothing about.

| variable | is |
|---|---|
| `{{skill_id}}` | the skill's id |
| `{{guidance}}` | the current `SKILL.md` body |
| `{{failures}}` | the rendered cluster representatives |
| `{{failure_count}}` | how many failures there really were |
| `{{shown_count}}` | how many reached the prompt |
| `{{cases_total}}` | eval cases the skill has |
| `{{cases_scored}}` | eval cases the run actually scored |
| `{{recall}}` / `{{fp_rate}}` | the run's scores, or `n/a` |
| `{{wiki}}` | repo context for the files the run failed on |
| `{{instruction}}` | whatever `--instruction` passed, or empty |

`{{instruction}}` lets the template decide where a one-off steer is read. Leave it out and a passed
instruction is appended at the end instead — it is never silently dropped, because a flag that
sometimes does nothing is worse than no flag.

Whetstone supplies the output *structure* (body, rationale, targeted cases), so `prompt.md` only has
to say how to think about the change. Case ids the model returns are validated against the skill;
ones that do not exist are dropped and reported rather than becoming a `--targeted` flag that fails
the gate for the wrong reason.

### The escape hatch

Replace `prompt:` with `run:` and Whetstone invokes your program instead:

```yaml
run: ["python", "run.py"]
```

The digest arrives as JSON on stdin; print `{"body": ..., "rationale": ..., "targeted_cases": [...]}`
on stdout. The working directory is the step folder. A non-zero exit surfaces your stderr.

`prompt:` and `run:` are mutually exclusive. Use `run:` when a prompt genuinely will not do — the
declarative form is what most skills want, and it is the one people will copy.

---

## `update/step.yaml` — regenerating the repo wiki

Whetstone does not summarize repositories. This step invokes the generator you already run — a
LangChain openwiki, an internal doc pipeline — and takes responsibility for the part that is
Whetstone's job: checking the output is usable, indexing it so retrieval is deterministic, and
making sure the refresh retracts any gate passed against the old context.

```yaml
description: Regenerate the repo wiki from the openwiki generator.

run: ["openwiki", "build", "--repo", "{{repo}}", "--out", "{{out_dir}}"]
timeout_s: 900

index:
  - page: auth-service
    paths: ["src/auth/**"]
```

```bash
uv run whetstone skills update --skill skills/<id> --repo /path/to/source/repo
```

The generated wiki is **staged on `whetstone/skill/<id>`**, not written into your checked-out
folder — the same branch guidance edits go to, so the console and the CLI never disagree about what
this skill's content is. `--working-tree` writes the files out instead when you just want a look;
a wiki left only in the working tree is invisible to the console, which reads the branch first.

Substituted into `run`: `{{repo}}`, `{{out_dir}}`, `{{skill_id}}`. It is a **list of arguments,
never a string** — nothing is re-split on spaces and no shell is involved, so a path containing a
space works and a config value can never become two arguments.

### What your generator must leave in `{{out_dir}}`

```
pages/<name>.md    one markdown file per subject; the first `# heading` becomes its title
index.yaml         which source paths each page describes
```

`index.yaml`:

```yaml
source:
  generator: openwiki
  repo: git@gitlab:team/service.git
  revision: abc123f         # shown in the preflight, so a stale wiki is recognisable
pages:
  - page: auth-service
    paths:
      - "src/auth/**"
```

Two supported arrangements:

1. **Your generator writes `index.yaml` itself** — preferred. The tool that knows which source files
   a page describes is the tool that wrote the page. Delete the `index:` block from `step.yaml`.
2. **Your generator writes only pages** — declare the mapping under `index:` in `step.yaml` and
   Whetstone writes `index.yaml` for you.

A generator producing neither is an error naming both options. The alternative would be a wiki that
loads as empty and a reviewer that silently lost all its context.

---

## How the wiki reaches the reviewer

Retrieval is **by source path, not by meaning**. The index maps globs to pages, a change names files,
and the pages covering those files are injected. No embeddings, no similarity search.

That is not a shortcut. A gate compares base and candidate over the same cases, so if retrieval
could return different context on the two sides, a score difference would stop meaning what it is
supposed to mean. Path retrieval is a pure function of the diff.

- Globs use `**` for "any depth" and `*` for "one segment", so `src/auth/*` does **not** match
  `src/auth/nested/thing.rs`.
- Pages are ranked by how many of the changed paths each covers, ties broken by index order.
- The caps apply per review. Over the page cap, the excess is dropped and named. Over the byte cap,
  the most relevant page is truncated rather than dropped — half of the right page is context, none
  of it is not.
- Wiki text is injected **after** the guidance, labelled explicitly as background and not as rules,
  so it cannot be read as guidance to apply.

### The wiki is part of `skill_hash`

Regenerating the wiki changes what the reviewer sees, so it **retracts a passing gate** and the
skill must be re-gated before it can be proposed. Without that, an `update` run could change the
reviewer's context while a stale gate still said the skill was safe to publish — exactly the hole
C6 exists to close.

A skill with no `wiki/` folder hashes exactly as it did before this feature existed, so landing it
invalidated no stored gate result.

Steps themselves are **not** hashed. They describe how to run things, not what the reviewer reads,
so editing a sample size does not retract a gate. The line is: does it change what the model sees
when it reviews?

---

## The loop, end to end

```bash
# 1. Score it. Records the run the improve step will read.
uv run whetstone eval run --skill skills/<id>

# 2. Draft a change from what it got wrong and stage it on the skill's branch.
uv run whetstone skills improve --skill skills/<id> --apply

# 3. Run the gate command it just printed, verbatim. It carries --targeted already.
uv run whetstone eval gate --repo . --skill-path skills/<id> \
  --base-ref main --candidate-ref whetstone/skill/<id> --targeted <case>

# 4. A passing gate is what unlocks Propose MR in the console.
```

Everything after step 1 addresses the skill by id and lands on one branch, so the gate evidence is
filed where C6 looks for it. Review what was staged with `git diff main whetstone/skill/<id>`, or
open the guidance editor in the console — it reads the same branch.

Check what a skill defines, and that all of it loads:

```bash
uv run whetstone skills steps --skill skills/<id>
```
