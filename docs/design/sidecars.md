# Sidecars: per-directory skill knowledge that lives with the code

**Status:** **All seven steps built** — §14 marks each, and §9.2 records what the fixture measured
and the two costs it turned up that this document did not predict. The exit criterion at step 2 is
answered *on a built fixture* (`examples/sidecar-review/`), which clears the mechanism and leaves
the efficacy question where it was: it needs a real corpus with sidecars written by the people who
own the code, and that is still the decision this whole design is subject to. Builds on
[`agentic-reviewers.md`](./agentic-reviewers.md), whose Phase 1 shipped: this doc reuses that
design's `context:` bag, `source_root` and `pin: true` rather than inventing a parallel mechanism,
and marks the one place it deliberately departs from it (§11). It does **not** depend on
`_feed_context`/`skill_hash`, which that doc now records as superseded — sidecar identity rides the
same `reviewer_context_digest` → `BaselineKey` path that already enforces measurement identity.

**The ask.** A review skill over a large proprietary codebase needs to know thousands of
particulars — why this class is a deliberate god object, why the retry cap is 3, which invariant the
reconciler depends on. None of it fits in `SKILL.md`, none of it is recoverable from the code, and
concentrating it in the skill folder produces a 38k-character `references/system-map.md` that the
one-shot reviewer drops on budget and the agent reviewer never chooses to read.

**The shape.** Move the *local, particular* knowledge next to the code it describes, as one markdown
file per directory per role. Keep general rules central. Load sidecars mechanically from the paths
under review, hash what was loaded into run identity, create them only from triaged evidence, and
verify them with the agents that already read them.

---

## 1. Goals / non-goals

**Goals**
- Knowledge scales with the codebase, not with the skill: cost per run is O(distinct directories in
  the diff + their depth), which dedupe makes much smaller than O(paths) — see §3.2.
- Retrieval is exact and deterministic — the path *is* the key. No embeddings, no vector store.
- The knowledge sits in the diff, the directory listing and the `git log` of the code it describes.
- Everything that reaches the reviewer is hashed, so a gate cannot be passed against context no gate
  ever scored.
- Zero behaviour change for a skill that declares no sidecar role.

**Non-goals**
- Replacing central rules. Cross-cutting invariants stay in the skill and stay gated by eval.
- Generating sidecars from source code (§7 — it produces confident restatement, not knowledge).
- Whetstone holding write credentials on source repos. Delivery is a PR the repo's owners accept.
- Semantic retrieval of any kind, for the same determinism reason the wiki is glob-keyed.

---

## 2. Storage

One directory, unbranded, hidden:

```
payments/
  .agents/
    context.md          # role-agnostic: what this subsystem is
    arch-review.md      # role overlay
    qa.md
  PaymentService.java
```

**A folder, not bare dotfiles**, for two operational reasons: sparse checkout is one stable glob
(`**/.agents/**`) that never needs updating when a role is added, and tool exclusion is one flag
(`--exclude-dir=.agents`) rather than a filename set that gets forgotten.

**Not named `whetstone/`.** Naming a directory in someone else's repo after the tool couples
thousands of paths to a vendor choice. `.agents/` reads correctly for arch review, QA generation and
code review alike. This is a proposed convention, not an established standard.

**One file per directory per role. No per-file sidecars.** File-specific claims go in
`## PaymentService.java` sections inside the directory file. That avoids a file explosion, keeps
`*.java` globs clean, and makes an intra-directory rename a heading edit.

**`context.md` is factored out** because every role needs the same subsystem description and three
copies drift.

**The role id comes from skill frontmatter, never the skill's folder name**, so forking
`arch-review` into `arch-review-v2` doesn't mean renaming sidecars across a monorepo.

### 2.1 Format

```markdown
---
role: arch-review
status: confirmed              # unconfirmed | confirmed | load-bearing
confirmed_at_tree: 9f2c1ab…    # `git rev-parse HEAD:payments/` when last verified
confirmed_by: run/2026-07-14/812
---

- Retries are capped at 3 — the upstream rate-limits at 4.
  <!-- src: HUB-45814#r411 @ 9f2c1ab -->

- Excepts R7 (no direct DB access from handlers): the reconciliation path is batch,
  not request-scoped.
  <!-- src: HUB-47733#r505 @ a71ce02, adr: ADR-22 -->

## PaymentService.java
- The only writer to `payments_ledger`.
  <!-- src: HUB-48163#r527 @ 3d90fe1 -->
```

**Every claim carries its source and is rejected without one.** Blind verification (§8) needs
something to check against beyond the claim's own plausibility, and the dead-claim sweep needs to
ask whether the originating constraint still holds.

