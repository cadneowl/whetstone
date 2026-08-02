# ANTI_ROT_PLAN — keeping skills sharp across hundreds of MRs and defects

> **Historical planning document.** The plan the anti-rot loop was built from, kept for its design
> reasoning and threat model. The status log below stopped being updated on 2026-07-28 and does not
> describe the current tree. For what ships, read the
> [anti-rot section of the README](README.md#keeping-skills-sharp-the-anti-rot-loop); for decisions
> still in force, read [docs/decisions.md](docs/decisions.md).

> **Implementation status (updated 2026-07-28, branch `anti-rot/phase-0`)**
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
> - **2.1 — DONE** (this commit): the holdout split, ON by default at 0.2.
>   `sampling.partition_of` (unseeded hash — no re-roll knob, deliberately) +
>   `holdout_report(score, fraction)` (None when nothing to compare, never zeros);
>   `SamplePolicy.holdout_fraction`; `CaseRun.partition` stamped at record time (old records
>   load as train, honestly); `RunRecord.holdout` + `GateRecord.base_holdout/candidate_holdout`
>   (`HoldoutReport` in domain/score.py with computed `divergence`). The blindfold lives in
>   `improve._failures` (digest reports `holdout_withheld`; render says "improve from the
>   pattern, not the exam"); `propose` drops model-named holdout ids like unknown ids
>   (`ProposalResult.holdout_cases`); `gate_skills` refuses `--targeted` holdout ids before any
>   spend, and threads the fraction to both sides via a no-draw SamplePolicy. CLI prints the
>   partition lines (`format_holdout`); drill-down shows train/holdout/divergence metrics, a
>   red note past 0.1 divergence, and a holdout badge per case. Aggregate scores unchanged —
>   every case is still scored; only learnability changed.
> - **2.2 — DONE** (this commit): case tiers. `EvalCase.tier` (`active`/`archive`, absent means
>   active so every existing case file keeps its meaning); loader reads it;
>   `SamplePolicy.archive_weight` (default 0.1) and `sampling._stratified` now stratifies over
>   `(kind, tier)` with archive strata weighted — full-corpus runs (`max_cases: null`) still
>   score everything at full weight (the monthly-distill posture). `curation.py`:
>   `retirement_proposals(skill, gates)` — an active case that passed its last
>   `RETIREMENT_GATES` (10) non-practice gate *appearances* (sampled-out gates are skipped, not
>   counted against) on the candidate side is proposed with evidence ("passed the last 10 gates
>   it appeared in, across N skill versions"); one recent failure kills it. `retier_yaml` edits
>   exactly one top-level line of `case.yaml` and validates the result — never a YAML round-trip
>   (hand-written files keep their bytes). The flip endpoint
>   `POST /api/skills/{id}/cases/{case_id}/tier` stages a commit on `whetstone/skill/<id>`
>   (never the working tree; reads the branch's copy first so flips compound) — and because a
>   rewritten case changes `skill_hash`, C6 demands a fresh gate before the archived corpus
>   ships. Inbox: new `curate` action kind (rank 5, below improve), `Attention.retirements`
>   with evidence, computed against the staged skill so a confirmed flip stops nagging
>   immediately. UI: Cases tab archive badge + show/hide filter, inbox rows with per-case
>   Archive buttons, SkillsIndex archive count.
> - **Phase H — endpoint + Health tab LANDED** (this commit, fills per phase):
>   `GET /api/skills/{id}/health` (`ui/routers/health.py`) aggregates `score` (latest run +
>   holdout pair), `composition` (tiers, kinds, evidence mix), `retirements`, `production`
>   (confirmed/rejected/pending over the last 20 reviews), and `judge` (reuses `get_judge`;
>   malformed JUDGE.md surfaces as `judge_error`, never a healthy blank). Sections whose phases
>   have not landed — `discrimination` (2.3), `drift` (3.1), `index` (4.1), `cadence` (5) — are
>   present and null, so the payload's shape is the plan's and the UI never restructures.
>   UI: SkillDetail **Health** tab (`components/HealthPanel.tsx`) with a "Not yet measured"
>   section naming the missing panels; SkillsIndex row shows the holdout score + a "diverging"
>   badge past 0.1. Fill in `discrimination` when 2.3 lands and `drift` when 3.1 does.
> - **2.3 — DONE** (this commit): the saturation probe. `service.strip_guidance` (body, pages,
>   wiki all removed; archived cases dropped — the probe informs curation of the *live* corpus)
>   + `record_baseline` (always the full active corpus, never a sample; no holdout partition —
>   nothing here is learnable-from; `RunRecord.baseline=True`). Store: schema v4, `baseline`
>   column on both tables; `RunStore.list(baseline=False|True|None)` — **default excludes
>   probes**, so no trend, inbox row, staleness check, or improve digest can mistake a
>   deliberately-blinded run for a catastrophic regression; `latest_baseline()`;
>   `case_history` excludes probes. `curation.discrimination(skill, probe)`: flagged = active
>   `should_catch` case the naked model caught in *every* trial (a sometimes-pass still
>   discriminates); noflag out of scope (a naked model staying quiet is the expected state);
>   cases promoted since the probe are unmeasured, not guessed at; `testing_guidance` =
>   active_catch − flagged, computed on read so archiving a case changes the answer
>   immediately. Surfaces: `whetstone eval baseline` (cost confirm, prints flags, `--json`),
>   `POST /api/jobs/baseline[/plan]` (JobKind "baseline"), health `discrimination` section
>   (filled, no longer null), `CaseDetail.baseline` verdict, inbox `saturated` proposals
>   (join retirements under the `curate` action — label now "Curate N cases" naming both
>   reasons). UI: HealthPanel Discrimination section with per-case Archive buttons + probe
>   LaunchButton, case page "passes with no guidance" badge, LaunchButton baseline result.
> - **2.3 deferral** (deliberate): the plan's `barely` flag (passing verdicts within ε of the
>   cascade threshold) — only meaningful with the cascade on, which is off by default, and it
>   needs the judge policy threaded into the discrimination read. Add when a deployment
>   actually enables `escalate_below`.
> - **2.4 — DONE** (this commit): dedup at the promotion door. `curation.similar_cases(candidate,
>   skill)`: lexical only, three signals with the *why* as a sentence — same provenance ref
>   ("mined from the same merge request"), expectation token-overlap ≥ 0.5 (Jaccard over
>   lowercased words minus a tiny closed stopword set), or same file + overlap ≥ 0.25. Same-kind
>   cases only; capped at 5, best first; `SimilarCase.semantic` carries the existing expectation
>   so the triage screen lays the two side by side. Wired at triage load only (`_CorpusCache` in
>   `ui/routers/candidates.py` — per-skill working tree **merged with the triage batch**, because
>   the commonest duplicate is the candidate promoted an hour ago; best-effort, a malformed skill
>   never 500s the queue). Never near the review path. Dispositions: `CaseEdits.tier` — promote
>   active (default, file unchanged from pre-tier days), **promote to archive** (case.yaml gains
>   `tier: archive`, provenance/ref chain intact, low draw weight from day one), or reject with
>   reason (already existed). UI: warn-toned expandable chip on the triage form ("similar to N
>   existing cases") with side-by-side expectations and case links; "Promote to archive" button
>   appears only when similars exist.
> - **3.1 — DONE** (this commit): the corpus drift metric. `llm/embedding.py`:
>   `OpenAIEmbeddingClient` (`/v1/embeddings` over the existing httpx, retries, order restored by
>   index) + `CachedEmbedder` (one JSON file per sha256(text) per model slug, torn file = miss)
>   + `build_embedder(provider, model=…)` through `resolve_backend` with `inherit_env=False` —
>   the chat env's `WHETSTONE_LLM_MODEL` must never leak into an embeddings call; anthropic kind
>   refused with the fix in the message. `drift.py`: the recent MR stream is the **candidate
>   queue** (decided + pending — `corpus pull`/watcher already materialize the trailing window,
>   so a probe is fully offline), grouped one-unit-per-MR by provenance ref so a chatty MR cannot
>   outvote ten quiet ones; corpus side = active cases with diffs (archive is regression
>   insurance, not representativeness). Coverage = fraction of MRs with an active case at cosine
>   ≥ `COVERAGE_RADIUS` (0.6); uncovered sorted farthest-first, capped at `MAX_UNCOVERED` (50)
>   with `uncovered_total` kept honest; centroid distance = 1 − cos(centroids); `DRIFT_ALARM` =
>   0.4 uncovered. `DriftStore` (JSON per report, like reviews), `[drift]` config block
>   (`dir`, `embed_provider`/`embed_model` — deliberately separate from `[llm]`: chat models do
>   not embed), cache under `<dir>/cache`. Surfaces: `whetstone corpus drift` (preflight,
>   uncovered lines, `--json`), `POST /api/jobs/drift[/plan]` (JobKind "drift"; plan 422s on a
>   missing model, an empty side, or a chat-only provider — before the click), health `drift`
>   section (report + trend history + `alarm` computed server-side so panel and inbox cross the
>   same threshold), inbox: `Attention.drift_uncovered` + new `drift` action ("Review uncovered
>   MRs", rank 5 — below improve, above curate; below triage/score, so fresh signal and a first
>   run still win). UI: HealthPanel Drift section (coverage, centroid distance, trend arrow-line,
>   uncovered rows linking `/triage?focus=<candidate>` — Triage now seeds its index from `?focus`
>   and consumes it), LaunchButton drift result, inbox drift badge + Health link. Tests:
>   `test_drift.py`/`test_embedding.py` (keyword-axis fake embedder — similarity arranged by
>   choosing words; MockTransport for the client; no Ollama anywhere), `test_drift_routes.py`.
> - **3.1 note:** the drift *action* only surfaces when nothing more urgent exists — with
>   unruled signals the inbox says triage (reviewing them is how uncovered MRs get promoted).
>   The reading itself (`drift_uncovered`) is always on the row.
> - **3.2 — DONE** (this commit): synthetic counterfactuals and mutation probes.
>   `corpus/synthesize.py`, both generators feeding **triage, never auto-promotion**.
>   Counterfactual = the parent `should_catch` case's diff reversed (`CodeChange.reversed()` —
>   for an escaped-defect case this reconstructs the original fix exactly), as a
>   `should_not_flag` asserting silence in the parent's own words; mechanical, no model.
>   Mutation = LLM-drafted (`MutantDraft {diff, note}` through the normal `structured()` path),
>   validated before it may enter the queue: must parse as a unified diff, must add lines for
>   the parent's expectation to anchor to (region remapped to the mutant's added span, semantic
>   carried verbatim — the probe's claim is that the same words still apply), and must differ
>   from the parent's added content (an echo is a skip, not a candidate). Eligibility shared
>   (`eligible_parents`): active `should_catch` with diff + expectation text; synthetic parents
>   refused — the chain stays one step from real evidence; every refusal is a reported
>   `Skipped(case_id, reason)`. Deterministic child ids (`syn-cf-*`/`syn-mut-*`) so re-runs hit
>   `store_candidates`' existing-dir skip instead of duplicating. Confidences 0.7/0.6 — below
>   every human-confirmed signal, so synthetics never outrank real evidence in the queue.
>   Vocabulary: `SOURCE_COUNTERFACTUAL`/`SOURCE_MUTATION` + `SYNTHETIC_PREFIX` contract,
>   `Provenance.synthetic` property, `SIGNAL_COUNTERFACTUAL`/`SIGNAL_MUTATION` in the closed
>   human_signal set (so every badge/filter surface treats them as what they are), and
>   `EVIDENCE_SYNTHETIC` — its own precision bucket, never `confirmed` (generated evidence must
>   not launder into the strongest tier). Filtered out of "what really ships": drift's stream
>   excludes synthetic candidates (a padded stream measures the corpus against its own
>   reflection); health `Composition.synthetic` counts them; evidence mix shows the bucket.
>   Surfaces: `whetstone corpus synthesize --skill … [--counterfactual] [--mutate] [--case …]`
>   (preflight for the mutation calls only), `POST /api/jobs/synthesize[/plan]` (JobKind
>   "synthesize"; counterfactual plan is billing=local calls=0). UI: signals.tsx entries,
>   DiscussionPane synthetic explanation + parent-case link (both in the silence pane and as the
>   header ref link), Corpus health section: synthetic count, evidence bucket, and both
>   generators as LaunchButtons; LaunchButton synthesize result names written and skipped.
>   Tests: `test_synthesize.py`, `test_synthesize_routes.py` — including the done-when
>   round-trip: counterfactual → triage → promote, provenance intact on the batch branch.
> - **4.1 — DONE** (this commit): case-corpus RAG at review time. `caseindex.py` (domain-free,
>   like `wiki.py`): `SkillIndex {model, provider, built_at, cases: id→content_hash, vectors:
>   hash→floats}` committed as `skills/<id>/index/{manifest.yaml,vectors.json}`;
>   `index_digest` over model+provider+case-hash map — **not** `built_at` (a no-change rebuild
>   must not retract gates; the `wiki_digest`-ignores-`source` precedent) and not the vectors
>   (pure function of model+content). Digest folds into BOTH `skill_hash` and `guidance_hash`
>   via `_feed_index` — empty index = byte-identical hashes, pinned by a characterization test
>   against digests captured pre-4.1 (`test_caseindex.PINNED_*`; failing it means every stored
>   gate stops covering its content). Loader loads `index/` (broken → SkillLoadError, the wiki
>   rule); `index/` joined `_NOT_GUIDANCE`. Retrieval: reviewer embeds the incoming diff with
>   the manifest's pinned model (service builds the embedder from the manifest — the caller
>   gets no knob, that's the pin), memoized per diff so k trials cost one embedding;
>   `retrieve_precedents` is a pure function of (vector, index, corpus), cosine-ranked, tie-broken
>   by case id, capped by `PrecedentLimits {max_cases: 3, max_bytes: 8000}` (configurable via
>   `inputs.precedents` in evaluate/step.yaml; drops are named). **A case is never its own
>   precedent** (`query_hash` exclusion): at eval time the query diff IS the case diff, and
>   without this every indexed case scores with its own answer key in the prompt. Injected after
>   guidance+wiki, framed "precedents … NOT rules"; both kinds (a stay-silent precedent teaches
>   restraint). `strip_guidance` strips the index too — the naked probe must not be credited
>   with the corpus's lessons. Gate sides each keep their own index (base-without vs
>   candidate-with is exactly the "did retrieval help?" gate). `ReviewRecord.precedents`
>   (`PrecedentRef {case_id, kind, similarity}`) from `reviewer.last_precedents`. Preflight
>   names the per-case embedding cost when an index is present. Surfaces: `whetstone skills
>   index` (stage-by-default like `skills update`, `--working-tree` opt-out, preflight),
>   `POST /api/jobs/index[/plan]` (JobKind "index", stages on `whetstone/skill/<id>` — C6 test:
>   inbox flips to "run the gate", can_propose false); `_embedding_backend` shared with drift.
>   Health `index` section filled (model, provider, built_at, cases, `stale` = active cases the
>   index does not cover, read from the staged skill); HealthPanel Case-index card with
>   build/rebuild LaunchButton and stale-case links; ReviewDetail "reviewed with precedent"
>   chips linking case pages. Tests: `test_caseindex.py` (deterministic retrieval twice,
>   self-exclusion, caps, digest properties, round-trip, characterization),
>   `test_index_routes.py` (staged rebuild + C6 retraction, staleness, eval-plan detail, review
>   precedents recorded end-to-end).
> - **4.2 — DONE** (this commit): the judge-distillation machinery. `meta_eval/distill.py`:
>   `export_triples(records, skills, judge_hash=…)` walks the run store and emits one
>   `DistillTriple` per recorded verdict — finding message/location, expectation
>   semantic/must/region, the case's grounding hunk joined from the skill on disk (capped at the
>   cascade's own 2000 bytes; "" when the case is gone — the pairwise fields stand alone),
>   verdict matched/confidence/reason/tier, and `prior` on escalations (the tier-1 call the
>   grounded teacher corrected — the hard negatives worth oversampling). **Filtered to one judge
>   identity** (mixing judges distills an instrument nobody ran); default = the newest real
>   run's hash (`newest_judge_hash` — "current" is not computable from the doctrine file because
>   judge_hash folds per-skill cascade policy). Practice runs excluded (regex verdicts), baseline
>   probes included (same instrument, naked reviewer). CLI `whetstone judge export` (JSONL,
>   `--judge-hash` accepts a unique prefix, excluded runs reported by reason). Deployment seam:
>   `JudgePolicy.tier1 {llm, model, base_url}` — record_eval builds a *separate* counted client
>   for tier-1 verdicts (reviewer + grounded tier 2 stay on the run's client; `llm_calls` sums
>   both counters), and the resolved tier-1 model folds into `judge_identity(tier1_model=…)` —
>   a swapped tier-1 is a different instrument, trends break at the seam; empty hashes exactly
>   as before. Validation/deployment reuse existing machinery: `judge eval --llm ollama --model
>   <distilled>` vs the ratcheted bar, config-only rollback. Recipe: `judges/default/distill.md`
>   (chat-example shaping, train-to-teacher-final-verdicts, hold out by case id not row,
>   escalation-rate operating band). Surfaces: Judge page `escalation` stats (tier-2 share over
>   the last 20 runs, computed on read from run records — probes in, practice out), eval-plan
>   detail when tier1 is configured, scaffold template documents the block.
> - **4.2 deferral** (operational, needs real hardware): the done-when's measured before/after
>   full-corpus run cost — record it here after the first real distilled model deploys. The
>   export/validate/deploy path is fully built and tested; the fine-tune itself is by design
>   outside Whetstone.
> - **5 — DONE** (this commit): the operating cadence, visible. `cadence.py` — four per-skill
>   clocks (`PERIOD_DAYS`: distill 30d, saturation 30d, anchor 90d, drift 90d). Three are
>   *derived* on read from stores that already record their events (saturation from
>   `latest_baseline`, drift from the drift store, anchor from the newest real run whose draw
>   covered every currently-active case — judged against the corpus as it is *now*, so a
>   promotion restarts the clock; `last_anchor_at` scans the last `ANCHOR_SCAN=10` records).
>   Only the distill pass is stored (`CadenceStore`, `[cadence] dir` config, default
>   `.whetstone/cadence`): a distill is an ordinary improve run with a consolidating
>   instruction and nothing in its record distinguishes it, so the operator marks it —
>   `whetstone cadence done`, or the Health tab button (`POST
>   /api/skills/{id}/cadence/distill`, Writable-gated; the derived clocks have no endpoint on
>   purpose). Never-done clocks count from the skill's first real run
>   (`RunStore.earliest_at`, baseline/practice excluded) — a day-one skill owes nothing, and a
>   skill with no runs owes nothing (score already outranks). Health payload's last
>   placeholder filled: `cadence: CadenceSection` (always present, `due` sentences computed).
>   Inbox: `cadence` ActionKind at rank 7 — below curate (evidence outranks a calendar),
>   above nothing; `Attention.cadence_due` carried even when outranked. **Dead-rule report**:
>   `deadrules.py` crosses `meta.yaml` rule→signal provenance with the corpus — verdicts
>   `unreferenced` (guidance no longer mentions the rule id; custom word-boundary so R1 ≠
>   R12), `evidence-archived` (all supporting cases archived; case ids carried),
>   `no-evidence` (refs match no case — matched on MR identity, `#note_…` stripped). Pure
>   function of the skill; health computes it from the *staged* skill so a distill on the
>   branch stops the nagging immediately. CLI `whetstone skills rules`, `whetstone cadence
>   status`. UI: Health tab Cadence section (clocks, due badges, mark-done) + Dead rules
>   section; the "Not yet measured" placeholder list is gone — every planned section now
>   renders.
> - **5 deferral** (needs the target repo, which Whetstone deliberately never has): the
>   dead-rule verdict for "provenance refs touch since-deleted paths" — recorded here rather
>   than approximated. Also still open from §5: the weekly/monthly/quarterly *practice* itself
>   is a human routine; the clocks only make skipping it visible.
> - 2.2 deferrals: `RETIREMENT_GATES` is a module constant, not yet config; the monthly
>   archive-at-full-weight distill gate is a documented posture (`max_cases: null`), not a
>   scheduled job — its clock now ticks on the Health tab.
> - **All phases 0–5 are now implemented.** Remaining open items are the recorded deferrals
>   above (2.3 `barely` flag, RETIREMENT_GATES config, dispute diff capture, console JUDGE.md
>   editing, hard adopt-gate, 4.2 measured cost after a real distilled model deploys, the
>   deleted-paths dead-rule leg) and Phase H's degraded-fixture "done when" walkthrough.

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

**Cadence must be visible, or it will not happen.** The inbox gains time-based actions (its own
`cadence` kind, ranked below evidence-backed work: "guidance distill pass due — last done 47 days
ago", "full-corpus anchor run due — never done"), driven by last-done timestamps — stored for the
distill pass, derived from the run and drift stores for the rest. A cadence that lives in a
document is a cadence that dies in the document.

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
