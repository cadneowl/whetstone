# Agentic reviewers with source access + per-skill context

**Status:** **Phase 1 implemented** (see §12). A skill's `evaluate` step may now name its own
reviewer program with `run:`, declare an open-ended `context:` bag, and Whetstone resolves it,
validates required vars at the plan, and forwards it to the program on stdin — wired into both the
console and the CLI, on **every path that scores**: `eval`, `gate`, the baseline probe and live
review. Runs, gates and reviews record which reviewer produced them and what it was given.

**Phase 2 is largely obviated, not pending.** It was specified as folding the hashable context slice
into `skill_hash`, and it stalled on a real snag (§14): `skill_hash(skill)` is a pure function of the
`Skill`, but context lives in a *step*. That fold turned out to be unnecessary. The property it
existed to provide — *a score measured under a different context bag is a different measurement* —
is enforced instead by `BaselineKey` (`gates.py:160`), which carries `reviewer` +
`reviewer_context_digest` + `inputs_digest` and therefore refuses to reuse a baseline across a
changed bag. That is the sounder place for it: `skill_hash` could only ever assert *the same
snapshot was available*, never *the same content was read*, and for an agent reviewer what gets read
is chosen at runtime by the model. Residual agent nondeterminism is surfaced by `reviewer_trace` and
`k>1` variance rather than papered over by a hash. **What genuinely remains** is smaller and is
tracked in §6.1: a stored gate result is not retracted when the context bag changes under unmoved
guidance. The design below is the full picture; the "Implementation notes" at the end record what
shipped and what did not.

**The ask.** A code-review skill over a 400k-file repo cannot pre-bake the repo into a wiki and
cannot fit it in context. The reviewer needs to **reach the actual source and query it while
reviewing a diff** — and different skills need different inputs to do that: one needs only a source
location, another needs that plus a DB schema, an API spec, a service registry, ten more things. So
we need (a) a way for a reviewer to have folder access, and (b) an **open-ended, per-skill variable
mechanism** the host resolves and forwards without having to understand the keys.

This doc traces how it works against the real code, answers the two questions directly — *which* LLM
gets folder access, and how the extra variables are requested — and covers determinism, security,
cost, records, failure modes, the exact files touched, and a phased rollout.

---

## 1. Goals / non-goals

