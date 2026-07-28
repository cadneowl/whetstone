# ANTI_ROT_PLAN — keeping skills sharp across hundreds of MRs and defects

> **Implementation status (updated 2026-07-27, branch `anti-rot/phase-0`)**
>
> - **0.1 — DONE** (commit "Name the judge on every run and gate record"): `judge_identity()` in
>   `judge/llm_judge.py` (prompt text now a hashable template constant, characterization test pins
>   the rendered prompt byte-for-byte); `judge_hash` on `RunRecord`, `RunSummary`, `GateRecord`;
>   SQLite schema bumped to v3 (rebuild-from-files migration); stamped in `service.record_eval`
>   and `service.record_gate`. UI: run drill-down names the judge beside the backend; the skill
>   History tab draws a seam where the judge changed between adjacent runs.
> - **0.2 — DONE** (this commit): `[meta_eval] dir` config (+ `WHETSTONE_META_EVAL_DIR`);
>   `meta_eval/disputes.py` (`Dispute`, `DisputeStore`, stable ids so re-ruling replaces);
>   `GET/POST /api/runs/{run_id}/disputes` in `ui/routers/runs.py` — the POST reconstructs the
>   (finding, expectation) pair from the run record's snapshot, stamps the run's `judge_hash`,
>   and refuses pre-snapshot records with a "re-run the eval" message. Agreeing rulings are
>   stored too (see the module docstring for why). UI: each judge verdict in the run drill-down
>   has same/different ruling buttons + a ruled badge (`RulingLine` in `RunDetail.tsx`), hidden
>   when read-only or when the record predates snapshots. Tests: `tests/api/test_dispute_routes.py`
>   (note: API error bodies are `{"message": ...}`, not `{"detail": ...}`).
> - **1.1 — DONE** (this commit): judge doctrine as versioned text. `judge/spec.py`
>   (`JudgeSpec`, `load_judge`, `builtin_judge`; frontmatter optional, empty body refused with
>   the way out); `[judge] dir` config (default `judges/default`, env `WHETSTONE_JUDGE_DIR`);
>   `LLMJudge(system=...)` + `judge_identity(system=None)` hash the *effective* text, so a
>   JUDGE.md that is word-for-word the default re-baselines nothing (tested). Threaded through
>   `run_eval`/`record_eval`/`gate_skills`/`record_gate` and both CLI + console-job call sites.
>   `GET /api/judge` (new `ui/routers/judge.py`) reports doctrine, identity, builtin-vs-file,
>   and ruling counts; console has a **Judge** nav tab (`routes/JudgePage.tsx`). Dogfood
>   `judges/default/JUDGE.md` committed with the builtin text verbatim. A malformed JUDGE.md is
>   422, never a silent fallback to the builtin.
> - **Deferred from 1.1** (deliberately): editing JUDGE.md from the console via the
>   GuidanceEditor/staging-branch machinery — display is read-only for now, edits happen in the
>   file; and the meta-eval **gate** on doctrine changes, which is 1.3's ratchet.
> - **1.2 — DONE** (this commit): diff-grounded cascade. `judge/cascade.py` (`GroundedJudge`
>   with `GROUNDED_TEMPLATE` in llm_judge.py, `CascadeJudge` bound per case,
>   `CascadingJudgeFactory` + `judge_for_case` seam in `core/harness.py`); `judge:` block in
>   evaluate/step.yaml (`JudgePolicy` in steps.py: `escalate_below` default 0.0 = off,
>   `max_diff_bytes`); `Match.tier`/`Match.prior` → `JudgeVerdictRecord.tier`/`prior` (one final
>   verdict per finding, escalation trail nested so `matched` aggregation is unchanged);
>   escalation triggers on low confidence in either direction; no-hunk escalation keeps the
>   tier-1 verdict rather than paying for an ungrounded repeat. Cascade config folds into
>   `judge_identity(escalate_below=…)` — different threshold, different instrument, visible
>   seam. Cost banner doubles the judge share and says why (`plan_eval(judge_cascade=…)`).
>   Drill-down shows a "grounded" chip + the tier-1 prior line. Scaffold + skill-pipeline.md
>   document the block. Rollout default is OFF — enabling is a per-skill choice.
> - **1.3 — DONE** (this commit): the rising bar. `MetaEvalReport` gains the missed/spurious
>   split; `load_judge_corpus(meta_eval_dir)` = optional `fixtures.json` seed + all drill-down
>   rulings. `meta_eval/ratchet.py`: `JudgeEvalRecord` (per-doctrine measurement, durable),
>   `RatchetStore.bar()` = max(floor 0.8, best binding accuracy − 0.02), one-way by
>   construction; a measurement under `MIN_PAIRS_FOR_BAR` (10) is reported but never binds — a
>   bar set by three lucky pairs would punish every future judge for a coin flip. `whetstone
>   judge eval` CLI (exit 1 below bar, CI-friendly) + `POST /api/jobs/judge-eval[/plan]` (empty
>   corpus is a named 422, not a zero-call run; JobKind gained "judge-eval"). Judge page shows
>   accuracy / missed-spurious / bar / clears-or-misses and launches the measurement
>   (LaunchButton, cost-confirmed; `pairs_total` gates the button).
> - **Deferred from 1.3** (deliberately): per-tier cascade accuracy — labeled pairs carry no
>   case diff, so judge-eval measures the pairwise judge; capturing the diff into `Dispute` at
>   mint time unlocks grounded-tier measurement later. A hard "adopt" gate on doctrine changes —
>   currently the bar is enforced by exit code (CI) and visible verdicts (Judge page), not by
>   blocking runs. Inbox surfacing of "rulings pile up unmeasured".
> - **Next up: Phase 2.1 — the holdout split** in `sampling.py` (hash-partition, digest
>   blindfold in `improve.py`, per-partition scores in gate output, targeted-must-be-train
>   rule), then 2.2 tiers → 2.3 saturation probe → 2.4 dedup. Phase H health endpoint skeleton
>   should land with 2.1 so the divergence number has somewhere to live.
> - Phase H note: the health endpoint skeleton (`GET /api/skills/{id}/health`) has NOT been
>   started; disputes-pending count now lives on `GET /api/judge` and belongs in the health
>   payload's `judge` block when it lands.

