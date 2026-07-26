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
