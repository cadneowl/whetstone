# Whetstone — Architecture Decision Record

Locked decisions for the project. Append-only; supersede rather than edit.

## ADR-001 — Language: Python
**Decision:** Python 3.13 for the harness, connectors, and core.
**Considered:** A compiled language (Rust) for compile-time exhaustiveness on the domain model and
connector normalization — the one genuine argument, since the plugin architecture depends on
normalization correctness.
**Why Python wins:** M1 is dominated by LLM I/O latency, not compute; iteration speed and the
Python-native eval/LLM ecosystem outweigh compile-time guarantees. The type-safety gap is recovered
with **pydantic v2 strict models + the connector contract conformance suite**, which catches
normalization drift at test time (where it matters). Memory/perf are non-goals. If any single piece
ever gets hot, it's a library swap, not a platform decision.

## ADR-002 — Tooling
- Env/deps: **uv**. Build backend: hatchling. src/ layout, package `whetstone`.
- Models/validation: **pydantic v2** (strict).
- CLI: **typer**. Tests: **pytest**. Lint/format: **ruff**. Types: **mypy**.
- HTTP (connectors): **httpx** + **respx** cassettes for hermetic tests.
- Config/skills: YAML (**pyyaml**). `.env`: **python-dotenv** — the parsing has real edge cases
  (quoting, `export` prefixes, `#` inside a quoted value, BOMs from Windows editors), and every one
  of them mangles a *secret* into a confusing auth error rather than a clean failure.

## ADR-003 — Plugin boundary
Core loop imports **zero** provider code. Providers implement capability Protocols
(`SourceConnector`, `ReviewConnector`, `WriteConnector`) and are discovered via entry points
(`whetstone.providers`). A single **contract conformance suite** runs against every provider
(Fake + GitLab today, GitHub later). GitLab first.

## ADR-004 — Skills are self-testing folders
`skills/<id>/` = `SKILL.md` (frontmatter + guidance) + `meta.yaml` (owner, triggers, references,
provenance) + `eval_cases/<case>/` (`case.yaml` + `change.diff`). Git is the source of truth.
Eval cases ship next to the guidance they test.

## ADR-005 — Determinism model
Reviewer and Judge are the two nondeterministic edges; both have Fake implementations so the entire
harness is deterministically testable with no LLM/network. The LLM judge is itself validated against
human labels (meta-eval) before its verdicts gate anything. Scoring math is pure and deterministic.

## ADR-006 — Trackers are a separate capability, and defects are the primary recall signal
**Decision:** Add a fourth capability, `tracker`, with its own `IssueConnector` protocol and its own
domain type (`domain/issue.py`). Jira implements it. Pairing an issue with the merge request that
fixed it lives in `corpus/linking.py`, not in either provider.

**Why a separate capability:** a tracker knows nothing about diffs and a forge knows nothing about
incidents. Widening `ReviewConnector` would have forced every forge to grow issue methods it cannot
answer, and every tracker to grow diff methods it cannot answer. The capability split already in
ADR-003 is the mechanism for exactly this.

**Why it earns its place in M1**, which listed Jira as out of scope: M1's deliverable is a gate whose
scores are trustworthy, and *recall* is the harder half to evidence. Review history only ever labels
what a reviewer caught — it is silent about misses, which is precisely what recall measures. A
shipped defect is a labelled miss. Reversing the merge request that fixed it reconstructs the change
that should have been caught, so the corpus gains cases in the one region review history cannot
reach. That is a measurement-quality argument, which is this milestone's subject.

**Consequences:** diff reversal (`CodeChange.reversed()`) and suggestion application
(`replace_added_lines`) become domain primitives. Reversal is only meaningful for fixes that remove
or replace lines — a purely additive fix reverses to a deletion with nothing to point an expectation
at — so the builder must filter, and does. Sprawling fixes are sampled and discounted rather than
trusted.

## ADR-007 — Precision evidence is graded, not averaged silently
**Decision:** `should_not_flag` cases carry a closed `human_signal` vocabulary
(`domain/eval_model.py`) from which `Provenance.evidence` derives a strength, and
`service.precision_evidence` reports the mix. Every applied suggestion additionally yields its
*accepted fix* as a confirmed negative case.

**The problem:** a case built from a clean merge asserts that a reviewer should stay quiet, on the
evidence that no human said anything. That is not the same as there being nothing to flag, so an
`fp_rate` computed mostly from such cases scores how quiet a reviewer is alongside how precise it is.
Averaging them in with confirmed negatives hides the difference behind one number.

**Considered:** weighting cases inside `scoring.py`. Rejected — the weights would be invented, and a
score whose arithmetic encodes a guess is harder to argue with than one that reports its inputs.

**Decision instead:** (a) generate genuinely sound negatives, from the replacement text an applied
suggestion already carries and that was previously discarded; (b) surface the mix wherever the score
is shown. The inference cannot be repaired. Hiding it was the fixable part.

## ADR-008 — Permission to publish guidance is bound to content, not to a branch
**Decision:** A gate result is persisted (`gates.py`) carrying the `skill_hash` of the candidate
skill **as committed**. `GateStore.verdict_for(skill_id, hash)` is the sole authority on C6, and it
is consulted both by `GET /api/skills/{id}/proposal` and by `POST /api/git/propose`.

**The problem it solves:** until now the project could *measure* a guidance change but had nowhere
to make one, and nothing structural stopped a change from being published unmeasured. `whetstone
eval gate` printed a verdict and exited. A verdict that is not stored cannot be checked later, so
"never ship a skill change you can't prove is an improvement" was a discipline, not a property.

**Why content and not the branch.** Keying evidence on `whetstone/skill/<id>` would make one passing
gate a standing licence: gate once, then keep editing. Keying on the hash means the permission
evaporates the moment the guidance changes, which is the only version of the rule that cannot be
gamed by ordinary use. It is also why `service.record_gate` hashes its *arguments* rather than the
skills `gate_skills` scores — those carry the union of both sides' eval cases, a set that exists in
neither commit.