This is a handoff document. It assumes the implementer (you) has not read the conversation that
produced it, so it carries its own reasoning: every step says *what* to build, *why it exists at
all*, *where* it lands in this codebase, and *how you know it's done*. Do not strip the "why"
sections when updating this file — the reasoning is what stops a future refactor from quietly
deleting the point of a mechanism while preserving its shape.

**The one-paragraph thesis.** Every number Whetstone produces flows through one pipeline:
*judge verdicts → case outcomes → scores → gate decisions*. Skills rot at three points on that
pipeline: the **instrument** miscounts (judge error), the **test set** stops representing reality
(corpus rot), or the **accumulated guidance** decays (entropy). The phases below are ordered by
dependency: fix the instrument first because everything downstream reads its output, then the
corpus, then the capabilities that change the slope rather than patch the level. A standing rule
throughout: **no backend mechanism ships without its console surface** — the operator asked to
"see the state of the skills," and a health signal that only exists in a SQLite column is a health
signal that rots unread.

---

## 1. The system today — code map

Read these before writing anything. Paths are relative to repo root.

### Scoring pipeline
- `src/whetstone/domain/eval_model.py` — `EvalCase {id, kind, change: CodeChange, expect, provenance}`.
  **The case already carries its own diff** (`change`) — the diff-grounded judge needs no new
  storage. `Provenance` records source/ref/human_signal and `semantic_drafted_by`; the
  `PRECISION_EVIDENCE` map classifies negative-case evidence as `confirmed` / `silence` /
  `unclassified` (read its comments — the evidence-mix idea recurs in the Health UI).
- `src/whetstone/core/matching.py` — structural prefilter (`eligible_indices`: path, line range,
  severity) then `evaluate_expectation`, which walks eligible findings through the judge and
  **short-circuits at the first match**. Records every verdict as `JudgeVerdictRecord
  {finding_index, matched, confidence, reason}` (in `domain/run.py`). The `confidence` field is
  recorded today and consumed by nothing — Phase 1 gives it a job.
- `src/whetstone/judge/` — `base.py` (Judge protocol), `deterministic.py` (regex stand-in),
  `llm_judge.py` (the real one). The LLM judge sees **two sentences and two locations, nothing
  else**: expectation `semantic` + where, finding `message` + path/line. No diff, no code, no
  guidance, no wiki. Its system prompt is a hardcoded `_SYSTEM` string.
- `src/whetstone/core/scoring.py`, `core/harness.py` — trials, aggregation into `SkillScore`.
- `src/whetstone/core/gate.py` — `GateConfig` (strict defaults: no recall loss, no new FPs, no
  case regressions) + `targeted_cases`. Read the comment at `GateConfig.targeted_cases`: without
  targeted cases "the gate only ever blocks *regressions* … it is a rot guard, not a sharpening
  one." That sentence is the seed of Phase 2.
- `src/whetstone/sampling.py` — deterministic (`sha256(seed:case_id)`), stratified by case kind,
  targeted cases always included. Both gate sides see one identical draw. `SamplePolicy` lives in
  `steps.py`. Phase 2.1 extends this file.
- `src/whetstone/service.py` — wires reviewer + judge (`LLMJudge(counted, effort=judge_effort)`
  ~line 122); both share one LLM client per run.

### Measurement of the measurement
- `src/whetstone/meta_eval/evaluate.py` — judge accuracy vs human-labeled pairs, loaded from a
  **static JSON fixture**; `JUDGE_ACCURACY_FLOOR = 0.8`.
- `src/whetstone/meta_eval/drafting.py` — measures the expectation drafter with genuine-vs-decoy
  probe findings at the same location. Its module docstring names the two judge error kinds and
  why **spurious** (different problem judged to match) is the dangerous one: "the case has stopped
  discriminating: it will now pass on almost any output … because nothing ever goes red." Phase
  2.3 generalizes this probe technique to the live corpus.

### Records and provenance
- `src/whetstone/runs.py` — run records on disk + SQLite index. Columns include `skill_hash`,
  `guidance_hash`, `backend`, `model`. There is a schema version and a `_rebuild` path — use them
  for the Phase 0 migration; do not hand-roll one.
- `src/whetstone/llm/factory.py` — `Backend` exists so "a score is attributable to a specific
  backend and model, not to whatever the environment held that day." Phase 0 extends that exact
  principle to the judge.
- `skills/<id>/meta.yaml` — owner, references, **rule → signal provenance** (feeds the dead-rule
  report, Phase 5).

### Loop machinery
- `src/whetstone/improve.py` + `docs/skill-pipeline.md` — clustered failure digest (bounded by the
  host, not the step), staged on `whetstone/skill/<id>` via `prepare_guidance`, `--instruction`
  passthrough (never silently dropped). Note for Phase 2.1: **the digest is where holdout cases
  must never appear.**
- `src/whetstone/inbox.py` — one ranked next-action per skill
  (`propose > gate > triage > score > improve > nothing`). New rot signals become new inbox action
  kinds, not a new page.
- `src/whetstone/gates.py` — stored gate evidence (C6: a passing gate unlocks Propose; content
  changes retract it via `skill_hash`).
- `src/whetstone/reviews.py` — adjudicating live review output; mints `finding confirmed /
  rejected / missed` signals (see `eval_model.py` constants). This is the production feedback
  loop; Phase 5's KPI reads from it.
- `src/whetstone/wiki.py` + update step — wiki staged on the skill branch, path-glob retrieval
  (deliberately not semantic: retrieval must be a pure function of the diff so both gate sides see
  identical context), wiki hash folded into `skill_hash`.