`confirmed_at_tree` stores a **git subtree hash**, not a commit SHA. Git is already a Merkle tree;
`git rev-parse HEAD:payments/` changes iff something under `payments/` changed, so the comparison is
free and exact. It scopes incremental verification — it does **not** certify freshness. A whitespace
fix moves it; the model, reading the diff since, is the only thing that judges staleness.

**They live in the source repo, not a mirror.** Diff-adjacency is the entire argument for sidecars
over a central file, and `git mv payments/ billing/` carrying `.agents/` along atomically is worth
more than every benefit of a parallel tree. Mirror only where there is no write access, and expect
rename reconciliation (`git log --follow --diff-filter=R`) to be partly manual.

**Ownership is CODEOWNERS.** A rule on `payments/` already covers `payments/.agents/`.

---

## 3. Retrieval

**The harness resolves it. The model never decides whether to read a sidecar.**

An instruction like *"check the folder for `.agents/arch-review.md`"* is a probabilistic behaviour:
followed early in a review, quietly skipped on file 30 of 40. It also makes "considered and
dismissed" indistinguishable from "never loaded", which is the ambiguity that makes the current
dead-rules panel hard to act on.

Algorithm:

```python
def resolve(source_root: Path, paths: Sequence[str], role: str, *, caps: Caps) -> Sidecars:
    """Every `.agents/` file the given paths pull in, nearest-last, capped."""
```

**Phase A — collect candidates. No reads.**

1. For each path under review, walk from its directory up to `source_root`.
2. At each level record `.agents/context.md` and `.agents/<role>.md` if present.
3. Dedupe by path; order root-first so specific text sits nearest the question.

**Phase B — apply caps, then read.**

4. Over `max_files` → drop from the front (most general) until it fits.
5. Read the survivors, `stat()` first. A file over `max_file_bytes` is dropped, not read.
6. Over `budget` bytes → drop from the front again.
7. Return the ordered `[(path, content)]` **and** every drop with its reason — all hashed (§5).

Both caps drop the same way and report the same way, so there is one rule to learn: **general goes
first, nearest survives.** Drops are named in-prompt, the discipline `render_pages` already applies
at `MAX_PAGE_BYTES` (`reviewer/llm_reviewer.py:128`).

Ordering and drops must be deterministic, or two runs differ silently and the hash lies. What makes
dropping safe here is that it is hashed: a truncated set is not a degraded measurement, it is a
*different* one, and the gate can tell.

### 3.1 The ceiling is `source_root`, not a depth number

The walk stops at `source_root` and nowhere else. It is already declared per skill (§4) and already
the security boundary (§11) — one concept doing both jobs beats two that can disagree.

**No numeric depth cap.** Depth measures how the repo happens to be nested, not how much context is
relevant: `com/company/hub/payments/gateway/` is five levels before anything meaningful, a flat Go
repo is one. A fixed *N* would starve the first and over-read the second. A skill that wants a
tighter ceiling points `source_root` at the subtree it actually reviews.

For a monorepo where `source_root` genuinely is the whole tree, the escape hatch is a `boundary:
true` key in a `.agents/context.md` frontmatter: *this is the top of a self-contained subsystem, do
not walk past me*, authored by the people who own that subtree because they are the ones who know.
**Not in v1** — `source_root` covers the cases we have.

### 3.2 Caps, and why they mostly never bind

| cap | default | job | when it fires |
|---|---|---|---|
| `max_files` | 24 | resource — bounds the IO a single case can cause | before any read |
| `max_file_bytes` | 32_000 | shape — a sidecar this big has become `system-map.md` again | at `stat()` |
| `budget` | 20_000 | attention — what the model can actually hold | after reading |

Dedupe is why 24 is generous rather than tight. Candidates are a *set of directories*, not a list
per file: forty changed files across six directories sharing a four-level prefix collapse to roughly
ten distinct directories, and most of those have no `.agents/` at all (§3.4 — absence is normal, and
there is deliberately nothing pushing coverage up). The cost claim in §1 is properly **O(distinct
directories + depth)**, not O(paths).

`max_file_bytes` is a drop rather than a hard failure — a review with nine of ten sidecars still has
value — but an oversized sidecar is a defect, so it also joins the mechanical CI floor (§8) where
fixing it is cheap.

### 3.3 Scope is per-role

```yaml
# skills/architect-skill/SKILL.md frontmatter
sidecar:
  role: arch-review
  scope: subtree+imports     # code-review: diff-paths | qa: module+deps
  budget: 20000
  max_files: 24              # both caps are optional; these are the defaults
  max_file_bytes: 32000
```

`diff-paths` is the default. Arch review needs the other side of a boundary and the other side is
never in the diff, so it declares `subtree+imports` and gets its own budget.

**This block is the only place the caps are authored** — see §3.5. Nothing else may pass them, or
the two callers drift and the gate stops describing what runs.

### 3.4 Absence is normal