**Why `meta.yaml` edits do not retract it.** `skill_hash` covers the guidance body and the eval
cases — what determines a score. Owner, references and provenance cannot change what the reviewer
does, so forcing a re-gate after an owner change would be ceremony that teaches nobody anything.

**Why a practice-mode gate is not evidence.** Practice mode (C4) substitutes the pattern reviewer
and the deterministic judge so the console is explorable with no spend and nothing to authenticate
with. Its PASS is a statement about a regex. Accepting it would let a demo mode wave the whole rule
through.

**Why the check sits at the push and not only in the editor.** The console's *Open in editor* escape
hatch hands a branch to whatever tools someone prefers, and the resulting commits arrive like any
others. A rule enforced only on the button most people happen to click is not enforced.

**What the guard actually asks.** *Does this branch change what the skill would publish?*, not *did
`SKILL.md` change?* The first draft asked the second question and had three bypasses: deleting an
eval case (the cheapest way to raise a score without improving anything — drop the case the reviewer
keeps failing), rewriting one into a vacuous form, and deleting `SKILL.md` outright, which the code
skipped because there was no skill left to hash. `skill_hash` covers the eval cases precisely so
that the first two count, so the guard now compares the skill at the base branch against the skill
at the branch.

**The one exemption is *adding* eval cases**, which is what lets triage batches push without a gate:
a case the skill did not have before cannot make the reviewer worse at the ones it did. Removing or
rewriting an existing case is not the same act and does not qualify. A skill that does not exist on
the base branch is also exempt — there is no baseline to regress from, and `eval gate --base-ref`
has nothing to load, so demanding evidence would make a new skill unpublishable rather than safe.

**The guard fails closed.** If it cannot determine what a branch changes — overwhelmingly because
`[git] default_base` names a branch the repo does not have — the push is refused and the
misconfiguration is named. A safety check that silently approves when it cannot run is worse than no
check, because it looks like one.

**A pass is not withdrawn by a later failure, but the disagreement is shown.** An eval at `k=1` is
noisy; letting a re-run revoke a demonstrated result would make publishing hostage to variance. So
the pass still permits, and `Verdict.caveat` carries the contradiction to the console, which badges
it *gated, with a caveat*. `reason` and `caveat` are separate fields so "you may not" and "you may,
but" never have to share one string.

**Consequences:** deleting `.whetstone/gates/` is not free the way deleting `.whetstone/runs/` is
(C2). Runs are telemetry; gate records are load-bearing, and removing them costs the right to
propose until the gates are re-run.

---

## ADR-009 — A skill carries its own pipeline; the host owns the budget

**Context.** Skills do not stay the same size or shape. One may hold forty eval cases and another
forty thousand; a Rust skill and a Terraform skill do not want the same improvement prompt; and only
the team that owns a skill knows which generator produces its repo context. Driving all of them
from operator flags means the flags encode the union of every skill's needs, and nobody remembers
which ones matter for which skill.

**Decision.** Policy moves into the skill folder as three optional steps — `evaluate/`, `improve/`,
`update/` — declared in YAML with an optional prompt template, and a `run:` escape hatch for the
minority that need real code. Whetstone keeps the budget: it assembles what a step sees, clusters
it, truncates it, caps it, and only then renders the prompt.

**Why the split falls there.** A step that could walk `eval_cases/` would work at forty cases and
fail at forty thousand, and it would fail in the worst way — a prompt that silently grows until it
is truncated by an API. Since a step is never handed the corpus, a step author cannot get the
scaling wrong; it is not theirs to get wrong. The cost of that is a step cannot do something we
did not anticipate without dropping to `run:`, which is the right trade for the default path.

**Failures are clustered, not sliced.** One representative per failure *kind*, largest group first.
The first twelve failures alphabetically are usually twelve instances of one problem; twelve
cluster representatives are twelve different things wrong with the guidance. Cluster size is also
the best proxy available for what a rule change is worth.

**Sampling is a hash, not a draw.** `sha256(seed:case_id)` — so base and candidate see one identical
set of cases, a sampled gate remains attributable to the guidance, and a result reproduces on any
machine. `random.sample` here would quietly turn every gate into a coin toss about which cases got
drawn. Stratifying by case kind keeps a sample of a 90%-positive corpus from containing no negative
cases and reporting a flattering `fp_rate` of zero. `--targeted` cases bypass sampling entirely: a
change asserting it fixes case X that is then never scored on X fails for an invisible reason.

## ADR-010 — Repo context is retrieved by path, and is part of `skill_hash`

**Context.** A reviewer seeing only a diff and a list of rules judges every change as if the
repository had no shape. Teams already run repo summarizers; the useful thing is to let a skill
review against that output.

**Decision.** A skill may carry `wiki/`: markdown pages plus an index mapping source-path globs to
pages. At review time the pages covering the changed files are injected after the guidance, labelled
as background and explicitly not as rules. Whetstone does not generate the wiki — `update/` invokes
the generator the team already has and indexes what it produces.

**Retrieval is by path, not by meaning.** Not a shortcut: a gate compares two skills over the same
cases, so retrieval that could return different context on the two sides would make a score
difference unattributable. Path retrieval is a pure function of the diff. Semantic retrieval would
also cost an embedding call per case and make the gate noisier — paying more to measure worse.

**The wiki is inside `skill_hash`.** It reaches the review prompt, so regenerating it changes what
the reviewer sees, and a gate passed against the old context must not still authorise publishing.
Without this, `whetstone skills update` would be a documented way around C6. Steps themselves are
*not* hashed — they describe how to run things, not what the model reads while reviewing, so editing
a sample size does not retract a gate.

> **Amended by ADR-022.** That last sentence held while every reviewer input came from the skill
> folder. A custom reviewer's `context:` bag lives in the *evaluate step* and does determine what the
> reviewer reads, so the "steps describe how, not what" premise no longer covers every case. Closing
> it properly is Phase 2 of ADR-022; until then a gate scored by a custom reviewer is warned rather
> than hash-protected, and the run record carries a context digest so the gap is at least visible.