### Console (FastAPI + React 19 + React Query)
- Routers in `src/whetstone/ui/routers/`: `skills.py` (`GET /api/skills`, `GET /api/skills/{id}`,
  `GET /api/skills/{id}/cases/{case_id}`), `runs.py` (`GET /api/runs`, `GET /api/runs/{run_id}`,
  `/report`), `inbox.py` (`GET /api/inbox`, `POST /api/inbox/check`), `jobs.py` (plan/launch pairs
  for eval/gate/improve/review with per-launch `provider`/`model` override — see `_pick`),
  `meta.py` (config, model choice, git status/propose).
- Routes in `ui/src/routes/`: `SkillsIndex`, `SkillDetail` (header `EvalLauncher`, Cases tab with
  `PendingCaseList`, History tab), `RunDetail`, `RunsIndex`, `InboxRoute`, `Triage`, `CaseDetail`,
  `ReviewsIndex`, `ReviewDetail`.
- Components: `LaunchButton` (two-click cost confirm, three-state billing, per-launch
  `LaunchModel` picker), `ModelPicker`, `GuidanceEditor`, `primitives.tsx`.

### Dev-loop facts that will bite you if unknown
- `ui/` builds with vite into `src/whetstone/ui/static/` (gitignored). Static assets are served
  per-request (`FileResponse`), so a rebuilt bundle reloads without restarting the server — **but
  Python route changes need a server restart**. A stale server showing new JS is a known trap.
- After changing any response/request model: `npm run gen:api` in `ui/` regenerates
  `ui/src/api/schema.d.ts`; keep `ui/src/api/client.ts` request types in sync by hand.
- Backend tests: `uv run pytest` from repo root; API tests follow `tests/api/test_job_routes.py`
  patterns (httpx test client, monkeypatched LLM). Full suite was 1016 passing at plan time.
- Cost discipline: anything that reaches a model goes through the plan/confirm flow (upper-bound
  estimate, three-state billing, `--yes` for CI). New LLM-calling features must join it, not
  bypass it.
- Secrets: never in `whetstone.toml` — env / gitignored `.env` only.

---

## 2. Invariants — do not break these

1. **Gate comparability.** Base and candidate must see identical cases, identical context,
   identical judge within a run. Anything injected at review or judge time must be a pure function
   of (case, pinned versioned inputs). This is why wiki retrieval is path-based and why sampling
   is a hash, not `random.sample`.
2. **The improvable unit is inspectable text.** Guidance, cases, and (after Phase 1) judge
   behavior are diffable files that gates can approve and retract. Nothing whose behavior can't be
   diffed may become the thing the improve loop edits. (This is the standing argument against
   fine-tuning the *reviewer*; Phase 4.2 explains why a distilled *tier-1 judge* is the narrow
   exception — it is a validated cache, not an improvable unit.)
3. **Attribution before capability.** A behavior may not become variable until runs record which
   variant produced them (`Backend` for models; Phase 0 for the judge). Never create a window of
   unattributed runs.
4. **Nothing silent.** Dropped cases are named, truncation is reported, unknown template variables
   error. Every new mechanism that caps, samples, retires or skips must say so in its output and
   in the UI.
5. **Humans own corpus membership.** Mechanisms *propose* (retire, dedup-skip, synthetic case);
   the triage door or inbox confirmation *disposes*. The corpus is ground truth; ground truth is
   not edited by heuristics.
6. **C6.** A passing gate is retracted by any change to what the reviewer reads (`skill_hash`
   covers guidance + wiki; Phase 4.1 adds the retrieval index). A judge change does **not**
   retract skill gates (both sides shared one judge) but does re-baseline trends — the UI must
   mark that boundary rather than draw a line through it.

---

## 3. The four rot vectors (threat model)

1. **Corpus saturation.** Once every case passes, the gate only guards regressions, improve has no
   failure clusters to learn from, and the skill freezes while the codebase moves. Individual
   cases also rot into tautologies via vague expectations — passing on any output, permanently
   green (`meta_eval/drafting.py` docstring).
2. **Guidance entropy.** Hundreds of improve cycles, each rewriting the body to fix the current
   cluster, produce an append-mostly rulebook: diluted attention, accumulated contradictions.
   Nothing in the gate penalizes size; nothing ever *removes* a rule.
3. **Corpus drift.** Cases reference retired APIs; the defect mix shifts; near-duplicates of the
   skill's favorite defect class pile up and skew the stratified sample toward what it already
   catches. Deterministic sampling gives a five-year-old case the same weight forever.
4. **Measurement decoupling.** Improve trains on the exact corpus the gate tests (train == test),
   so score climbs faster than capability; and the judge — one in five verdicts allowed wrong at
   the 0.8 floor — puts an error bar on every number, with spurious matches specifically hiding
   saturation.

Vector 4 is upstream of detection for 1–3: a noisy instrument makes every hygiene signal
noise-chasing. Hence the phase order.

---

## Phase 0 — Instrumentation (prerequisite)

### 0.1 `judge_hash` on every run

**Why.** The judge is about to become editable (Phase 1). Two runs judged by different judges are
different measurements that look identical; every trend line, saturation statistic and holdout
comparison silently assumes score comparability. `runs.py` already stores `backend`/`model` for
exactly this reason on the reviewer side. Doing this *before* the judge is editable means no
unattributed run ever exists (Invariant 3).

**How.**
- `runs.py`: add `judge_hash TEXT NOT NULL DEFAULT ''` to the schema; bump the schema version and
  extend `_rebuild` / `_upsert`. Old records rebuild with `''` meaning "the pre-Phase-1 hardcoded
  judge" — that is itself an honest attribution.
- Compute the hash where the judge is constructed (`service.py`): for now, hash of the hardcoded
  prompt text; after 1.1, hash of `JUDGE.md` content. Store on `RunSummary`/`RunRecord`.
- Also record `judge_effort` if it isn't already surfaced in the record (it changes verdicts).

**UI.** `RunDetail` shows the judge identity next to the backend line. `RunsIndex` and the
`SkillDetail` History tab group or badge runs by judge version; a trend spark that spans a judge
change renders a visible break, not a continuous line (Invariant 6).