**Goals**
- A reviewer can read the live source tree (and anything else it's told about) while reviewing.
- Per-skill, arbitrary set of declared inputs; the host resolves + validates + forwards them.
- Gating stays meaningful (a base-vs-candidate score delta is still attributable to the guidance).
- Zero behaviour change for every existing skill that doesn't opt in.

**Non-goals**
- Giving *whetstone's own* reviewer model filesystem tools. Whetstone stays the orchestrator; the
  agent is the operator's.
- Semantic/embedding wiki retrieval (deliberately excluded elsewhere for gate determinism).
- A general plugin system. This is one seam — the reviewer — widened along the pattern the
  `improve`/`update` steps already use.

---

## 2. Which LLM gets folder access? (the core clarification)

**Not whetstone's.** The built-in reviewer is a single structured call with no tools
(`llm_reviewer.py:86`): system = guidance + wiki + precedents, user = the numbered diff, out =
`{findings:[…]}`. It cannot open files, and this design does **not** change that.

The agentic, folder-reading LLM is **the operator's, running inside a reviewer program that
whetstone shells out to.** Whetstone's job is to hand that program everything it needs and take back
findings. The boundary:

```
          whetstone (orchestrator)                          your reviewer program
  ┌───────────────────────────────────┐            ┌───────────────────────────────────┐
  │ picks the case / diff             │            │ reads context.source_root         │
  │ loads guidance (SKILL.md + pages) │  stdin →   │ checks out change.base_ref        │
  │ resolves the context bag          │  JSON      │ runs ITS OWN LLM + file tools:    │
  │ (source_root, schema, …)          │  payload   │   read_file, grep, list_dir, …    │
  │ shells out ──────────────────────────────────► │ decides which of 400k files to    │
  │                                   │            │   open, asks questions, iterates  │
  │ parses findings ◄──────────────────  stdout ── │ emits {findings:[…]}              │
  │ scores / judges / gates           │  JSON      │                                   │
  └───────────────────────────────────┘            └───────────────────────────────────┘
```

So whetstone never gives *a model* tools — it gives *a program* a folder path (and a diff, and the
context bag), and the program decides how to use an LLM against that folder. That keeps whetstone
model-agnostic and keeps the agentic complexity (tool loops, retries, context management) on the
side that wants it.

---

## 3. The integration seam

The `Reviewer` protocol is tiny (`reviewer/base.py`):

```python
class Reviewer(Protocol):
    def review(self, skill: Skill, change: CodeChange) -> list[Finding]: ...
```

`run_skill_recorded` (`harness.py:77`) calls `reviewer.review(skill, case.change)` once per trial
per case, and the reviewer is constructed in exactly one place — `service.py:152`
(`record_eval`; `record_gate` builds it the same way for both sides). So a new
`SubprocessReviewer` implementing that protocol drops straight in, selected when the skill's
`evaluate` step declares a `run:`.

**Two delivery options** (recommend A; B is a thin add-on later):

| | **A — subprocess reviewer (recommended)** | **B — OpenAI-endpoint metadata** |
|---|---|---|
| Mechanism | `evaluate/step.yaml` gets `run:`; whetstone pipes a JSON payload to stdin, reads findings on stdout | Keep the LLM reviewer, but forward the context bag as `extra_body` on the `/v1/chat/completions` call to a gateway you own |
| Folder access | your program has it directly | your gateway has it |
| Precedent in codebase | identical to `improve`/`update` subprocess steps (`improve.py:_run_subprocess`) | new field on the OpenAI client |
| Agentic freedom | full (multi-turn, tools, any model) | full, but hidden behind one request/response |
| Recommendation | **primary** | secondary, for teams already routing through a gateway |

Currently `evaluate` is forbidden from having `run:`/`prompt:` (`steps.py:335`, *"an evaluate step
is configuration, not a program"*). This feature relaxes that specifically to allow `run:` — a
reviewer program — while `prompt:` stays disallowed (the reviewer prompt is the harness's).

---

## 4. The context-variable system

### 4.1 Declaration — `evaluate/step.yaml`, free-form `context:`

```yaml
# skills/architect-skill/evaluate/step.yaml
run: ["python", "reviewer.py"]      # absent → today's built-in LLM reviewer, unchanged

context:
  # machine-specific / secret → resolved from the environment, never committed
  source_root:  { env: HUB_REPO_ROOT, required: true }
  # pinned version so gated runs are reproducible AND hashable (see §6)
  source_ref:   { env: HUB_REPO_REF, pin: true }   # e.g. a commit SHA; optional
  # committed with the skill, relative to the skill folder
  db_schema:    { file: ./references/schema.sql }
  # a plain literal
  api_spec_url: https://internal.example/api/spec.json
  # any YAML value survives: scalars, lists, maps
  ignore_globs: ["**/generated/**", "**/*.pb.go"]
  # …as many as this skill needs; the host never interprets the keys…
```

Value forms (a small `ContextValue` union):

| form | meaning | committed? | hashed? (§6) |
|---|---|---|---|
| `literal` (scalar/list/map) | the value as written | yes | yes |
| `{ env: NAME, required: bool }` | read env var `NAME` (the **name** is committed, not the value) | name only | **no** (machine-local) unless `pin: true` |
| `{ file: ./path }` | contents of a file under the skill folder | yes | yes (by content) |
| `{ env: NAME, pin: true }` | a pinned VCS ref — not a secret, and it determines what is read | name only | **yes** (value shown in full) |

There is no `{ ref: … }` form and no magic on the key name `source_ref`: pinning is the explicit
`pin: true` flag, and `_DIRECTIVE_KEYS` (`context.py:46`) rejects anything else outright. Explicit
beats magic here — a skill can pin any input, and a key called `source_ref` that someone forgot to
pin fails loudly rather than silently dropping out of the digest.

The `{ env: NAME }` form is the same discipline whetstone already uses for `token_env`
(`config.py:174`): the skill commits *that it needs* `HUB_REPO_ROOT` and its name; the value lives
in the environment / `.env` per machine. That's exactly right for a source *path*, which differs on
every checkout and must not be baked into a shared, hashed skill.

### 4.2 Resolution — the host

Same order the rest of whetstone uses (`config.py:3`): **env → `.env` → literal/file → default**.
`.env` is already loaded before anything reads config (`load_config`, `config.py:270`), so a
`HUB_REPO_ROOT` written there is visible. Resolution produces a plain `dict[str, JsonValue]` — the
resolved bag — which is what gets forwarded. The host does not interpret keys; it only resolves the
value forms.

### 4.3 Validation — preflight, at the click

A missing `required` var fails in the **plan** (`preflight.py`), before any case runs — the same
place a missing model or token is caught today — with `context.source_root: HUB_REPO_ROOT is not
set`. The plan also *reports* the resolved bag with secrets redacted, so the operator sees exactly
what the reviewer will get before spending anything. No run dies three cases in because var #7 was
absent.

### 4.4 Forwarding

The resolved bag becomes one key in the reviewer payload (§5). For option B it becomes
`extra_body["whetstone_context"]` on the chat request.

---

## 5. Reviewer I/O contract (subprocess)

Mirrors `improve.py:_run_subprocess`: JSON on stdin, JSON on stdout, `cwd` = the step directory,
`timeout` = `spec.timeout_s`, argv list (no shell).

**stdin** (one review = one invocation):

```jsonc
{
  "skill_id": "architect-skill",
  "guidance": "…SKILL.md body…",
  "pages":    { "references/patterns.md": "…" },
  "change": {                        // the full CodeChange — repo + refs for a checkout
    "repo": "gitlab:acme/hub-backend",
    "base_ref": "<sha>", "head_ref": "<sha>",
    "files": [ { "path": "…/ComponentVersionRiskProfileAppService.java", "hunks": [ … ] } ]
  },
  "diff":    "…numbered unified diff…",   // the same gutter format the LLM reviewer uses
  "context": { "source_root": "/…/hub-backend", "source_ref": "<sha>",
               "db_schema": "…", "api_spec_url": "…", "ignore_globs": [ … ] },
  "wiki":       "…retrieved pages, if any…",
  "limits":  { "timeout_s": 900 }
}
```

No `precedents` key: precedent injection is what the built-in reviewer does *instead of* reading the
source, and a program that can open the repo is better placed to pick its own comparisons. A run
scored by a program therefore records no precedents, rather than recording ones nobody sent.

**stdout** — the existing findings contract (`LLMFindingList`):

```json
{ "findings": [ { "path": "…", "line": 123, "severity": "warning",
                  "message": "…", "rule_id": "R1", "confidence": 0.8 } ] }
```

`line` = line number in the **new** file. Scoring is unchanged: a finding must be structurally
eligible for an expectation before the judge sees it — same file, inside the region, meeting
`severity_min` if declared (`core/matching.py`). The region is the expectation's `line_range`
widened to the footprint of the case's change in that file, so a finding a few lines from the
anchor is judged rather than discarded, and one outside the change is discarded.

**Errors** (same taxonomy as the improve subprocess): `FileNotFoundError` on the program → step
error; non-zero exit → step error with the stderr tail; unparseable/again-invalid stdout → step
error naming what came back. **Policy decision (open, §13):** does a single failed review fail the
whole run, or record that case as "reviewer errored" and continue? Leaning: fail the run — a gate
computed with half the cases silently erroring is not a verdict.

**Trials & concurrency.** `k` trials = `k` invocations; `max_workers > 1` = that many concurrent
subprocesses. An agentic reviewer is heavy, so we add a **separate cap** on concurrent reviewer
processes (independent of `max_workers`, which was sized for cheap in-process calls) and surface it.

---

## 6. Determinism & `skill_hash` (the crux)

A gate scores base vs candidate over the same cases and calls the difference an effect of the
**guidance** — which only holds if *everything else the reviewer sees is identical across the two
runs*. That's why the wiki is deterministic (glob-keyed) and folded into `skill_hash`
(`domain/run.py:279`, via `_feed_wiki`/`_feed_index`). Agentic source access is the thing most able
to break this, so it's handled explicitly:

- **The context declaration is digested, selectively — and it landed in the gate, not in
  `skill_hash`.** `ResolvedContext.hashable` (`context.py:54`) holds the slice that identifies what
  the reviewer reads: `literal` values, `file:` contents, and `pin: true` env values. Machine-local
  `env:` paths are excluded, so the digest is identical on two teammates' machines.
  `ResolvedContext.digest` is recorded as `RunRecord.reviewer_context_digest` and is a component of
  `BaselineKey` (`gates.py:189`), which is what actually enforces the property: *"`agent: 8 steps`
  and `agent: 64 steps` are different instruments, and so is the same agent handed a different
  context bag."* A gate therefore cannot reuse a baseline measured under a different bag.
  `_feed_context` into `skill_hash` was the original plan and is **not** needed for this — see the
  status header.
- **Pin the version, not the path.** `source_root` (a path) is not hashed; `source_ref` (a commit
  SHA) is. For gated runs the reviewer checks out `change.base_ref` (or `source_ref`) so both sides
  read the same snapshot; for **live** reviews it can read HEAD. Changing the pinned ref changes
  `skill_hash`, which correctly retracts the gate — same contract as regenerating the wiki.
- **Residual nondeterminism is surfaced, not hidden.** Whetstone can't force an agent to be
  deterministic, but `k>1` trials already measure per-trial variance (`SkillScore` stdev). A
  tool-using reviewer that wanders shows up as *unstable*, which is the honest signal — and the run
  record names the reviewer and its resolved context so a noisy gate is diagnosable rather than
  mysterious.

**Bottom line:** the sound configuration is *pinned `source_ref` + a reviewer that reads that
snapshot*. Live-HEAD reading is supported and fine for live reviews; for gating it's allowed but
flagged (the plan warns, exactly like the "unknown billing" three-state warning does today).

### 6.1 What still needs doing

**A stored gate result is not retracted when the context bag changes.** `BaselineKey` governs
*baseline reuse*, so it forces a re-score on the **next** gate; it does not invalidate a pass
already on disk. Change `source_ref` without touching guidance and `skill_hash` is unmoved, so the
console still reads `gated`. Impact is bounded — ADR-028 made C6 advisory and no route refuses on it
— so this is a stale badge, not a bad publish. The fix does not need `skill_hash` either: store
`reviewer_context_digest` on the gate result and compare it at read time, exactly as `BaselineKey`
already does. That keeps `skill_hash` a pure function of the `Skill` and sidesteps the §14 snag
entirely.

**Two defects in the digest itself — found by review, now fixed.** Recorded because both had been
silently wrong since Phase 1 shipped, and the symptom in each case was a digest that looked fine.

1. **A literal `source:` entered the hashable slice.** `AgentPolicy.source` is `Any` and its
   docstring invites a literal path; `_agent_context` folds it in as `source_root`, and a
   non-directive is treated as a literal — which goes into `values`, `redacted` **and** `hashable`.
   So `source: /Users/alice/hub-backend` and `source: /home/bob/hub-backend` digested differently
   for identical content, breaking the cross-machine property `context.py` claims and stopping a
   gate ever reusing a teammate's baseline. The `{ env: … }` form was always correct; only the
   literal leaked. `_agent_context` now drops `source_root` from `hashable` where it already
   dropped it from `shown`, since a checkout path is machine-local whichever form declared it.
2. **`required: true` did not catch an empty string.** `_resolve_env` tested `value is None`, so
   `export HUB_REPO_REF=` — what a failed shell expansion leaves behind — passed preflight, and
   with `pin: true` the empty value entered the hashable slice as though it named a snapshot. Empty
   now counts as unset.

---

## 7. Security

- **Arbitrary program execution.** `evaluate: run:` is the same trust boundary `improve`/`update`
  already cross — the operator's own repo, argv list (no shell, nothing re-split on spaces),
  fixed `cwd`, hard timeout. No new class of risk; documented alongside the existing subprocess
  steps.
- **Secrets stay in env, never committed.** The `{ env: NAME }` form commits only the name.
  Resolved secret *values* are redacted in the plan, logs, and the run record — the bag is stored
  as a **digest + redacted view**, not verbatim.
- **Source egress is explicit.** If the reviewer sends source to a cloud model, that's the
  operator's choice, but the context bag makes "this reviewer has the whole repo and a network
  model" legible where it wasn't before. Worth a one-line note in the plan.
- **Path handling is the subprocess's.** Whetstone passes `source_root` and never traverses it
  itself, so no path-escape surface is added on whetstone's side.

---

## 8. Preflight / cost

A subprocess reviewer's model spend is **not** whetstone's to know — same as the improve subprocess,
whose plan says *"this step runs your own program; Whetstone calls no model"* (billing `unknown`).
But the reviewer runs **k trials × N cases × 2 gate sides**, so a slow agent multiplied by that is
the real cost. The plan must state the multiplier plainly (`agentic reviewer × 240 cases × 3 trials
× 2 sides = up to 1,440 reviewer invocations`) even though it can't price each one — the operator
owns the per-call cost, whetstone owns making the volume visible.

---

## 9. Records & provenance

A run/gate must stay attributable. The run record already carries `backend`/`model`; add (all three
shipped — see §14):
- **reviewer identity** — `subprocess: ["python","reviewer.py"]` (or the endpoint/model for the LLM
  reviewer), so "what reviewed this" is never ambiguous;
- **context digest + redacted view** — which resolved inputs shaped the review, secrets redacted;
- **`source_ref`** actually read, so a score is pinned to a snapshot.

This is the same instinct as recording the backend/model: a number with no attached instrument is
what makes a history contradict itself.

---

## 10. Failure modes & edges

- **Missing required var** → caught at preflight (§4.3).
- **Subprocess crash / timeout / bad JSON** → step error; run fails (leaning, §5).
- **Finding outside the case's change** → not eligible to satisfy the expectation
  (`core/matching.py`); it scores as a miss, and as noise if nothing else claims it. A finding
  inside the change but away from the expectation's anchor line *is* eligible — see
  `effective_region`.
- **Repo not at the declared ref** → the subprocess's problem to detect; we pass `base_ref` so it
  *can*. Optional future: a whetstone-provided checkout helper (§13).
- **Per-case repo differences.** `change.repo` already varies per case, so a fleet reviewing cases
  from several repos needs context keyed per-repo. Flagged as open (§13); v1 assumes one repo per
  skill.
- **Concurrency blowups** → the separate reviewer-process cap (§5).
- **Huge diffs** → unchanged; the diff is already what it is.

---

## 11. Files touched

- `steps.py` — a `ContextSpec` / `ContextValue` type on `StepInputs` (or top-level on the step);
  relax `_validate` to permit `run:` on `evaluate` (keep `prompt:` disallowed).
- `reviewer/subprocess_reviewer.py` *(new)* — implements the `Reviewer` protocol via a subprocess,
  building the §5 payload and parsing findings. Reuses the `improve._run_subprocess` shape.
- `context.py` *(new)* — resolve the declared bag (env/`.env`/file/literal/ref), validate required,
  produce the resolved dict + a redacted view + a hash digest.
- `service.py` — in `record_eval`/`record_gate`, choose `SubprocessReviewer` when
  `evaluate.run` is set, else `LLMReviewer`; thread the resolved context in.
- `domain/run.py` — `_feed_context` into `skill_hash` **and** `guidance_hash`; extend the run
  record with reviewer identity + context digest + `source_ref`.
- `preflight.py` — validate + report the context bag (redacted) in the `Plan`; add the
  volume-multiplier line for an agentic reviewer.
- `ui/routers/jobs.py`, `cli.py` — surface the plan additions; no new endpoints.
- `docs/` + `README` — the reviewer contract, the `context:` schema, the determinism rules.

---

## 12. Rollout (phased, back-compat first)

1. **Phase 1 — context bag + subprocess reviewer, no hashing change unless used.** An `evaluate`
   with no `run:` and no `context:` hashes and behaves byte-identically to today (the same
   "hashes as it did before it existed" property the wiki and pages shipped with). Delivers folder
   access for live reviews immediately.
2. ~~**Phase 2 — pinned-ref hashing + gate determinism.**~~ **Superseded.** The goal — making
   agentic reviewers safe to *gate*, not just to run — was met by putting `reviewer` +
   `reviewer_context_digest` into `BaselineKey` rather than `_feed_context` into `skill_hash`. See
   the status header and §6. What remains is the narrower gate-retraction item and the two digest
   defects in §6.1.
3. **Phase 3 — endpoint variant (option B) + optional checkout helper.** `extra_body` forwarding
   for gateway users; optionally a whetstone-managed worktree at `source_ref` so the subprocess
   doesn't manage its own checkout.

---

## 13. Open questions

1. **Failed review = fail the run, or record-and-continue?** (Leaning fail-the-run for gates;
   maybe record-and-continue for exploratory `eval run`.)
2. **Per-case / per-repo context.** `change.repo` varies per case; do we allow context keyed by
   repo, or is one-repo-per-skill enough for v1?
3. **Does whetstone provide the checkout**, or leave it entirely to the subprocess? (Leaning: pass
   `base_ref`, leave checkout to the program in v1; offer a helper in Phase 3.)
4. **`file:` values — hash content or path?** (Leaning: content, like pages.)
5. **Live reviews vs. corpus runs** — should live reviews default to HEAD and corpus/gate runs
   default to `source_ref`, or is that always explicit?
6. **Concurrency cap default** for reviewer subprocesses (1? cores/2? configurable?).

---

## 14. Implementation notes (what shipped in Phase 1)

**Decisions adopted** (the §13 leanings): a failed review raises `StepError`, which fails the run;
one repo per skill (context is not per-case yet); Whetstone passes the change's `base_ref` and
leaves the checkout to the program; `file:` values are hashed by content. The `source_ref` form is
implemented as `{ env: NAME, pin: true }` — general and explicit — rather than a hardcoded key name.

**Shipped**
- `context.py` — `resolve_context(declared, skill_dir) → ResolvedContext{values, hashable, redacted,
  missing}`, with literal / `{env,required,pin}` / `{file}` forms, `../` escape refused.
- `steps.py` — `StepSpec.context`; `evaluate` may now have `run:` (not `prompt:`); `context:` is
  refused without a `run:` reviewer and on non-`evaluate` steps.
- `reviewer/subprocess_reviewer.py` — `SubprocessReviewer` (the §5 stdin/stdout contract), and
  `reviewer/factory.py` — `reviewer_for` / `reviewer_from_step` → `ReviewerChoice`, shared by the
  console and the CLI so they cannot diverge.
- `service.py` — `reviewer=` threaded through **every** reviewer path: `record_eval` → `run_eval`
  → `gate_skills` → `record_gate` (eval + gate), `record_baseline` (the saturation probe, via
  `record_eval`), and `record_review` (live review). The built-in `LLMReviewer` is still the default
  when none is passed. So a skill's `run:` program is the reviewer everywhere the reviewer runs.
- `domain/run.py`, `reviews.py`, `gates.py` — `reviewer` names the instrument (`""` = built-in) and
  `reviewer_context` / `reviewer_context_digest` record the redacted bag and the identity of its
  hashable slice (§9). All three records carry them, the gate included: that is the one C6
  publishes on, and with a source-aware reviewer its `backend`/`model` describe only the judge.
  Assembled by `reviewer/base.ReviewerProvenance`, so a reviewer reports itself once.
- `ui/routers/jobs.py` + `cli.py` — every scoring path resolves the reviewer, refuses a missing
  required var at the plan/click, and (console) annotates the plan with the reviewer, the redacted
  context, the **invocation count** (§8), and a gate-determinism warning. The estimate stops
  counting review calls Whetstone will not make, so the budget check reflects real spend.
- The console shows it: the run drill-down names the reviewer and relabels `backend`/`model` as the
  *judge's* when a program produced the findings; the review page does the same.
- Tests: `test_context.py`, `test_subprocess_reviewer.py`, `test_reviewer_factory.py`, updated
  `test_steps.py`, `record_eval`/`record_review` integration tests in `test_service.py`, and
  `tests/api/test_agentic_reviewer_routes.py` — which drives eval, gate, baseline and live review
  through the real routes and asserts on the stored records, because the console *supplying* a
  reviewer is a separate claim from `record_eval` *honouring* one, and only the route tests fail
  when the wiring is removed.

**Not done — and `_feed_context` is no longer the plan**
- `_feed_context` into `skill_hash`/`guidance_hash` was deferred over a real design snag:
  `skill_hash(skill)` is a pure function of the `Skill`, but the context lives in a *step*, and
  steps are deliberately outside `skill_hash`. Closing it meant either loading the resolved context
  onto the `Skill` at load time or passing a digest into the hash. **Neither is needed:** the
  measurement-identity property landed in `BaselineKey` instead (§6), which is the sounder home for
  it — a hash can assert *the same snapshot was available*, never *the same content was read*, and
  an agent chooses its own reads. The snag is therefore closed by not needing to solve it.
- Still open, and much narrower: gate-result retraction on a changed context bag, plus the two
  digest defects — both in §6.1.
- The OpenAI-endpoint `extra_body` variant (option B), a Whetstone-provided checkout helper, and a
  separate reviewer-process concurrency cap. (Live review and the baseline probe are **now wired** —
  the reviewer program runs on eval, gate, baseline, and live review alike.)

**Also shipped, after review**
- Context directives are validated strictly: a mapping carrying any of `env`/`file`/`required`/`pin`
  must be a well-formed directive. Previously an unrecognised key (`pinned:` for `pin:`) demoted the
  whole mapping to a literal, which forwarded the declaration instead of the value and — worse —
  made `required: true` with a misspelled `env` silently satisfy the preflight it exists to fail.
- Cancel reaches the program. `SubprocessReviewer` runs it under a sliced wait and the harness hands
  over the cancel event, so cancelling no longer waits out `timeout_s` (900s by default) on every
  review in flight.
- The gate's estimate is doubled *before* the budget check, not after — the warning was previously
  computed against one half of the comparison.