**A skill with no wiki hashes exactly as before**, so landing this invalidated no stored gate.

**Caps are enforced at the retrieval boundary and never silent.** Over the page cap the excess is
dropped and named; over the byte cap the most relevant page is truncated rather than dropped, since
half of the right page is context and none of it is not.

## ADR-011 — No model call starts without saying what it may cost

**Context.** Every command that reaches a model can spend money, and how much depends on
configuration an operator may have set weeks ago in an env var they have since forgotten.

**Decision.** A shared preflight prints the resolved backend, model and endpoint, whether that
backend bills, and an upper bound on the call count — then asks. `--yes` skips it; a local backend
skips it automatically because nothing can bill.

**"Billed" is three-state.** Local is free, Anthropic and OpenAI are not, and an internal gateway on
someone's own hardware is genuinely unknown to us. Guessing "free" for the third is the guess that
costs money; guessing "billed" trains people to ignore the warning. So it says unknown, and unknown
prompts.

**The estimate is an upper bound and says so.** Judging short-circuits at the first matching
finding, so real runs come in under it. An estimate that could be exceeded would be worse than
none — an operator who trusts it once and is billed twice over will never trust it again.

**No confirmation available means abort, not proceed.** The failure mode of guessing wrong is
somebody's invoice. CI passes `--yes`, which is the same consent given deliberately.

## ADR-012 — One branch, addressed by id, for every writer

**Context.** `whetstone skills improve` shipped handing the operator a guidance body and a gate
command containing `<edited copy>`. Following the documented way of applying it — overwrite
`SKILL.md` — dropped the frontmatter, so the skill's id fell back to the folder name. The gate then
ran, passed, and stored its record under a skill that does not exist. Every step reported success
and the result was unusable: C6 looks up evidence by id and would never find it.

**Decision.** Staging primitives live in `staging.py` and both the console and the CLI use them.
`skills improve --apply` and `skills update` write to `whetstone/skill/<id>` through the same
`prepare_guidance` path the console's editor uses, which preserves the frontmatter, bumps the
version once per proposal, and validates by loading the result back.

**Skills are addressed by id, and that is now checked.** `prepare_guidance` builds
`<skills_root>/<id>/SKILL.md` while the console looks up `<skills_root>/<id>`, so a folder whose
name differs from its frontmatter id commits to a path that is not the folder the operator pointed
at. Previously that was an unstated invariant; `_staging_id` now verifies it and names both paths
when it does not hold. The bug it prevents is silent — the command says "staged" and the branch
still holds the old guidance.

**A body is not a file, and the CLI says so.** `--out` still exists and still emits the body alone,
because diffing it is useful. It now labels the output as a body and points at `--apply`.

## ADR-013 — Evidence must describe the skill you have

**Context.** `skills improve` read the most recent stored run regardless of whether it scored the
skill's current content, so editing the guidance and re-running produced a confident proposal aimed
at failures that may already have been fixed.

**Decision.** Refuse, naming both hashes, unless `--stale-ok`. The run record has carried
`skill_hash` since it was written, and the console already badges the same condition on the runs
list and on uploaded reviews; the improve path was the one place that ignored it.

**Refuse rather than warn.** Improving from stale failures is not a degraded result, it is a wrong
one, and it is indistinguishable from a good one by inspection. A warning on a command whose output
is a page of plausible markdown would be read past.

**A clean run does not spend a call either.** Nothing to learn from is not a reason to pay for a
rewrite — unless the operator passed `--instruction`, which is them saying they want one anyway.

## ADR-014 — A declared knob must do something

**Context.** The step contract shipped with `model:` blocks on the evaluate and improve steps that
nothing read, and an `inputs.guidance` flag that nothing read. The scaffold documented all of them.
Someone pinning a skill to a local runner to keep it off a metered API would have been billed
anyway, silently.

**Decision.** `model:` is wired through `eval run`, `eval gate` and `skills improve` as the default
under any command-line flag; `inputs.guidance` is deleted. The rule is that a key in a scaffold is a
promise, and the scaffold is copied far more often than it is read.

**The skill sets the default, the operator gets the last word.** A skill that knows it wants local
hardware says so once; the person running it can still override per command.

## ADR-015 — The console runs the work, and says what it costs first

**Context.** Every measurement handed the operator back to a terminal. The console could show what
a run produced and could stage a guidance edit, but the moment a number was needed it stopped —
and the guidance editor said so out loud, stating the rule that blocked publishing and then
printing a command to satisfy it elsewhere. That made C6 read as an obstacle rather than a step.

**Decision.** A thread-backed job runner, launched from four routes: eval, gate, improve, update.
The console drives all of them.

**Threads, not processes or a queue.** The work is almost entirely waiting on a model, and the
harness was already built for it — `run_skill_recorded` takes an `on_event` callback and a
`threading.Event` to cancel. A broker would add an operational dependency to a tool that currently
needs none.

**Polled, not streamed.** SSE delivers a second sooner and costs a streaming route, an
`EventSource` client and reconnection logic on both sides. The console talks to a process on the
same machine and a run emits roughly one event per case.

**In memory, not persisted.** A finished job's output is in the run or gate store and survives; a
job in flight when the server stops is lost. Persisting partial runs would mean owning a schema for
half a measurement, which is worse than a job the operator restarts.

**Capped at two.** More concurrency does not finish sooner against the same rate limits; it only
spends faster.

**Every launch is two clicks.** The first fetches the plan, the second starts the work. Both the
banner and the estimate come from `preflight`, the same code the CLI prints, so the surfaces cannot
drift into disagreeing about what a run costs. Read-only mode blocks launches — spending money is a
write regardless of what it leaves on disk.

**Where the CLI refuses, the console warns.** `skills improve` on a run with no failures is refused
outright by the CLI, because `--yes` would otherwise spend with nothing on screen to stop it. The
console has no `--yes`: every launch is a click on a banner, so the fact goes into the banner as a
warning and the operator decides. Rewriting passing guidance is a legitimate thing to want.