**Done when:** migration round-trips old stores (`_rebuild` covered by a test); new runs carry the
hash; History tab shows the break marker; `npm run gen:api` regenerated.

### 0.2 Dispute-a-verdict → labeled meta-eval pair

**Why.** `meta_eval` loads a static fixture; a static quality bar stops representing the
disagreements the judge actually faces — the judge's own corpus rots exactly like skill corpora.
The moment a human notices a wrong verdict in the drill-down is the only moment the label is free.
This mirrors the existing production loops (`finding confirmed/rejected/missed` in `reviews.py`):
what a person actually ruled is the highest-grade signal the system collects. Without this, Phase
1's improve loop for the judge has no fresh material and the floor is measured against frozen
fixtures forever.

**How.**
- Every ingredient of a `MetaEvalCase` is already in the run record (`ExpectationOutcome` copies
  the expectation; `JudgeVerdictRecord` cites the finding by index).
- New endpoint: `POST /api/runs/{run_id}/disputes` with
  `{expectation_id, finding_index, human_is_match: bool, note}` → appends a labeled pair to a
  meta-eval corpus directory (config: `[meta_eval] dir`, default under the runs root; same
  storage discipline as candidates). `Writable`-gated like other mutating routes.
- Record disputer provenance (timestamp, run id, skill id) — a disputed pair is only meaningful
  with the context it was disputed in.

**UI.** In `RunDetail`, each verdict row (matched/confidence/reason already displayed or
displayable) gets **Agree/Dispute**. Disputing asks one question — "same underlying issue, yes or
no?" — plus an optional note. A disputed verdict shows a badge thereafter. A counter ("N disputed
pairs await the next judge eval") appears on the Health panel (Phase H).

**Done when:** disputing from the UI mints a pair loadable by `load_meta_eval_cases`; duplicate
disputes on the same (run, expectation, finding) update rather than double-mint; API test covers
mint + reload.

---

## Phase 1 — Measurement integrity: the judge

### 1.1 Judge behavior as versioned text (`JUDGE.md`)

**Why.** Whetstone's core bet is that improvable behavior lives in diffable text (Invariant 2).
The judge is currently the one behavioral component that is neither versioned nor improvable
except by code deploy — invisible to run history, ungated by anything. As text: changes go through
review, meta-eval gates them (1.3), 0.1 attributes them, 0.2's disputes feed them. This is also
the load-bearing answer to "why not fine-tune the judge first": a fine-tune can't be diffed,
can't explain a regression, and can't be gated on anything but aggregate accuracy. Text can.

**How.**
- Repo-level `judges/default/JUDGE.md` (frontmatter: id, version; body: system prompt + judging
  doctrine). Loader mirrors the skills loader in miniature; `llm_judge.py` takes the text instead
  of the `_SYSTEM` constant; `service.py` loads it once per run and hashes it (0.1).
- Missing file → exact current hardcoded text as fallback, hash of that text recorded. Zero
  behavior change on day one; the diff that lands this feature must prove that with a
  characterization test (same verdicts before/after on a fixed transcript).
- Changing `JUDGE.md` requires the 1.3 meta-eval check. Wire that as a preflight on runs (warn) и
  a hard gate on "adopt this judge version" (see 1.3), not as a commit hook.

**UI.** A **Judge page** (route `/judge`): current `JUDGE.md` rendered, version + hash, meta-eval
accuracy (overall and per-tier once 1.2 lands), missed vs spurious split, disputed-pairs backlog,
and the accuracy history across judge versions. Edit flow can reuse the `GuidanceEditor`
machinery against a `whetstone/judge/default` staging branch — same pattern as skill guidance, so
the console and CLI never disagree about judge content.

**Done when:** verdicts are byte-identical before/after with the fallback; `JUDGE.md` edits change
the recorded hash; Judge page renders content + accuracy; characterization test in place.

### 1.2 Diff-grounded tier-2 judge with confidence cascade

**Why grounding.** The judge's question is "same underlying issue *at this location*?" and two
sentences frequently underdetermine it — "swallows the error" vs "maps the wrong error type" is
one issue or two depending on the code. The dangerous error is the spurious match (silent case
saturation), and it is exactly the error that code context reduces.

**Why the diff and not the wiki.** Three reasons, all structural: (a) the case's diff is *frozen
inside `EvalCase.change`* — deterministic, versioned, no retrieval machinery, no new storage;
(b) it is a few hundred bytes where wiki caps run to 24KB, and judge calls are the volume cost of
the entire system (cases × trials × both gate sides); (c) **independence** — the reviewer already
reads the live wiki, so a judge reading the same wiki inherits the reviewer's bias: a wrong page
produces a wrong finding *and* a judge inclined to bless it. The instrument must not share the
subject's inputs. A wiki-grounded tier-3 remains a possible *explicitly recorded* escalation if
meta-eval later shows diff-grounding insufficient; it is deliberately not the default.

**Why a cascade instead of grounding everything.** Most verdicts are easy — the short-circuit in
`evaluate_expectation` exists because judging is assumed cheap. Grounding every call multiplies
the largest cost line for context most calls don't need. `JudgeVerdictRecord.confidence` is
recorded today and used by nothing; the cascade is its job: pay for grounding only on contested
calls, which are precisely the ones that saturate cases.

**How.**
- `judge/grounded.py`: same protocol, prompt = expectation + finding + the `FileChange` hunk(s)
  from the case's `change` for the expectation's path (bounded, e.g. 2000 bytes — reuse the
  improve digest's `max_diff_bytes` convention). This requires threading the case (or just the
  relevant hunk) to the judge call: extend the `Judge.match` call path in `core/matching.py` to
  accept optional grounding context; the deterministic judge ignores it.
- `judge/cascade.py`: tier 1 = current pairwise judge; if `confidence < threshold` (default 0.75,
  configurable per skill in `evaluate/step.yaml` under a `judge:` block), tier 2 re-judges
  grounded. Record **both** verdicts with tier attribution (extend `JudgeVerdictRecord` with
  `tier: int = 1`); the tier-2 verdict wins.