One line in the skill preamble: *no sidecar means no local context; do not search further, do not
infer what it would have said.* Without it the model treats a missing file as a puzzle and burns
tokens probing. There is deliberately **no coverage metric** — the moment one exists, someone fills
every folder and the tier becomes noise.

### 3.5 One collector, two callers

**Whetstone is not the only harness these skills run in.** The same skill folder gets installed into
Claude Code and run against a working tree with no Whetstone anywhere. If Whetstone injects sidecars
and the Claude Code path does something else — an instruction to go look, or nothing — then the gate
measures a retrieval that no user ever exercises. That is `patterns/rust.md` again
(`domain/run.py:331`) in a new place: guidance reaching the prompt through a door the hash does not
watch.

So retrieval is **one script, living in the skill folder**, and both callers run it:

```
skills/arch-review/
  SKILL.md
  tools/
    collect_sidecars.py        # the only implementation
```

```
$ git diff --name-only main | python tools/collect_sidecars.py --root . --paths -
```

- **Whetstone** imports it — `src/whetstone/sidecars/collect.py`, called in-process.
- **Claude Code** runs the copy `whetstone sidecars install` wrote to `<skill>/tools/`, per one line
  in `SKILL.md`.

Identical resolution then holds *by construction* rather than by convention, which is the only
version of this that survives a year of edits.

> **Built as import + installed copy, not shell-out.** The first draft had Whetstone subprocess the
> skill's copy, for isolation. Importing the canonical file is better on both counts that matter: it
> is literally the same code rather than a copy trusted to be the same, and it costs no process per
> case (a 200-case run at `k=2` would have spawned 200 collectors). The installed copy is what makes
> the skill self-contained elsewhere; `installed_state` compares it byte for byte and reports a
> mismatch at the plan, so the two cannot silently drift. Whetstone still validates every returned
> path against `source_root` — see below.

**It takes `--root` and `--paths`, and nothing else.** Caps, role and scope come from the skill's own
`SKILL.md` frontmatter (§3.3), which the script reads itself — one authored place, no flags to keep
in sync across two call sites, and no `step.yaml` dependency in a context where `step.yaml` does not
exist.

**Constraints.** Python stdlib only — a skill must not drag Whetstone in as a runtime dependency of
being *used*. No git: retrieval is pure filesystem, which keeps it fast, testable, and ref-agnostic
for the reason §5 gives. Never writes.

**Two output modes.** `--json` for Whetstone (files, drops, `context_hash`); markdown by default for
a model reading tool output, carrying the same `context_hash` in a trailing comment so the two modes
cannot disagree about identity.

#### The script is hashed

`collect_sidecars.py` decides what reaches the prompt, so it is guidance in every sense that matters
and must be **hashed into `reviewer_context_digest`** as a `file:`-shaped context value — the same
slice §5 puts sidecars in. Skip this and it becomes precisely the hole this design exists to close:
`skill_hash` covers the body, companion pages, eval cases, wiki and index (`domain/run.py:319`) — it
does not cover an arbitrary `tools/*.py`, so a rewritten collector would leave every gate reading
`gated` while the reviewer's context changed underneath it.

Editing the collector therefore retracts baselines. Correct: it changed what every case reads.

**The traversal guard lives in the collector**, so the Claude Code path has it too: every
candidate path is resolved and refused if it leaves `source_root`, and only `.agents/(context|
<role>).md` is ever opened. Whetstone additionally re-checks what comes back, because the installed
copy lives in an editable skill folder (§11).

#### The residual, stated plainly

In Whetstone the collector always runs; the harness calls it. In Claude Code the model has to invoke
it. That is a real probability of being skipped, and this document does not pretend otherwise — it
is *one* instruction at the top of a review, not forty per-file decisions, which is the difference
between a step and a habit. It is also detectable after the fact: the output carries an explicit
"no sidecars loaded" sentinel, so a review that never called it is distinguishable from one that
called it and got nothing.

---

## 4. Repo resolution — reuses what shipped

`agentic-reviewers.md` §4 already solved this and Phase 1 is built. A skill declares:

```yaml
context:
  source_root: { env: HUB_REPO_ROOT, required: true }
  source_ref:  { env: HUB_REPO_REF, pin: true }   # a label, recorded and hashed; nothing reads it back
```

`{ env: NAME }` commits the *name*, never the value — correct for a path that differs on every
checkout. Preflight already fails at the plan with `context.source_root: HUB_REPO_ROOT is not set`
(§4.3), which is exactly the loud failure sidecars need: an unresolvable repo must never degrade to
an empty sidecar set, because that produces a valid-looking hash over missing context and forks gate
results by checkout location.

**A `repositories.json` map is not needed for v1.** It is only better when one skill spans many
repos; revisit if that materialises. Do not build it now.