**A drafted proposal is not committed.** `improve` returns the body into the editor; staging is a
separate act. The value of a draft is that a person decides whether it is an improvement, and a
machine-written rule then takes the identical path a hand-written one does — stage, gate, propose.

## ADR-016 — The console opens on what to do, not on what exists

**Context.** Whetstone grew as a set of capabilities — mine, triage, score, draft, gate, publish —
each correct and each on its own screen. Assembled by hand they are not a workflow: the operator had
to hold the whole pipeline in their head and work out which of ten actions was today's. The home
screen was a list of skills, which answers "what exists" rather than "what should I do".

**Decision.** The inbox is home. One row per skill: what arrived since last time with its merge
request provenance, what the last run got wrong, where any change sits in the pipeline, and one
ranked next action with the reason for it.

**Ordered by closeness to shipping, not by severity.** A passing gate nobody has proposed is value
already paid for; a queue of fresh signal is work not yet started. So `propose` outranks `gate`
outranks `triage` outranks `score` outranks `improve`. An inbox that sorted by how much was wrong
would bury the cheapest win under the largest pile.

**The row shows evidence, not counts.** "Four unwraps shipped in !812, !814 and !820" is a reason to
change a rule; "4 candidates" is a number. The merge request is what makes a signal checkable, so it
is what the row leads with.

**The decision is a pure function**, over exactly the state the buttons are enabled by, so the reason
shown can never disagree with what the console will let you do.

**Unrouted signal is counted, not hidden.** A candidate matching no skill's triggers is invisible
from every per-skill view; the inbox surfaces the count rather than letting it rot in the queue.

## ADR-017 — Show the difference, not the result

**Context.** The guidance editor showed a textarea and a rendered preview. When the improve step
drafted a change, the textarea's entire contents were replaced and the preview showed what the
result would look like. Both were true, and neither answered the only question that matters at that
moment: *what is different?* A rewrite that quietly dropped four working rules looked exactly like
one that tightened a fifth — and the first version of the drafting feature produced precisely that.

**Decision.** The right-hand pane defaults to a line diff against what is staged. Preview is still
there, one click away, for judging how a rule reads.

**Computed client-side.** It has to keep up with typing, and an LCS over lines is quadratic on
inputs of a few dozen lines — which is free, and cheaper than a dependency.

**It warns when a change is mostly deletion.** "+1 −9 lines, this removes far more than it adds"
catches the failure mode a generated rewrite actually has, at the moment it can still be rejected.

**Eval cases show their last outcome.** The pinned list previously showed ids and paths, which is
decoration; which of them the skill currently gets *wrong* is the only thing that makes it worth
reading while rewriting a rule.

## ADR-018 — A drafted expectation has to beat the comment it replaces, measurably

**Context.** The triage step offers to rewrite a case's `semantic` — the ground truth every future
score is computed against — from the reviewer comment the corpus builder seeded it with. The
argument was intuitive: "nit: use ? here" describes nothing, so a standalone sentence must be
better. `drafting.py` already guaranteed the drafter never sees the guidance, which stops the eval
becoming a tautology. That is a different property from the sentence being *good*, and only the
first one had ever been checked.

The asymmetry is what makes this worth a decision. A bad guidance edit fails a gate and never
ships. A bad expectation ships silently and stays: nothing downstream will ever fail because of it,
so nobody finds out, and every score computed against it is quietly wrong from then on.

**Decision.** The claim is a measured metric with a floor, alongside the judge's, in
`meta_eval/drafting.py`. Each fixture case carries probe findings labelled by hand — one or more
that genuinely describe the underlying problem, and one or more that describe a *different* real
problem at the same location. Both arms face the same judge, the same probes and the same region;
only the expectation text differs. `DRAFT_IMPROVEMENT_FLOOR` is deliberately above zero: a drafter
that merely ties has bought a model call and a story.

**The two error kinds are counted apart**, because they fail in opposite directions. A **missed**
pair is a finding that was about the right problem, judged not to match — recall reads low and
somebody goes hunting for a hole in guidance that works. A **spurious** pair is a finding about
something else, judged to match — recall reads high and the case has stopped discriminating, which
is worse because nothing ever goes red.

**What it found** (qwen3-coder:30b via Ollama, 24 labelled probes over 8 cases, two runs):

| arm | accuracy | missed | spurious |
|---|---|---|---|
| raw comment | 0.71 | 6 | 1 |
| drafted | 0.88 – 0.92 | 1–2 | 1 |

Improvement `+0.17` and `+0.21` on the two runs. The claim holds, and the dominant baseline failure
is `missed` — vague comments do not cause wild matches, they cause real catches to be scored as
misses. Left alone they make a working skill look broken.

**It also found the failure the human accept exists to catch.** On the one case where two plausible
defects sat on the same line — a mutex guard held across an `await`, and an `unwrap()` on the lock
— the drafter described the second. That was the decoy, not the case. It produced a confident,
well-formed, checkable sentence about the wrong problem, which is precisely the output that gets
accepted on a reflex. Both of that run's drafted-arm errors came from that single case, so the
report attributes failures to cases rather than only totalling them: two errors on one case is a
drafter bug, one error each on two cases is judge variance, and the aggregate cannot tell them
apart.

**Known limits, stated so the number is not over-read.** The corpus is 8 hand-written cases, not
mined history, so it measures the mechanism rather than any real team's review habits. 24 probes is
small — the two runs differ by 0.04 on the drafted arm, which is one probe. And the `should_not_flag`
case is the weakest label in the fixture: describing what is *correct* forces the judge to match a
finding that takes the opposite stance on the same code, and it is genuinely arguable both ways.

## ADR-019 — A verdict names the guidance it describes

**Context.** The editor screen shows two things that look like one thing. The textarea, the diff and
the C6 proposal panel all describe the **staged branch**; the eval-case list and the improve panel's
digest describe the **working tree**, because an eval scores the working tree. Once anything is
staged those diverge, and the screen renders a red `MISSED` directly beneath a change that already
fixed it.