- Cost estimate in the plan/confirm flow must mention the cascade: "up to 2 judge calls on
  low-confidence matches." Upper bound stays honest (Invariant 4 / the existing banner style).
- Threshold is measurement config, not reviewer content: it does **not** enter `skill_hash`
  (steps are not hashed — same line the pipeline doc draws), but it *is* recorded on the run.

**UI.** `RunDetail` verdict rows show tier badges ("escalated"); the Judge page shows the
escalation rate (what fraction of verdicts hit tier 2) and per-tier accuracy from meta-eval —
escalation rate is the knob's feedback dial; without it the threshold can't be tuned honestly.

**Done when:** meta-eval on the labeled corpus shows tier-2 ≥ tier-1 accuracy on low-confidence
pairs (that's the hypothesis; if it fails, stop and reassess before shipping); cascade recorded
with tiers; cost banner updated; tests cover threshold boundaries and the "tier-2 overturns
tier-1" path.

### 1.3 Meta-eval as a live, rising bar

**Why.** 0.8 is an adoption floor, not a health target; a fixed floor invites
regression-to-the-floor (a judge edit dropping 0.93 → 0.81 "passes"). And the two error kinds must
be reported separately because they rot differently: *missed* shows up red and wastes an
investigation into a healthy skill; *spurious* shows up green and kills a case silently — the
split is what makes the dangerous one visible.

**How.**
- `whetstone judge eval` CLI + `POST /api/jobs/judge-eval` (plan/launch pair, joins the existing
  job/cost-confirm machinery in `jobs.py`): runs the current judge (cascade included) over the
  meta-eval corpus (fixtures + 0.2 disputes), reports overall / per-tier accuracy and the
  missed/spurious split.
- **Ratchet:** store the best accuracy achieved per judge id (in the meta-eval dir). Adopting a
  new `JUDGE.md` version requires ≥ max(floor, best − small tolerance). One-way; lowering it is a
  deliberate config edit with a reason, never automatic.
- Wire into the inbox: "judge has N undisputed-pending pairs / accuracy unknown for current
  version" ranks alongside skill actions — measurement work should compete for attention in the
  same queue, or it will always lose to skill work.

**Done when:** judge-eval runs from CLI and console with cost confirm; ratchet blocks a
worse-judge adoption in a test; Judge page shows the split and history.

---

## Phase 2 — Corpus hygiene

### 2.1 Deterministic holdout split

**Why.** `skills improve` reads failures from the same corpus the gate then scores: train equals
test, structurally. Over hundreds of cycles the score is guaranteed to climb faster than real
capability — the drafter is shown the answers. Train-vs-holdout divergence is the only cheap,
always-on overfitting alarm available, and nothing currently plays that role. Hash-based
membership (extending `sampling.py`'s existing `sha256(seed:case_id)` discipline) costs no stored
state, is stable forever, and preserves the both-sides-identical property gates depend on.

**How.**
- `sampling.py`: `partition(case_id) -> "train" | "holdout"` via hash bucket; ratio in
  `SamplePolicy` (default `holdout_fraction: 0.2`, settable per skill in `evaluate/step.yaml`).
  20% because: at a few-hundred-case corpus it is enough for divergence to be signal not noise,
  and small enough not to starve the improve digest of failure clusters.
- `improve.py`: the failure digest **excludes holdout cases unconditionally** — filtered at digest
  assembly (the host owns the budget; the host now also owns the blindfold). The digest reports
  "N holdout failures withheld" so nothing is silent (Invariant 4).
- Scoring/gate: report `recall`/`fp_rate` per partition alongside the aggregate; `GateResult`
  gains the per-partition numbers (additive, defaulted — old records must still parse).
- **Targeted-case rule:** `--targeted` must name train-partition cases only; a holdout case named
  is an error naming the reason. Otherwise targeted pressure leaks holdout cases into prompts one
  at a time and the alarm quietly disconnects itself.
- Divergence metric: `train_recall − holdout_recall` (and FP analogue), with a warning threshold
  (default 0.1) surfaced in inbox + Health panel — a *warning*, not a gate: divergence says "stop
  polishing, promote fresh cases," which is advice about the next action, not about this change.

**UI.** `SkillDetail` header scores become "train / holdout" pairs; Health panel plots divergence
over runs; failure drill-down labels holdout cases with a lock glyph and never exposes a "send to
improve" affordance for them.

**Done when:** partition is stable across machines (hash test); digest provably excludes holdout
(test: seeded failures in both partitions, digest contains only train); gate output carries both
partitions; targeted-holdout is rejected with a clear error; UI shows the pair.

### 2.2 Case tiers: active / archive

**Why.** Deterministic sampling gives every case equal draw probability forever, so as the corpus
grows, an ever-larger share of each run's budget re-verifies what the skill has demonstrably
internalized — runs get more expensive *and* the aggregate score gets more flattering (dominated
by solved cases), masking weakness at the live edge. Archive-at-low-weight rather than delete
because retired cases are regression insurance: the monthly distill pass (Phase 5) that drops a
rule must still trip the case that motivated the rule. Humans confirm retirement (Invariant 5).

**How.**
- `EvalCase.tier: Literal["active","archive"] = "active"` (additive, default keeps every existing
  case file valid).
- Retirement *proposal*: a case passed by every skill version across the last N gates (default 10,
  config) is proposed in the inbox — new action kind `curate` with the evidence ("passed 10
  consecutive gates across 3 skill versions"). Confirming flips the tier (a commit on the case
  file — corpus membership changes are diffs, like everything else). Reversible the same way.
- `sampling.py`: stratified allocation gains a tier dimension — archive draws at low weight
  (default 10% of its proportional share, config). Full-corpus runs (`max_cases: null`) still
  score everything; the *monthly distill gate runs with archive included at full weight* — that
  is the moment archives earn their keep.
- Saturation statistics (2.3) and drift (3.1) compute over active only; anything else would count
  deliberately-retired cases as evidence of health.

**UI.** Cases tab gains tier chips + filter; Health panel shows corpus composition: active/archive
counts, kind split (`should_catch` / `should_not_flag`), and the **evidence mix** for negatives
(`confirmed` / `silence` / `unclassified` from `Provenance.evidence` — the codebase already warns
that precision computed from silence rewards a reviewer that says nothing; show the mix so the
operator can see how much of their FP score is built on silence).

**Done when:** tier round-trips case YAML; retirement proposals appear in inbox with evidence and
flip on confirm; sampler honors weights deterministically (test with fixed seed); composition
panel renders.

### 2.3 Saturation probe: zero-guidance baseline

**Why.** A case can stop discriminating two ways the pass-rate can't distinguish: the guidance
genuinely internalized the lesson (good — 2.2 archives it) or the expectation is so vague anything
matches (bad — the case is dead but looks alive). The naked-model baseline separates them: a
`should_catch` case the model passes *with no guidance at all* never measured the guidance. This
generalizes the genuine-vs-decoy probe technique already validated in `meta_eval/drafting.py`
from drafter fixtures to the live corpus.

**How.**
- `whetstone eval baseline` + `POST /api/jobs/baseline` (plan/launch): score active cases with an
  empty skill body through the normal harness (there is already a no-expectations path in
  `service.py` — this is its sibling: no guidance, full expectations).
- Output per case: `tests_guidance: bool` (naked model failed it, current skill passes → the case
  genuinely measures guidance) plus a `barely: bool` for cases whose passing verdict rode a tier-1
  confidence within ε of the cascade threshold — those expectations deserve a human read.
- Store as a run variant (record carries `baseline: true`) so history is queryable; monthly
  cadence, local backend by default (it's a diagnostic sweep, not a gate — the per-launch model
  picker makes this a one-click choice).
- Flagged cases become inbox `curate` proposals: "case X passes with no guidance — tighten its
  expectation or retire it," with a deep link to `CaseDetail`.

**UI.** Health panel: "N of M active cases currently discriminate" with the flagged list;
`CaseDetail` shows the case's last baseline verdict.

**Done when:** baseline run works end-to-end with cost confirm; flags mint inbox proposals; a test
fixture with one tautological expectation is correctly flagged.

### 2.4 Dedup at the promotion door

**Why.** Hundreds of MRs of real defects are heavily repetitive — that is what "defects that keep
shipping" means. Promoted naively, the stratified sample skews toward the over-represented class
and the score increasingly measures "does the skill still catch its favorite thing." Correcting at
the door is strictly cheaper than detecting the imbalance later (2.3 would surface it eventually,
but only after months of budget re-verified duplicates). The door never auto-rejects: the 9th
unwrap case *in a new subsystem* may be exactly the promotion you want (Invariant 5).

**How.**
- At triage load (`candidates.py` / `ui/routers/candidates.py`): compare each candidate against
  the skill's existing cases — same rule provenance, overlapping path pattern, expectation-text
  similarity. Start lexical (token overlap on `semantic`, reusing the clustering spirit of the
  improve digest); embeddings are an optional upgrade in Phase 3, not a dependency here.
- Response model gains `similar_cases: [{case_id, why}]` per candidate.
- Promotion flow offers three dispositions when similars exist: promote active / promote straight
  to archive (counted, cheap) / skip with reason. The chosen disposition lands in provenance
  (`ref` chain preserved).

**UI.** `Triage` route: similarity chip on the candidate card ("similar to 3 existing cases"),
expandable to side-by-side expectation text; the promote button splits into the three
dispositions when similars exist.

**Done when:** a candidate crafted to duplicate a fixture case surfaces its similar; all three
dispositions round-trip; no similarity computation happens at eval time (door-only — keeps the
review path pure).

---

## Phase 3 — Representativeness (offline; no gate-path constraints)

Both steps live entirely outside the review path, so the determinism constraint that bans
embeddings from scoring does not apply here.

### 3.1 Corpus drift metric

**Why.** The one rot vector nothing else can see: holdout catches overfitting *to the corpus*;
saturation catches dead cases *in the corpus*; neither detects that the codebase moved to a new
framework and the entire corpus tests last year's idioms — every internal check reads green while
relevance goes to zero. Provenance dates can't distinguish "old but representative" from "old and
obsolete"; content distance can. This converts "should we invest in this skill's corpus?" from a
feeling into a number the inbox can rank.

**How.**
- Embed (local model via the existing Ollama path; embedding endpoint support may need a small
  addition to the OpenAI-compatible client) the diffs of active cases and the diffs of the
  trailing ~90 days of live MRs from the already-configured corpus source.
- Report per skill: centroid distance + **coverage** (fraction of recent MRs with no case within a
  similarity radius — coverage is the actionable one: it names *which* recent MRs look like
  nothing in the corpus, and those are triage-priority candidates).
- Cache embeddings keyed by content hash (corpus dir, gitignored store); recompute is cheap and
  offline. CLI `whetstone corpus drift` + job endpoint; quarterly by default, on-demand from UI.
- Threshold crossing mints an inbox action: "corpus drift: 40% of recent MRs have no nearby case —
  review the uncovered list."

**UI.** Health panel: drift score with trend and the uncovered-MRs list, each linking into triage.

**Done when:** drift runs offline against fixtures with a fake embedder (tests must not need
Ollama); uncovered MRs link to triage; inbox action appears past threshold.

### 3.2 Counterfactual negatives and mutation probes

**Why (counterfactuals).** A corpus mined from defects is structurally positive-heavy, and
`sampling.py`'s own docstring names the consequence: an FP rate over zero negative cases is "a
flattering zero." Stratification allocates proportionally but cannot conjure negatives that don't
exist. Applying a case's stored fix yields the highest-grade negative obtainable — the exact code,
defect removed — nearly free because the fix (`change.diff`) is already stored with the case.

**Why (mutation probes).** The holdout detects overfitting to *unseen* cases but says nothing
about instance-memorization on cases the drafter *was shown*: a rule that names variables from one
incident passes that incident forever while missing every recurrence. Mutating a passing case
(rename identifiers, relocate code, restructure — defect preserved) is the only test for
pattern-vs-instance. If the skill misses the mutant, the guidance memorized the instance.

**How.**
- Two generators feeding **triage, never auto-promotion** (Invariant 5), with
  `Provenance.source = "synthetic-counterfactual" | "synthetic-mutation"` and `ref` pointing at
  the parent case — synthetic cases must be excludable from any "what really ships" analysis.
- Counterfactual: mechanical where the stored fix applies cleanly; skip and report where it
  doesn't. Mutation: LLM-drafted (joins job/cost machinery), validated by re-running the *parent's*
  expectation against the mutant's diff region mapping before it may enter triage.
- CLI `whetstone corpus synthesize --skill … [--counterfactual|--mutate]` + job endpoints.

**UI.** Triage cards badge synthetic provenance with a link to the parent case; Health panel
counts synthetic vs mined in the composition block.

**Done when:** counterfactual from a fixture case round-trips through triage → promote → scoring;
mutation flow marks provenance; a synthetic case can be filtered out of every corpus statistic.

---

## Phase 4 — Capability: change the slope

### 4.1 Case-corpus RAG at review time

**Why.** Today all corpus knowledge must pass through improve cycles into guidance prose — a lossy
distillation with one-full-loop latency, and the direct cause of guidance bloat (every incident
fights for a sentence in `SKILL.md`). Retrieval inverts it: a case promoted this morning sharpens
this afternoon's reviews with zero improve cycles, and guidance can shrink to durable principles
because the corpus itself carries the incidents. This is the single change that turns corpus
growth from a cost (bigger eval runs) into an asset (richer precedent) — it reframes the rot
economics rather than patching them.

**Why it's admissible despite the "no embeddings in retrieval" stance.** The wiki doc's objection
is precise: retrieval must be a pure function of the diff so both gate sides see identical
context. A *pinned* embedding model over a *versioned, committed* index satisfies exactly that
property. The principle survives; only the retrieval key changes. Folding the index hash into
`skill_hash` makes an index rebuild retract gates exactly as a wiki refresh already does (C6).

**Why sequenced after Phases 1–2.** RAG feeds corpus content into live reviews. Injecting an
unvetted, duplicate-heavy corpus amplifies its flaws at review time, and without the trustworthy
instrument you cannot believe the gate that tells you whether retrieval helped.

**How.**
- `skills/<id>/index/` (or staged on the skill branch like the wiki — prefer the branch, same
  rationale: console and CLI must agree): embedding vectors per case keyed by case content hash,
  plus a manifest naming the embedding model+version. Manifest hash → `skill_hash`.
- At review time: embed the incoming change with the pinned model, retrieve k nearest active cases
  (both kinds — FP precedents teach restraint, which prose guidance is notoriously bad at
  encoding), inject **after** guidance, labelled as precedent-not-rules — the exact framing
  discipline `wiki.py` already applies to background context.
- Caps like wiki caps (pages→cases, bytes), preflight-reported when exceeded (Invariant 4).
- A skill without an index behaves exactly as before (the no-wiki precedent: absent folder, hash
  unchanged, nothing retracted).
- Gate it like any content change: base without / candidate with, same cases, and let the gate
  say whether precedent injection helped *this* skill.

**UI.** `SkillDetail` gains an index card (model, version, case count, staleness vs corpus — cases
added since last index build); rebuild is a job with the usual confirm; `ReviewDetail` shows which
precedent cases were injected for a given review (findings become explainable: "flagged like
case-X was").

**Done when:** deterministic retrieval test (same diff + same index → same cases, twice); index
rebuild retracts gate evidence (C6 test); skill-without-index hashes unchanged
(characterization); review detail lists injected precedents.

### 4.2 Judge distillation — last, deliberately

**Why last.** Distillation is a cost optimization; optimizing cost before accuracy bakes the
teacher's errors into cheap, fast, permanent form. After Phase 1 the teacher is trustworthy and
the training set is free exhaust (0.1 attributes every verdict to its judge version). **Why at
all:** judge calls scale as cases × trials × both gate sides — at thousands of cases they dominate
run cost, and a near-free tier 1 makes the *unsampled* full-corpus run affordable weekly instead
of quarterly; frequent full measurement feeds every detector in Phases 2–3 fresher data. **Why
Invariant 2 permits it here:** the reviewer must stay inspectable because it is the thing being
improved; the tier-1 judge is a *validated cache* of the grounded judge's behavior — permanently
re-validated by the 1.3 bar, permanently backed by tier-2 escalation. If it drifts, the bar
catches it and the cascade absorbs it. It is never the improvable unit.

**How.** Export (finding, expectation, diff-hunk → verdict) triples filtered to the current judge
version; fine-tune a small local model (outside Whetstone; document the recipe in
`judges/default/distill.md`); validate via `judge eval` against the full labeled corpus + ratchet;
deploy as cascade tier 1 via config (`judge: {tier1: {llm: ollama, model: …}}` in the evaluate
step), grounded judge stays tier 2. Rollback is config.

**Done when:** distilled tier 1 meets the ratcheted bar on meta-eval; full-corpus run cost drops
measurably (record before/after in this file); escalation rate visible on Judge page.

---

## Phase 5 — Operating cadence (near-zero code; the routine that removes what the machinery detects)

- **Weekly — work the inbox in its own order** (propose > gate > triage > score > improve): the
  ranking already encodes "finishing something in flight beats starting something new." Local
  backends for routine scoring; the strong model for gates — the per-launch picker exists for
  exactly this split.
- **Monthly — guidance distill pass:** `skills improve --instruction "consolidate; merge
  redundant rules, remove subsumed ones, shorten"`, no targeted cases, gated over the **full
  corpus including archive at full weight**. *Why scheduled:* improve cycles add rules weekly and
  nothing else ever removes one; entropy management must be on a clock because no failure ever
  demands it. *Why archives count here:* they are the tripwires against over-aggressive deletion.
- **Monthly — dead-rule report:** from `meta.yaml` rule→signal provenance, list rules whose
  supporting cases are all archived or whose provenance refs touch since-deleted paths. *Why:* it
  turns distillation from "the model shortened some prose" into an evidenced removal list — the
  difference between compression and vandalism. (Small build: a report command + Health panel
  list; fold into the distill month.)
- **Monthly — saturation probe (2.3) + judge missed/spurious review (1.3).**
- **Quarterly — re-anchor:** one unsampled full-corpus run per skill (sampled scores are
  estimates; estimates need ground-truthing), wiki regeneration (retraction guard already makes
  it safe), drift review (3.1).
- **On default-model change — re-baseline everything:** trend lines are per-(backend, judge);
  comparing across either is the mistake the `Backend` record exists to prevent.
- **The KPI that overrides all internal numbers:** production catch rate and dismissal rate from
  the live loops (`reviews.py` signals, escaped defects). Every internal number is a proxy. When
  eval says 98% and shipped misses keep arriving, the verdict is "corpus rotten," not "skill
  sharp" — and Phases 2–3 name *which kind* of rotten.

**Cadence must be visible, or it will not happen.** The inbox gains time-based `curate` actions
("distill pass due — last run 47 days ago", "quarterly re-anchor overdue"), driven by stored
last-done timestamps. A cadence that lives in a document is a cadence that dies in the document.

---

## Phase H — the Health surface (cross-cutting; build incrementally alongside Phases 0–3)

The operator's requirement, verbatim: *"the UI reflects the state of affairs — I need to see the
state of the skills."* Each phase above names its own UI deliverable; this section is the
integrating surface so the state of a skill is one look, not a scavenger hunt.

**`GET /api/skills/{id}/health`** aggregates (all fields optional/null until their phase lands —
the endpoint ships early and fills in, so the UI never blocks on a phase):

```
score_now            {train, holdout, aggregate}     per latest run, per partition   (2.1)
divergence           float + trend                   train − holdout                 (2.1)
composition          {active, archive, kinds, evidence_mix, synthetic}              (2.2, 3.2)
discrimination       {testing_guidance, flagged: [case_id]}                          (2.3)
drift                {score, coverage, uncovered: [mr_ref]}                          (3.1)
judge                {id, version, accuracy, split: {missed, spurious},
                      escalation_rate, disputes_pending}                             (0.2, 1.x)
index                {model, built_at, stale_cases} | null                           (4.1)
cadence              {last_distill, last_anchor, last_baseline, due: [...]}          (5)
production           {confirmed, rejected, missed, escaped} trailing window          (reviews.py)
```

**`SkillsIndex`** gets a compact health strip per skill — train/holdout score pair, a
divergence/saturation/drift traffic-light, and days-since-anchor — so "which skill needs me"
is answerable from the index without opening each one. Sort by worst health, because the index's
job is triage of attention.

**`SkillDetail`** gets a **Health tab** rendering the full payload: score trend with judge-version
break markers (0.1), divergence plot (2.1), composition + evidence-mix bars (2.2), the
discrimination list with case links (2.3), drift + uncovered MRs linking into triage (3.1),
cadence clocks with one-click launch for what's due (5), and the production KPI strip —
deliberately adjacent to the eval scores so the proxy and the ground truth sit in one eyeline.

**`/judge`** page per 1.1–1.3. **Inbox** carries the new `curate` action kind (retirement,
saturation, drift, cadence-due) ranked with everything else — the inbox stays the single "what's
next" surface; Health explains, Inbox directs.

**Done when:** a skill in a deliberately degraded fixture state (saturated case, diverging
holdout, overdue distill) shows all three signals on the index strip, the Health tab explains
each with a link to the fixing action, and the inbox ranks the fixes.

---

## Sequencing and dependency graph

```
0.1 judge_hash ──► 1.1 JUDGE.md ──► 1.2 cascade ──► 1.3 rising bar ──► 4.2 distillation
0.2 disputes  ──► 1.3
1.x (trustworthy instrument) ──► 2.1 holdout, 2.2 tiers, 2.3 saturation   (readable at full signal)
2.x (vetted corpus) ──► 4.1 case-RAG            (never amplify an unvetted corpus)
3.1 drift, 3.2 synthetic — independent after Phase 1; any time
Phase 5 — starts immediately (no code); cadence actions land with 2.2's inbox kind
Phase H — endpoint skeleton lands with 0.1; panels fill in per phase
```

Hard rules restated: 0 before 1 (no unattributed runs, ever); 1 before trusting any Phase-2
signal; 2 before 4.1; 4.2 strictly after 1.3; holdout cases never reach an improve digest or a
`--targeted` flag.

Rough effort: Phase 0 ~1 day · Phase 1 ~1 week · Phase 2 ~1–2 weeks · Phase 3 ~1 week ·
Phase 4.1 ~2–3 weeks · 4.2 when volume justifies · H incremental throughout.

---

## Working agreements for the implementer

- Ship in the order above; within a phase, land each numbered step as its own PR with its UI
  surface and tests in the same PR (the no-dark-mechanisms rule).
- Every new LLM-touching path joins the plan/confirm cost flow and the three-state billing model;
  every new mutating endpoint is `Writable`-gated.
- Every schema/response change: `npm run gen:api`, update `client.ts` request types, and remember
  Python route changes need a server restart even though static assets hot-reload.
- Characterization tests before behavior-preserving refactors (1.1 especially: byte-identical
  verdicts with the fallback prompt).
- Additive data-model changes only (defaults that keep old files/records valid); `runs.py` schema
  changes go through its version + `_rebuild` machinery.
- When a step's hypothesis fails (e.g. 1.2's "grounding beats tier-1 on contested pairs"), stop
  and update this document with the measurement before writing more code. The plan is a set of
  bets with stated reasoning; a falsified bet is a finding, not an obstacle.