**Sidecars are ordinary files and get no VCS handling of their own.** They are read from
`source_root` exactly as the reviewer reads code: same tree, same state, same moment. Whetstone
does not resolve a ref for them, because it does not resolve one for the source either — `source_ref`
is recorded and hashed today but never read back, and no reviewer path performs a checkout
(open question #3 in `agentic-reviewers.md`). When that general answer lands — a managed worktree, a
pinned read, whatever it turns out to be — sidecars inherit it for free.

Giving them their own ref mechanism would be strictly worse than having none: two ways to read one
tree can *diverge*, and code from one snapshot judged against sidecars from another is a failure
mode that does not exist today.

**Practical note for a monorepo:** you need the sidecars, not the source. A sparse checkout limited
to `**/.agents/**` gives every sidecar in a 2M-line repo for a few megabytes.

---

## 5. Determinism and hashing (the crux)

This is what decides whether sidecars are gateable, and `run.py:331` already documents the exact
failure being avoided:

> While it sat outside this hash, rewriting a referenced page from "never unwrap" to "always unwrap"
> left the digest byte for byte identical — so the console went on showing `gated`.

Sidecars are that hole, thousands of files wider.

**Rejected: fold all sidecars into `skill_hash`.** Any edit anywhere would revoke every gate,
including for cases that never touch that folder.

**Rejected: hash `source_ref` alone.** Two reasons. Every unrelated commit to a busy monorepo would
retract every gate. And `source_ref` is an operator *assertion* — nothing reads it back or checks
the tree against it — so it identifies what someone claimed was checked out, not what was read.

**Chosen: a per-case context hash over what actually loaded.**

```python
context_hash(case, role) = H(sorted (path, sha256(content)) for the resolved set, plus the drop list)
```

Recorded per `CaseRun`. **Comparability rule: two measurements of a case are comparable iff their
`context_hash` matches.** A source commit that touches nothing the case pulls in invalidates
nothing — the Merkle property paying rent. This is the granularity `case_set_hash`
(`domain/run.py:353`) already reasons at.

**Hashing content is what makes §3's "no ref handling" safe.** The hash is over what was actually
read, so it is ref-agnostic by construction: it does not need to know which snapshot the bytes came
from, because the bytes *are* the identity. A pinned ref would be a redundant second identity for
the same fact, and the weaker one — a ref asserts which tree was available, content records what was
read. Two runs against different branches that happen to carry identical sidecars are, correctly,
the same measurement.

No new run-level ref field is needed: `reviewer_context_digest` already carries a declared
`source_ref` when one is pinned, and the per-case content hash covers what was read regardless.
`skill_hash` is unchanged.

**Where this plugs in.** A sidecar is a `file:`-shaped context value whose paths are computed per
case rather than declared, so it belongs in the same slice `ResolvedContext.hashable` already
carries. Two things fold into `reviewer_context_digest`, and therefore into `BaselineKey`:

- the **declaration** — role, scope, and the *effective* caps, not the declared ones, so a shift in
  a default is as visible as an edit to the frontmatter;
- the **collector script** itself (§3.5), because it decides what the declaration means.

Both must forbid baseline reuse when they change: each one alters what every case reads. The
per-case content hash lives on `CaseRun` alongside them. None of it touches `skill_hash`, which
stays a pure function of the `Skill`.

**Why sidecars are hashable when agent reads are not.** An agent reviewer chooses what to open at
runtime, so no hash can constrain it — that residual is surfaced by `reviewer_trace` and `k>1`
variance instead. Sidecar injection is the opposite: the *harness* selects the files, from the diff
paths, deterministically. That is exactly the part that can and should be hashed.

---

## 6. Creation — the triage destination

Triage today has one destination; the judgment being made has three. This is the change that makes
the tier fillable.

| destination | writes | gated by |
|---|---|---|
| **rule** | central skill + `meta.yaml` provenance | eval: catches without regressing |
| **context** | eval case **+** sidecar claim | attribution |
| **exception** | eval case **+** sidecar claim citing R*n* | attribution; the rule stays strict |
| **reject** | — | — |

**Exception is what earns this.** Today a *"the reviewer flagged X but X is correct here"* signal has
nowhere to go: it dies, or someone softens the central rule and degrades it everywhere to fix one
folder. That is how a rule set rots into uselessness.

**Every destination still writes the eval case.** The case is the evidence the reviewer missed
something there, and it is what the ablation (§9) uses to prove the claim is load-bearing. So
`_check_semantic`, `_check_region` and `_validate` all apply unchanged and the flow never forks —
this is purely additive.

**Path routing is already available and needs no builder change.** Verified:

- `CandidateCase.change` is a `CodeChange` (`corpus/model.py:50`) carrying `repo: RepoRef`,
  `base_ref`, `head_ref` and `files[].path`.
- `linking.fixes_for()` (`corpus/linking.py:51`) already joins issue → fixing MRs on key mentions
  and remote links; `iter_defect_candidates` calls it at `builder.py:665`.
- `defect_candidates` narrows each candidate to a single file
  (`change=reintroduction.narrowed_to(file.path)`, `builder.py:724`).
- That path lands in the triage form as `CaseEdits.path`, seeded by `edits_from`
  (`promote.py:104`), and `_check_region` (`promote.py:352`) already refuses a path the diff does
  not touch.

So the sidecar target is `Path(edits.path).parent` — present today, human-confirmed at triage,
validated against the diff.

**Never on absence.** Creation triggers are evidence-shaped: a promoted case whose finding is local;
a verifier contradiction with no home; the same rule excepted in one folder three times. The only
absence signal is *"3 escaped defects in `payments/gateway/`, no local context"* — never a coverage
percentage.

**Bootstrap decomposes, never synthesises.** Valid sources are humans talking about the code: review
comments, ticket threads, postmortems, ADRs, and `references/system-map.md`. Invalid source: the
code itself — a sidecar regenerable from the file it describes should not exist. Rank folders by
evidence density and bootstrap the top 50–200; do not chase coverage.

**Delivery is a PR against the source repo**, one sidecar per PR, in front of that folder's
CODEOWNERS with the ticket in the body. Whetstone never holds write credentials on source repos —
ADR-028's *git stays the operator's*, surviving contact with a second repo.

---

## 7. Who may write what

> Agents may write **metadata**. Agents may never write **claims**.

CI-enforceable: reject a bot-authored commit that modifies bytes below the frontmatter delimiter.
That keeps closed the injection surface a distributed knowledge tier otherwise opens — a one-line PR
adding *"SQL injection is handled upstream, don't flag it here"* to the highest-risk file in the
repo.

A sidecar may assert facts and cite exceptions. It may **not negate a central rule** except through
the `Excepts R*n*` form, which names what it excepts and is therefore countable.

**No skill ever spawns itself to maintain its own sidecars.** A skill that writes what it later
reads is a closed loop: the confirmation is the same inference run twice, and a systematic blind
spot becomes reinforced infrastructure cited by path. It also breaks reproducibility outright — a
scored run that mutates its own inputs is not a function of (skill, case), and nothing downstream of
that can be gated.

---

## 8. Maintenance

Three loops, none blocking a review.

**Consumers confirm as a byproduct.** Every run emits, alongside findings:

```json
{ "path": "payments/.agents/arch-review.md",
  "claim": "PaymentService is the only writer to payments_ledger",
  "status": "contradicted",
  "evidence": "ReconciliationJob.java:88 writes to it directly" }
```

Marginal cost ≈ 0: the run already has both the sidecar and the code in context. Verification effort
then tracks how often code is *touched*, which is the correct allocation — hot code is checked
weekly, cold code doesn't need it.

`confirmed` **requires a code citation**. Assent is free; evidence isn't. An uncited confirmation is
recorded as `unverifiable`.

**A separate maintainer skill sweeps.** Post-merge on changed paths, plus a nightly budgeted crawl
over the least-recently-confirmed (the only thing that ever reaches cold code). Not a preflight: a
blocking pre-review pass duplicates reads, taxes every review's latency, and has no good answer to
finding a contradiction mid-review.

**Verification is blind.** Give the verifier the diff since `confirmed_at_tree` and ask *what would a
reader of this folder need to know?* — then compare its independent account against the stored
claims. **Never** show it the claim and ask "still true?"; it will anchor and confirm. This is the
single detail most easily lost, and losing it turns the loop into theatre.

**Contradictions become triage candidates.** The maintainer stamps `status` and files; a human
promotes the edit. Confirmation is automatic, correction is gated.

**Stamp on change, not on check.** A claim edit commits; a routine "checked, unchanged" goes to
Whetstone's ledger. `git blame` then answers when the knowledge last *moved*, and the console
answers when it was last *verified*.

**The mechanical floor**, cheap enough for a pre-commit hook: a sidecar whose directory no longer
exists, malformed frontmatter, a claim with no `src:`, a sidecar over `max_file_bytes` (§3.2 — it
has become the central file this design exists to break up), a bot commit touching claims. 100%
decidable, should block CI.

---

## 9. Trust ladder, and the safeguard that matters most

```
unconfirmed  →  confirmed  →  load-bearing
```

- **unconfirmed** — agent-authored or bootstrap-decomposed. **Never injected into a consuming run.**
- **confirmed** — blind verification agreed, with a citation. Injected.
- **load-bearing** — human-promoted, or an ablation showed a finding depends on it.

Agents may write freely; nothing they write is believed until something independent agrees. Same
ladder as `working → draft → promoted`, pointed at a different artifact.

### 9.1 `--no-sidecars` is a standing evaluation mode, built first

The realistic failure is not a wrong claim. It is fifteen files of mediocre context on every run,
attention diluted, findings quietly worse — invisible, because there is no baseline without them.

Every other safeguard here addresses individual claims. This is the only one that measures the tier
as a whole. Score the corpus with and without injection and compare recall / FP rate. If the number
doesn't move, the tier is costing tokens and attention for nothing.

**Measure it or delete it** — the standard already applied to guidance.

### 9.2 What the first ablation said

`examples/sidecar-review/` is a built fixture: a source tree with five `.agents/` files at three
depths, and eight cases in three groups — sidecar-dependent catches, sidecar-dependent silences,
and controls whose folders carry no `.agents/` at all. `qwen3-coder:30b` via Ollama, k=3, two runs
of each arm:

| | recall | fp_rate |
|---|---|---|
| sidecars on | **0.733**, 0.733 | 0.444, 0.444 |
| `--no-sidecars` | **0.400**, 0.533 | 0.000, 0.222 |

**Recall goes up and false positives go up with it**, and neither half is noise. The recall gain is
entirely on the sidecar-dependent catches — `ledger-second-writer` and `retry-cap-raised` both move
0.00 → 1.00 — which is the tier doing exactly what it is for. The false-positive cost is almost
entirely one case, for one understood reason, below.

**This clears the mechanism, not the tier.** The sidecars and the cases were authored together, so
the direction of the recall result is not evidence about anyone's codebase; what it establishes is
that the text reaches the model, the model reasons from it, and the two arms are distinguishable by
digest. The efficacy question §9.1 poses still needs a real corpus.

**Two costs this document did not predict.**

*Concurrence findings.* Given an exception, the reviewer reports a finding whose message says the
code is *fine* — *"increments the counter, which aligns with the documented exception for R3"* —
rather than staying silent. Scored a false positive, correctly, and it is most of the FP gap above.
The score cannot distinguish it from the reviewer disagreeing with the sidecar, which is a different
bug with a different fix; only the message can. `_sidecar_block` now states that honouring an
exception means reporting nothing, and that instruction is **not** known to be sufficient — this
model produced the concurrence finding with and without it. Recorded rather than closed.

*The confirmation loop is not free.* §8 argues its marginal cost is ≈ 0 because the run already
holds both the sidecar and the code. True of tokens, false of attention: asking for claim verdicts
moved recall 0.733 → 0.600, twice, and the case it lost was `retry-cap-raised`. So
`sidecar: confirmations:` defaults to **false** and is part of the hashed declaration, which makes
turning it on retract baselines rather than quietly change what was being measured. §8 is amended
by that: *a byproduct with no marginal cost* is the intent, not the measurement.

---

## 10. Records

`CaseRun` gains `sidecars: {paths: [...], dropped: [...], context_hash: str}` — which files were
loaded, which the budget dropped, and the identity of the set. Without this, "the reviewer never
loaded it" and "the reviewer read it and disagreed" are indistinguishable, and the whole maintenance
loop loses its input.

The drill-down shows the loaded set per case, so a surprising miss can be checked against what the
reviewer actually had.

---

## 11. Security — one deliberate departure

`agentic-reviewers.md` §7 states: *"Whetstone passes `source_root` and never traverses it itself, so
no path-escape surface is added on whetstone's side."*

**Sidecars require Whetstone to traverse the source tree.** That is a real departure and is
justified: if the reviewer subprocess collects its own context, Whetstone cannot hash what was read
and §5 collapses. Mechanical, hashable retrieval is the whole point.

Constraints on the traversal, enforced in `collect_sidecars.py`:

- Read only files matching `.agents/(context|<role>).md` — never arbitrary paths.
- Resolve every candidate path and refuse anything that escapes `source_root` after
  `Path.resolve()`; do not follow symlinks out of the tree.
- Stop the ancestor walk at `source_root` (§3.1) — it is the boundary, so the walk and the guard
  cannot disagree about where the tree ends.
- Apply `max_files` and `max_file_bytes` before reading, not after (§3.2).
- Never write. Sidecar *creation* goes out as a PR (§6), never as a filesystem write.

And re-checked in `sidecars.py` on the way back, because the collector lives in an editable skill
folder: every returned path resolved, under `source_root`, and `.agents/`-shaped before it is hashed
or shown. The guard belongs in the collector so the Claude Code path has it too; the re-check exists
because Whetstone should not take a skill folder's word for which files it read.

Sidecar content reaches the model, so it is source egress in the sense §7 describes — the plan
should say how many sidecars will be sent and from which repo.

---

## 12. Failure modes

| failure | behaviour |
|---|---|
| `source_root` unresolvable | **fail the plan**, never an empty set (else the hash forks by machine) |
| sidecar unreadable / not utf-8 | fail the run, naming the file — same as a bad guidance page |
| `budget` or `max_files` exceeded | drop general-first, deterministically, name the drops in-prompt; the drop list is hashed, so the truncated set is a *different* measurement, not a silently worse one |
| single sidecar over `max_file_bytes` | drop it with a named reason, and fail it at the CI floor (§8) where it is cheap to split |
| case path absent from the resolved source tree | resolve to no sidecar, **report it** — that is the orphan signal surfacing through evals |
| role declared, no sidecars anywhere | valid; behaves exactly like today |
| sidecar edited mid-gate | `context_hash` differs between sides → refuse the comparison |
| collector script missing / non-zero exit / unparseable | fail the run naming it — it is the retrieval path, and a review without it is not the reviewer being gated |
| collector returns a path outside `source_root` | refuse the whole result (§3.5) — a partial answer here is a path-escape that got halfway |

---

## 13. Files touched

**New**
- `skills/<role-skill>/tools/collect_sidecars.py` — **the algorithm**: the ancestor walk, the caps,
  the traversal guard, `context_hash()`. Stdlib only, no git, never writes (§3.5).
- `src/whetstone/sidecars.py` — *not* a second implementation. Runs the collector, validates its
  output paths against `source_root`, and folds the script + declaration into the context digest.
- `tests/unit/test_sidecars.py`, and tests for the collector runnable without Whetstone imported.

**Changed**
- `src/whetstone/core/loader.py` — parse the `sidecar:` frontmatter block into the `Skill` model.
- `src/whetstone/reviewer/llm_reviewer.py` — inject alongside `render_pages` (:128); the built-in
  reviewer is the common case and must not be an afterthought.
- `src/whetstone/agent/runner.py` — `_system` (:134) lists sidecars as *given* context, not as files
  to fetch; agent mode must not reintroduce model-decided retrieval.
- `src/whetstone/domain/run.py` — `_feed_context` for the `sidecar:` declaration; `CaseRun.sidecars`.
- `src/whetstone/promote.py` — `CaseEdits.destination` + `excepts_rule_id`; the branch at :312;
  `PreparedCase.sidecar` as a **separate field** — *not* in `files`, which `commit_promotion`
  writes entirely under `skills_repo` (`candidates.py:558`).
- `src/whetstone/ui/routers/candidates.py` — destination in the triage payload; gate `_meta_yaml`
  on `destination` rather than on `rule_id` doubling as a flag (:616).
- `src/whetstone/preflight.py` — report the sidecar count and source repo before spending.
- `ui/src/api/client.ts`, the triage screen — the destination control and its pre-fill.
- `docs/decisions.md` — an ADR for the §11 departure and the per-case hash choice.

**Free validation to add while there:** `prepare()` should refuse a promotion whose
`candidate.change.repo` is not among the skill's declared sources — four lines next to the
`_RULE_ID` check (`promote.py:282`), and much cheaper before there are promotions in flight.

---

## 14. Rollout

Value arrives at step 2. Everything after is earned.

1. ✅ **Resolution + retrieval.** `collect_sidecars.py` first and standalone — it is the artifact both
   harnesses share, and writing it against Whetstone's internals is the one mistake that cannot be
   undone later. Then `sidecars.py` to run it, the frontmatter block, injection into the built-in
   reviewer, loud failure on unresolved `source_root`. Hand-write ten `.agents/context.md` files
   decomposed from `references/system-map.md`. **Verify it works from Claude Code with Whetstone
   uninstalled** — that is the second caller, and it is easiest to check before there is anything
   to keep working.
2. ✅ **`--no-sidecars` ablation** — the flag, and an ablation run recorded as a distinct
   measurement so it can never be confused with a normal one.
   → **Exit criterion. If recall doesn't move, stop here.** **Recall moves** — see §9.2. The
   number is from a built fixture, not a real corpus, so it clears the mechanism and leaves the
   efficacy question open.
3. ✅ **Hashing.** `context_hash` per `CaseRun`; the declaration and the collector's own bytes in
   `reviewer_context_digest`, and therefore in `BaselineKey` and the C6 publish check. Gateable.
4. ✅ **Triage destinations** + PR delivery. `rule | context | exception` on `CaseEdits`, with
   `reject` staying the separate endpoint it already was. Every destination still writes the eval
   case. A claim comes back as `PreparedCase.sidecar` — a patch, a branch, a title and a PR body —
   and never in `files`, which `commit_promotion` writes under `skills_repo`.
5. ✅ **Consumer confirmations** — `sidecars/confirm.py`, a per-claim verdict asked as a byproduct,
   matched back to a real claim or dropped, appended to a ledger on save. **Opt-in**, against this
   document's expectation: see §9.2.
6. ✅ **Maintainer skill** — `sidecars/maintain.py` and `whetstone sidecars verify`. Two calls: a
   blind account of the folder, then a comparison. Post-merge with `--folder`, cold crawl with
   `--limit`, least-recently-verified first. Writes no sidecar.
7. ✅ **Mechanical CI floor** — `sidecars/floor.py` and `whetstone sidecars check`. Uncited claims,
   off-ladder frontmatter, oversized files, orphaned headings and folders, role/filename mismatch,
   and the bot-write boundary from a diff. Exits 1; no model.

**Also built, and not in the original plan:** the trust ladder is now *enforced*, in `collect.py`
rather than in Whetstone's half, so the Claude Code caller climbs it too. An `unconfirmed` sidecar
is dropped with a reason and the drop is hashed — promoting a claim later therefore invalidates the
runs taken while it was withheld, which is the same discipline every other cap already follows.

---

## 15. Verification

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
cd ui; npx tsc --noEmit; npx vitest run; npx vite build
```

**Tests to write**

- `test_sidecars.py` — ancestor walk order (root-first); the walk stops at `source_root` and never
  above it; role file + `context.md` both collected; each of `max_files`, `max_file_bytes` and
  `budget` drops general-first and reports its reason; symlink escaping `source_root` refused;
  non-`.agents` path never read; `context_hash` stable under reordering and changed by content, by
  a drop, and by the declared scope. **Run against the collector as a subprocess**, so what is
  tested is what both harnesses call.
- The collector, **imported with no `whetstone` on `sys.path`** — the dependency claim in §3.5 is
  the kind that rots silently, and one test pins it.
- `reviewer_context_digest` moves when `collect_sidecars.py` changes by a byte, and when an
  effective cap changes without the frontmatter changing.
- `test_run.py` — `skill_hash` unchanged for a skill declaring no role (nothing existing is
  invalidated by this landing); `_feed_context` moves the hash when `scope` or `budget` changes.
- `test_promote.py` — a `context` destination emits a case **and** a sidecar; the sidecar is not in
  `PreparedCase.files`; an `exception` destination requires `excepts_rule_id`; a candidate from an
  undeclared repo is refused.
- `tests/api/` — the destination round-trips through preview/promote; undo leaves the sidecar PR
  alone (it cannot unmerge someone else's repo, and must not try).
- `test_docs_match_reality.py` — no route writes to `source_root`; the README's claim about what
  reaches the reviewer names sidecars.

**End-to-end**, in the seeded console: declare a role on a skill pointed at a scratch repo with two
`.agents/` files → run an eval → the drill-down names both files → delete one → `context_hash`
changes and the gate refuses the stale comparison → triage a candidate to `context` → the prepared
sidecar names the right folder.

---

## 16. Open questions

1. ~~`SKILL.md` frontmatter or `evaluate/step.yaml` `context:`?~~ **Settled: frontmatter**, by §3.5.
   The collector must read the declaration when it runs under Claude Code, where `step.yaml` is a
   Whetstone concept that is not there. `context:` keeps owning source *access* (`source_root`),
   frontmatter owns *retrieval* — the split is along the seam of who needs to read it, not an
   arbitrary one.
2. ~~`source_ref` vs `HEAD` for scoring.~~ **Not a sidecar question.** Sidecars are ordinary files in
   the source tree (§3) and inherit whatever Whetstone decides about reading source at a ref. That
   decision belongs to `agentic-reviewers.md` open question #3, where it applies to code and
   sidecars alike. Worth knowing that today's answer is *neither* — the reviewer reads whatever is
   checked out — so an agent-scored corpus mined from old merge requests already reads current code
   against historical diffs. Sidecars make that no better and no worse.
3. **Does the maintainer skill share the corpus?** Its cases are (diff, stale sidecar) → contradiction
   found. That is a different eval shape and may want its own directory, like `task-runs` is separate
   from `runs`.
4. **Multi-repo skills.** Deferred (§4). The `repositories.json` map returns here if it materialises.
5. **Sidecars for a case whose repo differs from the skill's declared source.** Refuse, or resolve
   per case from `change.repo`? Refusing is proposed; per-case resolution is more general and more
   surface.
6. **One collector per role skill, or one shared copy?** §3.5 puts it in the skill folder, which is
   what makes it self-contained under Claude Code — but four role skills then carry four copies that
   drift, and each is separately hashed. A plugin-level shared tool fixes drift and breaks
   self-containment. Leaning: copies for v1 (there is one role skill), with the CI floor asserting
   they are byte-identical the moment there are two.
7. **A repo with no Python.** The collector is stdlib Python 3.9+, which most Claude Code users
   have and a Node or JVM shop may not. A second implementation is the one thing §3.5 exists to
   forbid, so the answer is probably a prebuilt binary or accepting the dependency — not a rewrite.