Gating does not close the gap — it widens it. `record_gate` writes a `GateRecord`, never a
`RunRecord`, so clearing a candidate at `recall 0.33 → 1.00` leaves every case row still reporting
the baseline. The screen then states a number that says *fixed* four lines above a list that says
*MISSED*, with nothing anywhere saying they are about different versions of the guidance. The
preflight already warned about exactly this for `eval run` ("holds a staged change that this run
will NOT measure"); the editor never did.

**Decision.** An outcome is shown with the run that produced it. `SkillDetail.scored_by` names that
run explicitly rather than leaving callers to infer it from `runs[0]`, and the console compares its
`skill_hash` against the staged content:

- **Same content** — the run is named and linked, as provenance. "Where do I see the score?" should
  be answerable by reading the screen, and an unattributed verdict invites the reader to assume it
  means whatever the rest of the page means.
- **Different content** — the case list says so in warn tone: these verdicts describe the base, the
  staged edit has never been run against them, and the gate verdict above is the only measurement
  of what is staged.
- **Never scored** — said plainly, rather than a column of "not scored" the reader has to add up.

**The improve panel warns on the same condition**, because the server already refuses it — `_run_for`
rejects a run that scored different content, as the CLI does with `--stale-ok`. Being told before
the click why the button will not work is the difference between a guard rail and a wall, and the
failures it would learn from are ones the staged edit may already have fixed.

**Computed once, in the parent.** Two panels depend on the same comparison, and a screen whose whole
purpose is to stop two halves disagreeing must not itself contain two copies of the test.

**Not fixed by writing run records from gates.** A gate scores a *union* of both sides' cases under
its own sampling policy; filing that as a run of the candidate would make the runs list a mix of two
different measurements. Saying which measurement you are looking at is the fix for the *label*;
ADR-020 is the fix for the missing measurement itself.

## ADR-020 — A draft can be scored on its own

**Context.** The loop an operator actually works in is: change the guidance, run the full suite, read
the score, ask the improve step to fix what failed, repeat. It broke at the second step, and the
break was invisible because every individual piece behaved correctly.

Staging never touches the working tree, and an eval scored the working tree. So the only way to
measure an unmerged change was a gate — and a gate answers a *comparison* ("did that help?") while
writing no run record at all. An operator with a failing gate therefore had a verdict, no per-case
outcomes, and nothing for `improve` to read, because `improve` reads runs. Worse, `improve` resolved
the skill from the working tree, so a run of the draft was rejected as describing different content
while a run of the working tree had nothing to say about the draft. **No run existed that satisfied
it.** The dead end was structural, not a missing button.

**Decision.** `POST /jobs/eval` takes `staged: true` and scores what `whetstone/skill/<id>` holds.
The whole skill folder is loaded, not just `SKILL.md` — a draft may add or change eval cases, and
"run the full suite on my draft" means the suite the draft carries.

**A boolean, not a ref.** No caller-supplied revision is handed to git; the server resolves the
branch from the skill id, the same rule `GateRequest` already followed.

**The evaluate step still comes from the working tree.** It is how this machine runs a model, not
part of the guidance under test — taking it from the branch would let a staged change quietly alter
the harness measuring it.

**`improve` resolves the skill exactly as the editor does**: the staged draft when there is one.
"Fix these failures" has to act on the version the operator is looking at, or a staged change can
never be improved, only abandoned.

**The refusal moved into the plan.** The stale-run check was already right, but the console shows a
plan *before* the click, and the plan swallowed it — so the operator confirmed a spend and only then
got a 422. That was tolerable while staleness was rare. It stops being rare the moment drafts are
scorable, because with work on a branch the newest run is usually of the working tree, which is
exactly when someone reaches for the button.

**The old preflight warning was right and incomplete.** It named the gate as the answer, reasoning
that one number about a candidate settles nothing. True for "did that help?", and wrong for the
other question an operator asks — *what is still broken in my draft?* — which is not a comparison
and which only a run can answer.

## ADR-021 — Guidance is the whole folder, not one file

**Context.** Guidance outgrows `SKILL.md`. A real skill splits its rules into `patterns/rust.md`,
`reference/errors.md` and so on, and the body points at them by name. Whetstone read none of it.
`load_skill` opened `SKILL.md`, `meta.yaml`, `eval_cases/` and `wiki/`, and the review prompt was
built from `skill.body` alone — so "see ./patterns/rust.md for the full list" reached the model as a
pointer to a file it could not open, and the guidance under test was silently incomplete.

The second consequence was worse. `skill_hash` did not cover those files either, so rewriting a
referenced page from *never unwrap* to *always unwrap* left the digest byte for byte identical:

```
hash before: 687e5fd86e1791cf
hash after : 687e5fd86e1791cf
```

A gate passed against one set of rules therefore went on authorising the publication of a different
set — *Propose MR* stayed enabled, the badge stayed green. That is precisely the failure C6 exists
to prevent, and it was reachable by editing a file the tool never mentioned.

**Decision.** Every `.md` under a skill folder is guidance. It is loaded into `Skill.pages`, covered
by `skill_hash`, and inlined into the review prompt after the body.

**Four exclusions, each something other than rules:** `SKILL.md` (it is the body), `eval_cases/`
(the corpus that tests the rules), `wiki/` (repo context, retrieved per change rather than always
sent, and already hashed), and the step folders (prompts instructing the harness). Everything else
in the folder is sent to a model, so anything that is not guidance does not belong there.

**Path is hashed as well as text.** Moving a rule between pages changes what the prompt says; two
skills differing only in where a rule lives are not interchangeable for scoring.

**Loaded in full, bounded at render.** The hash covers what is on disk; the cap applies when the
prompt is built. So editing a page that was too large to send still invalidates a gate — the
conservative direction. `MAX_PAGE_BYTES` matches `WikiLimits.max_bytes` for the same reason: this
text is paid for on every case of every trial on both sides of a gate.

**A page that will not fit is dropped whole and named in the prompt.** Half a set of rules reads to
a model as a complete set, and a rule truncated mid-sentence is worse than one honestly absent. The
prompt says which files were withheld so the model does not report confidently on rules it never saw.

**A skill with no pages hashes exactly as before**, verified against a live gate record: no stored
evidence is invalidated by this landing.

**The console shows pages but does not edit them.** The editor stages `SKILL.md`; companion pages
travel with the branch because staging is folder-level, and the Edit tab names them so nobody
mistakes the textarea for the whole guidance. Editing them in the browser is a bigger change — a
multi-file editor — and not required to close the soundness hole.

---

## ADR-022 — A skill may bring its own reviewer, and the host stays the orchestrator

**Context.** The built-in reviewer is a single structured model call with no tools: guidance and
retrieved wiki in, findings out. For a code-review skill over a large repository that is not enough
— whether a call is dangerous usually depends on the *called* function, which is not in the diff.
The repository cannot be pre-baked into a wiki (400k files) and cannot fit in context. Such a
reviewer needs to reach the actual source and query it while reviewing, and different skills need
different things to do that: one a source location, another that plus a schema, an API spec, ten
more.

**Decision.** A skill's `evaluate` step may name its own reviewer program under `run:`, and declare
an open-ended `context:` bag of whatever that program needs. Whetstone resolves the bag, validates
it, forwards it on stdin with the guidance and the diff, and takes back findings. `prompt:` stays
forbidden on `evaluate` — the reviewer prompt is the harness's.

**Whetstone never gives a model filesystem access; it gives a program a folder path.** This is the
whole shape of the decision. The alternative — teaching the built-in reviewer to use file tools —
would make Whetstone own tool loops, retries and context management for every provider, and would
put the agentic complexity on the side that does not want it. Instead the agent is the operator's,
running behind a one-method seam (`Reviewer.review`), and Whetstone keeps doing what it is good at:
picking cases, judging findings, scoring, and gating. The host stays model-agnostic.

**One seam, widened along an existing pattern.** Not a plugin system. `improve`/`update` already
shell out to operator programs with a JSON-on-stdin contract; the reviewer now does the same, with
the same argv list (no shell), fixed `cwd`, hard timeout, and error taxonomy. A failed review raises
and fails the run: a gate computed with cases the reviewer silently errored on is not a verdict.

**One resolver for the console and the CLI.** Divergence here would be the worst kind — a gate run
from the CLI and the same gate run from the console would measure different things. So both go
through `reviewer_for`, and the program runs everywhere the reviewer runs: eval, both gate sides,
the saturation probe, and live review.

**Context values are typed by what is safe to commit, print and hash**, not by what they mean — the
host never interprets the keys. An `env:` value is machine-local or secret: forwarded, shown as
`<env:NAME>`, and excluded from the hashable slice so a shared gate survives a teammate whose
checkout lives elsewhere. `pin: true` marks a value that identifies *what the reviewer reads* (a
commit sha), so it is shown in full and is hashable. A `file:` value is committed material, hashed
by content. Anything else is a literal. A mapping carrying any directive key must be a well-formed
directive: read as a literal, a misspelled key would forward the declaration instead of the value
and make `required: true` silently stop refusing an unset variable.

**A score is only attributable if the instrument is named.** A custom reviewer runs a model
Whetstone never sees, so on such a run `backend`/`model` describe the judge alone. Runs, gates and
reviews therefore record the reviewer identity, the redacted context, and a digest of its hashable
slice, and the console shows them. Recording the number without the instrument is what makes a
history contradict itself.

**Cost is counted where it cannot be priced.** Whetstone makes no review call, so the estimate drops
the reviewer term and counts judge calls only. What it cannot know is the program's own spend, so
the plan states the invocation volume instead — the one number the operator can multiply by their
own per-call cost.

**Determinism is the open edge, and is surfaced rather than hidden.** A gate attributes a score
delta to the guidance only if everything else the reviewer saw was identical on both sides. One
reviewer instance serves both sides, so guidance is the only variable on Whetstone's side — but a
program reading a moving source can still break it. Folding the hashable context into `skill_hash`
is **Phase 2**: `skill_hash` is a pure function of the `Skill`, while context lives in a step, and
resolving that is a choice worth making deliberately rather than smuggling in here (see ADR-010, now
amended). Until then the plan **warns** when a custom reviewer is gated, and the sound configuration
is a pinned ref that the program reads.

**Zero change for skills that do not opt in.** No `run:` means the built-in reviewer is constructed
exactly as before, and `reviewer` on a record is empty. Landing this invalidated no stored evidence.

**The trust boundary is unchanged, and stated.** `run:` executes code from the skill folder — the
same boundary `improve`/`update` already cross. Whetstone passes `source_root` and never traverses
it, so it adds no path-handling surface of its own. If a reviewer sends source to a cloud model that
is the operator's choice; the context bag makes "this reviewer has the whole repo and a network
model" legible where it previously would not have been.

---

## ADR-023 — A skill folder is run, not concatenated

**Context.** ADR-022 widened the reviewer seam so an operator could plug in their own program, and
declared a non-goal: *"Giving whetstone's own reviewer model filesystem tools."* That was the wrong
call for the thing skills actually are. A skill is a folder — `SKILL.md` plus reference pages plus
generated repo docs — and `SKILL.md` refers to the rest of it the way a person would, with markdown
links: *"see [principles.md](references/principles.md)."* The built-in reviewer cannot follow a
link, so `_load_pages` swept every `.md` in the folder into one prompt. On a real skill that meant
17.6KB sent on every case regardless of what the change touched, a `README.md` written for humans
fed to the model as rules, and a `wiki:` path index that mapped documentation to source paths doing
nothing at all. The seam ADR-022 added did not fix this: it only moved the work, since every skill
would have had to ship its own agent loop.

**Decision.** Whetstone runs a skill as an agent. `SKILL.md` is the instruction set, the folder's
other pages are fetched on demand through `read_skill_file`, a declared source root adds sandboxed
`read_file`/`grep`/`list_dir`, and the skill may declare its own tools as programs. This supersedes
ADR-022's non-goal; the reviewer-program seam remains, and the two are mutually exclusive.

**Progressive disclosure, because that is what the author wrote.** The alternative — keep inlining
but do it more cleverly — cannot work: only the agent knows which page is relevant to the change in
front of it, which is precisely the judgement `SKILL.md` was written to express.

**The host still learns nothing domain-specific.** A skill that needs a tracker ships a script that
reads the tracker and names it under `agent: tools:`. Whetstone offers it to the model and runs it
on the same JSON-on-stdin contract `improve`/`update` already use. Whetstone never grows a Jira
integration, and a tool that fails reports back *to the model* rather than ending the run, because
an agent told "no such issue" tries something else.

**It cannot block on a human, and that is enforced rather than hoped for.** Skills written for
interactive use say things like "ask clarifying questions"; unattended there is nobody to ask. So:
no tool exists for asking, the runtime preamble states there is no human, a turn calling no tool
costs a step and is nudged, and at `max_steps` the answer is forced with only the terminal tool
offered. If the model still refuses, the run fails — an eval reporting zero findings because nobody
answered is indistinguishable from a clean review, and that ambiguity is worse than a failure.

**A terminal tool, not parsed prose.** `submit_findings` is the only way to finish, so the harness
never guesses what a skill concluded.

**Tool support is required, never worked around.** `structured` degrades gracefully when a local
server rejects `response_format`; `converse` does the opposite and raises `ToolsUnsupported`. A
review carried out with no tools looks exactly like one that worked and would report confidently on
code the model never opened.

**Determinism is weaker, so it is measured.** An agent is a less fixed instrument than a single
call. Both gate sides share one instance, and every run records the trajectory it actually took;
`trace_diverged` reports when the two sides investigated differently, which is the honest signal —
a delta may then reflect what the agent chose to read rather than the guidance. `k>1` still measures
per-trial variance. This does not make an agentic gate as strong as a single-call one, and the
record says so instead of implying otherwise.

**Cost is the run's own.** Unlike a reviewer program, an agent spends Whetstone's backend, so the
estimate counts `max_steps` calls per review rather than one and the plan names the ceiling.

**Off by default.** Without `agent: enabled` nothing changes, and `agent:` and `run:` are mutually
exclusive — the skill is run here, or reviewing is handed over entirely.

---

## ADR-024 — The judge is one verifier, not the definition of scoring

**Context.** Every layer of the eval model assumed code review. `EvalCase.change` is a diff,
`kind` is `should_catch`/`should_not_flag`, `Expectation.must` is `appear`/`not_appear` at a
`path`+`line_range`, `Finding` is the only output type, and scoring reduces to recall and fp_rate
via the judge's match/no-match calls. That is an honest model of *reviewing*. It cannot express a
skill that writes tests: the output is code, not located claims, and success is "the tests pass and
catch the defect", which is a question for a test runner rather than for a judge comparing two
sentences. Forcing such a skill into `must: appear` would not be awkward — it would produce numbers
that mean nothing.

**Decision.** Generalise the *grading*, not the whole model. A skill may be scored on **task cases**
(`task_cases/`) instead of eval cases: it is given an instruction and a fresh workspace, it produces
files, and a **verifier** grades them. The judge becomes one verifier among several rather than the
definition of what scoring is.

**The constraint that shaped everything: a verifier must return a comparable scalar.** Whetstone's
whole claim is that no skill change ships without evidence it is an improvement, and that only works
if base and candidate reduce to numbers that can be compared. So `VerifyOutcome` always carries
`score` (0–1) beside `passed`. A grader that answered "it depends" would quietly turn the gate into
decoration.

**Partial credit is not a nicety.** A gate over binary outcomes can only see whole cases flip, and
most real improvement is smaller than that. So a command verifier may report a degree — while the
exit code keeps the verdict, so a grader cannot pass itself by printing `1.0`.

**Two verifiers ship, and an escape hatch.** `command:` covers the overwhelming majority (run the
tests, run the linter, run the build) and is deterministic, which matters more here than anywhere
else: a flaky grader corrupts a gate in a way that is far harder to notice than a flaky reviewer,
because the number still looks like a score. `run:` hands grading to a program the skill ships, for
quality an exit code cannot express. There is deliberately no LLM-judge-on-prose verifier yet —
that would reintroduce the ambiguity this ADR exists to remove, and should be added only against a
real skill that needs it.

**Additive, not a rewrite.** The review path is untouched: same `EvalCase`, same judge, same
`SkillScore`, same gate, same stored evidence. Task skills run alongside. Merging the two scoring
models would have meant migrating every stored record and every C6 gate for no gain — the machinery
worth reusing (sampling, trials, gate discipline, provenance, the console) was never review-specific
in the first place.

**Errors are separated from failures.** An executor that crashes on one case records the error and
scores zero, so one malformed case cannot lose a corpus of two hundred. A *verifier* that crashes
stops the run instead: scoring an ungradeable case as a failure would blame the skill for a broken
grader, and a corpus that silently grades itself down is worse than one that refuses.

**Isolation per case.** Each task case gets its own workspace, seeded from a `files/` directory
rather than YAML-embedded strings — the seed for a realistic task is source code, and source code
inside YAML is neither readable nor reviewable, the same reason a case's diff has always been its
own file. The write tool is sandboxed exactly as the read tools are.

**What is deliberately not built.** A generic `Input` union (task cases take an instruction, seed
files and optionally a change — enough for every task described so far), task cases in the console,
and mining task cases from production signal. Each needs a real skill to design against rather than
a guess, and guessing here is what produced the wrong abstraction the first time.

> Superseded in part by ADR-027: task cases now have a console surface. The other two hold.

## ADR-025 — An agent that fails must fail visibly, and one that succeeds must be affordable

**Context.** ADR-023 made a skill folder something Whetstone *runs*. A code review of that work
found the engine sound and the edges around it not: the configuration channel the design depended on
was unreachable, the failure paths disagreed with each other, and several things the records claimed
were not true. The individual defects were small; what they had in common is that each one degraded
into something that *looked like a normal run*.

**Configuration was declared and then refused.** `context:` — the documented way a skill names the
token its own Jira tool needs — was validated as requiring `run:`, which `agent:` forbids. So the
bag could never be populated for an agent, and the only configuration that reached one was
`agent.source:`. The fix is that `context:` is accepted wherever something consumes it (`run:`,
`agent:`, `task:`) and still refused where nothing does.

**The obvious way to fix that would have leaked secrets.** The agent's system prompt was assembled
from the *resolved* context values. With the bag reachable, a `{ env: JIRA_TOKEN }` would have been
written into the prompt of every case and into every transcript. So the split is explicit and
load-bearing: **tools receive `values`, the model receives `redacted`**. A script calling Jira needs
the credential; a model reasoning about a diff never does.

**Silent degradation is the failure mode this feature is prone to.** Three instances, all fixed the
same way — refuse loudly:

- A `source:` root that is *set but wrong* left every source tool answering "no such file", which
  reads exactly like a clean codebase. Refused at the plan.
- A `task:` skill fell through to the review path, scored its empty `eval_cases/`, and reported
  `recall 1.000` — a perfect score over nothing, on the bundled example. The review path now refuses
  it and `whetstone eval task` runs it.
- A glob with a directory component (`src/**/*.py`) matched nothing, because `Path.match` compares
  right-to-left against the file name. A model asking the obvious question got "no such code".

**Cancelling is one event, however deep it is noticed.** `AgentCancelled` was an unrelated exception
that fell past every `except RunCancelled`, so stopping a run reported a crash. It now subclasses a
`RunCancelled` that lives in a dependency-free module both layers can reach.

**A run is not lost because one case was.** A reviewer that cannot answer used to kill the whole
run — and an agent makes many calls per case, so the exposure across a large corpus is not small.
An unscorable case is now recorded as such and contributes *nothing* to the confusion counts:
scoring it as a miss would blame the skill for the instrument, and scoring it as a pass would hide a
broken run. `SkillScore.errors` reports the count and the gate blocks a candidate that produced more
of them than its base — the same rule the task gate already applied. This closes a disagreement
between the two harnesses that had no justification other than being written at different times.

**Unbounded work is a correctness problem, not a performance one.** `grep` pruned only
dot-directories, so a real checkout meant walking `node_modules` and decoding bundles and
executables as text: 42.5s per call against this repository, against 0.1s with the usual vendor
trees pruned. An agent greps several times per case, on every case, on both sides of a gate. A tool
that slow is a tool a skill author stops using, which quietly returns the agent to reviewing a diff
with nothing open.

**Evidence nobody can read is not evidence.** `trace_diverged` was computed on two classes and
rendered nowhere; as a plain `@property` it did not even survive serialization. The trajectory has
the same job as `reviewer_context_digest` — letting a reader decide whether to trust a delta — so it
is now a `computed_field`, printed by the CLI for runs and gates, and flagged in the console. In the
same spirit, `record_gate` now counts the agent's own calls, which it alone was omitting while its
two sibling recorders included them, and the cost plan prices the forced final turn it had been
quietly leaving out of every review.

**What is deliberately not built.** Driving task skills from the console: the review console is
built around recall/fp_rate and a run drill-down, and giving task runs a first-class surface is a
feature, not a fix. The CLI runs them and both the console and the CLI refuse to pretend otherwise.

> Superseded by ADR-027, which builds that surface.

## ADR-027 — Measure the sharpening, and give task skills the same instruments

**Context.** Whetstone's purpose is sharpening skills, and nothing in it could say whether that was
happening. The console showed one run and one gate; neither is an answer, because a run is a
snapshot and a gate is a verdict about a single edit. Separately, task skills were runnable only
from the CLI, so half the product's surface treated them as skills that did not exist.

**The obvious trend line is a trap, so the report says what it is resting on.** Plotting recall over
time and reading the slope is wrong three ways. The corpus changes underneath it — and the healthy
loop promotes exactly the cases the skill got *wrong*, so a skill doing its job shows **falling**
recall while a skill whose corpus was frozen a month ago shows a flattering flat line. The judge
changes, which re-scores history. The reviewer changes, which moves the number with no guidance
edit at all.

So `sharpening.py` reports two things and is explicit that they are not the same strength of
evidence. The **trend** is weak: the score over time, cut into comparable segments, with every seam
named, and a delta computed only across the longest unbroken stretch — never first-to-last. The
**ledger** is strong: a gate holds the case set, the judge and the reviewer fixed across both sides,
so a case it recorded going from failing to passing genuinely improved and no corpus churn explains
it away. Each proven fix is then re-checked against the newest run — `still passes`, `REGRESSED`, or
`not re-measured since`. That third state is deliberate: a ledger that counted a March fix forever
would be a monument to an April regression nobody noticed.

The verdict is written to be quotable and to be *true*, which mostly means declining to claim
sharpening on evidence that does not support it. A skill with gates but no proven fixes is told
plainly that what is proven is that it has not rotted.

**Task skills get the same instruments, not lesser ones.** Task runs and task gates are now
recorded (`taskruns.py`), so a task skill has a history, a trend and a C6 verdict like any other.
Two things fell out of building it. `gate_tasks` computed which targeted cases a change fixed and
then threw the answer away, so a task gate could never demonstrate an improvement — the same disease
diagnosed for review gates, in a second place. And the C6 verdict is now one function
(`gates.verdict_over`) over a minimal `GateLike` shape, because two copies of a publish rule is how
a task skill quietly ends up held to a weaker standard than a review one.

Records name **two** instruments, not one: the executor that produced the work and the verifier that
graded it. A task score without both is uninterpretable — the same skill graded by a different
`verify:` command is a different measurement, exactly as a review score judged by a different judge
is — so a changed grader breaks a task trend the way a changed judge breaks a review one.

**What is deliberately not built.** A `TaskGateRecord` that fits in the review `GateStore`: five of
`GateResult`'s nine fields are meaningful for a task gate and four are not, and putting a recall of
zero next to a task score would be a number that was never measured sitting in front of the person
deciding whether to ship.
