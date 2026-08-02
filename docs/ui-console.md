# Whetstone — Console UI (implementation plan)

**Status:** decided (§1), **Phases 0, 1 and 2 shipped** (§12). All open questions from the first
draft are resolved in §15.

**Goal:** a web console covering every human-facing Whetstone operation — authoring skills, curating
test data, triaging corpus candidates, running and reading evals, comparing versions through the
gate, and validating the judge — while leaving automated monitoring (CI gating, scheduled corpus
pulls) in CI where it belongs.

**Non-goal:** replacing the CLI. The CLI stays the automation surface; the console is the human
surface. Both call the same `service.py` functions, and anything the console can do a script can do.

---

## Table of contents

1. [Decisions](#1-decisions)
2. [Scope](#2-scope)
3. [Design constraints](#3-design-constraints)
4. [Gaps in core today](#4-gaps-in-core-today)
5. [Architecture](#5-architecture)
6. [Configuration](#6-configuration)
7. [Identity, authorization, concurrency](#7-identity-authorization-concurrency)
8. [New data model](#8-new-data-model)
9. [Required core changes](#9-required-core-changes)
10. [The screens](#10-the-screens)
11. [HTTP API surface](#11-http-api-surface)
12. [Phasing](#12-phasing)
13. [Testing](#13-testing)
14. [Packaging & distribution](#14-packaging--distribution)
15. [Decision log](#15-decision-log)

---

## 1. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Frontend stack | **React + TypeScript + Vite.** Built to static assets, served by FastAPI. Maintainers need Node; users never do. |
| D2 | Deployment model | **Local-first, single user, loopback-bound** — but the auth seam is built from day one (§7), so team deployment is configuration, not a rewrite. |
| D3 | Where the console writes | **Configurable `skills_root`**, defaulting to this repo's `skills/`. A separate company skills repo is a config change (§6), not a code change. |
| D4 | Skill-guidance editing | **In scope** — with git write-through and a **gate-before-propose** rule (§10.2) that makes the console enforce the project's core thesis rather than route around it. Built in Phase 3; see §12 for what that phase does and does not yet cover. |

D4 reverses the first draft's recommendation to defer. The concern behind that recommendation —
"a web editor for prose in git duplicates GitLab" — is real, but it is answered by *design* (every
edit is a branch; no MR can open without a passing gate) rather than by cutting scope.

> **Correction.** This row read "In scope, built in full" while Phase 3 was still unbuilt, which
> made the table a statement of intent dressed as a statement of fact — the one thing a scope table
> must not be. It now describes the phase, and §12 says what is in it.

---

## 2. Scope

**In scope — every human operation:**

| Area | Operations |
|---|---|
| Skill registry | Browse, inspect guidance, view triggers / owner / references / provenance |
| Skill authoring | Edit `SKILL.md` and `meta.yaml`, manage triggers, bump version, gate, propose as MR |
| Test data | Author eval cases by hand or capture from real MRs; edit expectations directly on a diff |
| Corpus triage | Review candidates, rewrite expectations, route to a skill, accept / reject with reasons |
| Evaluation | Launch runs, watch progress, cancel, read scores, drill into findings and judge verdicts |
| Gating | Compare two versions, see which cases flipped and why, re-tune tolerances at zero cost |
| Judge validation | Label judge verdicts from real runs, grow the meta-eval set, track judge accuracy |
| Configuration | Model backends, health checks, providers, gate defaults, repo status |

**Out of scope:**

- CI gating (`whetstone eval gate` exit code) — stays a CI job.
- Scheduled corpus pulls — stays a cron job writing candidates the console triages.
- Alerting, uptime dashboards, on-call surfaces.
- Auto-merge. Every console write lands on a branch a human merges.

---

## 3. Design constraints

**C1 — Git stays the source of truth.** ADR-004 makes `skills/` canonical. The console is a *git
client*, not a database app. Every mutation produces files on a branch and, where a `WriteConnector`
is configured, an MR. There is no "publish" step syncing a database into git — the write *is* the
file write.

**C2 — Runs are derived artifacts.** Run records are telemetry. Deleting `.whetstone/runs/` costs
nothing but history. Gitignored.

> **Amended by ADR-008.** `.whetstone/` is no longer uniformly disposable: `gates/` holds the
> evidence C6 checks, so deleting it costs the right to propose a guidance change until the gates
> are re-run. `runs/` is still pure telemetry.

**C3 — Capture must not change scores.** Adding finding/verdict capture must leave every existing
score bit-identical. `tests/golden/` pins exact scores and must pass unmodified.

**C4 — Zero-cost mode is always available.** `PatternReviewer`, `DeterministicJudge`, and
`FakeLLMClient` already exist. The console exposes them as **practice mode**, so the entire UI is
explorable, demoable, and E2E-testable with no credentials and no spend.

**C5 — One process, no infrastructure.** `whetstone ui` starts a single Python process on
`127.0.0.1`, serves prebuilt assets, opens a browser. No Node at runtime, no Redis, no Postgres, no
container.

**C6 — No skill change proposes without evidence.** The console will not open an MR for a guidance
edit that has no passing gate run against the exact edited content. This is the project thesis
("never ships a skill change it can't prove is a net improvement") made structural in the UI.

---

## 4. Gaps in core today

These become Phase 0. All are independently valuable — they improve the CLI whether or not the
console ships.

**G1 — Findings and judge verdicts are discarded.** `core/matching.py:22` returns a bare `bool`:

```python
def expectation_matched(findings, expectation, judge) -> bool:
    return any(judge.match(f, expectation).matched for f in region_candidates(findings, expectation))
```

`core/scoring.py:32` keeps only `Confusion` counts, so `CaseScore.trials` (`domain/score.py:56`) is
four integers per trial. The reviewer's `Finding` objects and the judge's `reason` are computed,
used once, dropped. The central screen — *"this case failed, show me why"* — has no data behind it.

The `any()` short-circuit matters: judging stops at the first match. Capture must preserve it or the
console silently multiplies LLM cost.

**G2 — No run store.** `SkillScore` has `skill_id`, `version`, `k`, `cases` — no id, timestamp,
model, backend, or duration. Two runs aren't comparable without external bookkeeping.
`eval report --run <id>` is sketched at `milestone-1-eval-harness.md:409`, never built.

**G3 — No git write path.** `vcs.py` has one function, `export_tree`, read-only. C1 has no
implementation.

**G4 — The harness is blocking and silent.** `run_skill` (`core/harness.py:10`) is a nested loop of
synchronous LLM calls with no concurrency, progress, or cancellation.

**G5 — Corpus triage has no edit step.** `corpus/builder.py:63` sets the ground-truth expectation to
the raw first comment of a review thread:

```python
semantic = thread.comments[0].body if thread.comments else ""
```

In real repos that is `"nit: use ? here"`, `"see above"`, `"👍"` — and it becomes the text the judge
scores every finding against. `corpus promote` (`cli.py:204`) is a verbatim `shutil.copyfile`. The
human rewrite step that must exist has no tooling.

**G6 — `Skill.version` is unenforced.** Hand-set in frontmatter (`core/loader.py:41`) and trivially
stale. Comparison keyed on `version` silently compares unlike things; records need a content hash.

---

## 5. Architecture

### 5.1 Process model

```
  browser — React SPA (prebuilt static assets)
        │  JSON over HTTP  ·  SSE for run progress
        ▼
  FastAPI app  (src/whetstone/ui/)
        ├── routers/     thin HTTP over service.py — no business logic
        ├── deps.py      principal, config, repo handle
        ├── jobs.py      ThreadPoolExecutor + progress bus + cancellation
        └── static/      built SPA (generated; shipped in the wheel)
        │
        ▼
  whetstone.service   ← unchanged public surface, already the intended seam
        │
        ├── whetstone.runs     RunRecord persistence   (new)
        ├── whetstone.gitio    branch / commit / push  (new)
        ▼
  core · reviewer · judge · llm · providers · corpus
```

Routers are deliberately anaemic: parse → call a `service.py` function → serialize. Logic appearing
in a router belongs in `service.py`, where the CLI can reach it too.

### 5.2 Backend layout

```
src/whetstone/
  runs.py                    # RunRecord save/load/list/reindex           (new)
  gitio.py                   # git read+write primitives                  (new)
  config.py                  # whetstone.toml loading                     (new)
  domain/run.py              # RunRecord and friends                      (new)
  ui/
    __init__.py
    app.py                   # FastAPI factory, static mount, SPA fallback
    deps.py                  # Principal, Config, Repo, ReadOnly guard
    jobs.py                  # job registry, worker pool, SSE event bus
    errors.py                # SkillLoadError → 422 with field pointers
    routers/
      skills.py  cases.py  candidates.py  runs.py  gate.py
      meta_eval.py  git.py  config.py
    static/                  # built SPA — generated, gitignored, wheel-included
```

### 5.3 Frontend layout

```
ui/                          # repo root; not shipped, only its build output is
  package.json  vite.config.ts  tsconfig.json  tailwind.config.ts
  src/
    main.tsx  router.tsx
    api/            generated client + TanStack Query hooks
    components/
      diff/         DiffView · LineGutter · RegionSelect · RegionOverlay
      run/          ScoreHeader · CaseTable · TrialDrilldown · VerdictCard
      shared/       Badge · Sparkline · KeyboardHint · ConfirmCost · Toast
    routes/
      skills/       Index · Detail · Guidance · Cases · Runs
      triage/       Queue
      cases/        Editor
      runs/         Index · Detail · Compare
      judge/        Lab
      settings/     Backends · Repo · Gate
```

| Concern | Choice | Rationale |
|---|---|---|
| Server state | **TanStack Query** | Every screen is server-state-dominated. Caching, invalidation, and polling come free; no global store needed. |
| Local state | **`useState` / `useReducer`** | The only genuinely stateful widget is the diff region selector. Redux would be ceremony. |
| Routing | **React Router** | Deep-linkable run/case URLs matter — people paste them into MRs. |
| Styling | **Tailwind v4** (Vite plugin) | Data-dense internal tooling; utility classes keep velocity high without a design system to maintain. |
| Primitives | **Radix UI** (headless) | Menus, dialogs, tooltips with correct keyboard and ARIA behaviour. No visual opinion imposed. |
| Charts | **Custom SVG** | Only sparklines and one accuracy trend line. A charting library is disproportionate. |
| API types | **Generated from OpenAPI** | FastAPI emits the schema from the existing pydantic models; `openapi-typescript` keeps the client in lockstep. Domain drift becomes a type error. |

**The diff component is custom.** `FileChange.added` already carries new-file line numbers
(`domain/change.py:12`) — precisely what expectation regions key on (`Region.contains`,
`domain/refs.py:30`). An off-the-shelf viewer would need that mapping rebuilt, and the interaction
we need (drag to select a line range, overlay existing expectations, anchor findings to gutters) is
the whole point. Contract:

```tsx
<DiffView
  files={change.files}
  selection={range}                 // [lo, hi] in new-file numbering
  onSelect={setRange}
  overlays={[{ range, kind: "expectation" | "finding", severity, label }]}
/>
```

### 5.4 Dependency hygiene

```toml
[project.optional-dependencies]
ui = ["fastapi>=0.115", "uvicorn[standard]>=0.30"]
```

`whetstone ui` fails with an actionable install message if the extra is absent — the same lazy-import
discipline `AnthropicClient` already uses. Core CLI and existing CI stay untouched.

---

## 6. Configuration

`whetstone.toml`, discovered upward from CWD (new `config.py`). Resolution order for every field:
**CLI flag → environment → file → default**, matching `llm/factory.py`.

```toml
[skills]
root = "skills"                  # D3: path to the skill registry
repo = "."                       # git repo containing it; may be a separate checkout

[git]
branch_prefix = "whetstone/"
default_base = "main"
push_remote = "origin"
author = "principal"             # "principal" | "console"
protected_branches = ["main", "master"]

[ui]
host = "127.0.0.1"
port = 8787
read_only = false
practice_mode = false
trust_proxy_headers = false      # D2: must be explicitly enabled to deploy for a team

[runs]
dir = ".whetstone/runs"
max_llm_calls_per_run = 2000     # preflight warning when the estimate exceeds it (preflight.check_budget)

[gate]
recall_tol = 0.0
fp_tol = 0.0
reuse_baseline = true            # reuse a base-side score an earlier gate already measured
baseline_max_age_hours = 24.0    # how old that measurement may be; 0 also disables reuse
```

**On reusing the baseline.** A gate scores the last commit and the working tree. The last commit
does not change between two gates ten minutes apart, so measuring it twice costs double — once in
spend, and once in variance, because a second sample of a nondeterministic reviewer can fail a gate
on its own. Two real gates 6.5 minutes apart over byte-identical content disagreed on one case, and
one of them blocked a change the other had passed.

Reuse is offered only when every input that could move the number is identical: the base content,
the case set actually drawn (the **union** of both sides, so a new candidate case forbids reuse),
the judge, the reviewer identity and context, the backend and model, `k`, practice mode, and the
wiki/precedent budgets. The one input that key cannot see is a provider changing the model behind a
name, which is what `baseline_max_age_hours` is for. A record that borrowed a baseline says so —
`base_from_gate` and `base_measured_at` — and both are carried forward through a chain of reuses,
so ten gates reusing one measurement all age from the original.

Force a fresh measurement for one run with `--fresh-baseline`, `{"fresh_baseline": true}` on the
gate job, or the *re-measure the baseline* checkbox beside **Run the gate**.

Pointing the console at a company skills repo is `[skills] repo = "../company-skills"`. `gitio`
operates on whatever repo contains `skills.root`, so nothing else changes.

---

## 7. Identity, authorization, concurrency

Built in Phase 1, not retrofitted — this is what makes D2 a config change later.

**Principal.** A FastAPI dependency resolving to `Principal(name, email, mode)`:

- **Local mode (default):** reads `git config user.name` / `user.email`. Mode is `owner`.
- **Proxy mode:** reads trusted headers (`X-Forwarded-User`, `X-Forwarded-Email`) — **only** when
  `trust_proxy_headers = true`. Intended to sit behind an OIDC reverse proxy.

We do not hand-roll authentication. There is no password, no session, no token. A team deployment
puts an authenticating proxy in front and flips one flag.

**Read-only mode.** A single router-level dependency guards every mutating route. `read_only = true`
turns the console into a safe dashboard for anyone; the UI hides write affordances based on
`GET /api/config` rather than discovering the 403 on click.

**Attribution.** Commits are authored as the principal (`git.author = "principal"`, the default), so
a shared deployment attributes correctly; `"console"` uses the repo's own identity instead, for
deployments where the proxy-supplied name is an authentication detail rather than something to write
into permanent history. Every `RunRecord` and every promote/reject records the principal regardless.

**Publishing.** `gitio.check_publishable` refuses any branch that is protected or outside
`branch_prefix`, before a remote is even consulted. The branch on a propose request comes from the
client, and publishing is the one action here with no local undo.

**Concurrency — optimistic, keyed on git.** Every read of an editable resource returns the commit
sha it was read at. Every write sends it back. If `HEAD` moved for those paths, the write returns
**409** with the competing diff instead of clobbering. Git is already the concurrency-control
mechanism; the console just refuses to fight it. Same rule prevents the console from stomping an
edit made in a text editor while a tab was open.

**Dirty-tree rule.** `gitio` refuses to write when the working tree is dirty in the paths being
touched, and surfaces the offending diff. Never `git checkout .`, never `git stash`, never a
destructive recovery path.

---

## 8. New data model

`src/whetstone/domain/run.py` — pydantic throughout, no new concepts leaked into existing types.

```python
class JudgeVerdictRecord(BaseModel):
    finding_index: int           # index into TrialRecord.findings
    matched: bool
    confidence: float
    reason: str

class ExpectationOutcome(BaseModel):
    expectation_id: str
    must: Must
    outcome: Literal["tp", "fn", "fp", "tn"]
    eligible_finding_indices: list[int]    # survived the structural prefilter
    verdicts: list[JudgeVerdictRecord]     # only findings actually judged (short-circuit preserved)

class TrialRecord(BaseModel):
    index: int
    findings: list[Finding]                # everything the reviewer said, matched or not
    outcomes: list[ExpectationOutcome]
    confusion: Confusion

class CaseRun(BaseModel):
    case_id: str
    kind: EvalKind
    trials: list[TrialRecord]

class RunRecord(BaseModel):
    id: str                      # timestamp-prefixed, lexically sortable
    created_at: datetime
    principal: str
    skill_id: str
    skill_version: int
    skill_hash: str              # sha256 over SKILL.md body + every eval case — the real identity
    backend: str                 # the judge's, and the reviewer's too unless `reviewer` is set
    model: str
    reviewer: str                # "" = built-in; else e.g. "subprocess: python reviewer.py"
    reviewer_context: dict       # a custom reviewer's inputs, redacted (`<env:NAME>`, `<file:…>`)
    reviewer_context_digest: str # identity of the hashable slice of those inputs
    reviewer_effort: Effort
    judge_effort: Effort
    k: int
    practice_mode: bool
    duration_s: float
    llm_calls: int
    cases: list[CaseRun]
    score: SkillScore            # existing type, unchanged
    git_ref: str | None          # commit sha the skill was read at, when the tree was clean
```

Three details that carry weight:

- **`skill_hash` is the identity, not `version`** (G6). The console compares and caches on the hash,
  and warns when two runs share a version but differ in hash — a stale version bump, caught for free.
- **`findings` holds everything the reviewer returned**, including findings matching no expectation.
  Those are the interesting ones: unlabeled true positives (→ a new `should_catch` case) or noise
  (→ a new `should_not_flag` case). The console turns them into one-click case proposals.
- **`llm_calls` makes cost estimation self-calibrating.** Reviewer calls are exactly `cases × k`;
  judge calls depend on how many findings survive the prefilter, which is unknowable in advance. The
  estimator uses the trailing mean per skill from prior records, falling back to a conservative
  multiplier on first run.
- **`reviewer` names the instrument, because `backend`/`model` may not.** A skill can replace the
  reviewer with a program of its own ([skill-pipeline](skill-pipeline.md#bring-your-own-reviewer)),
  which runs a model Whetstone never sees — so on such a run `backend`/`model` describe the *judge*
  alone, `llm_calls` counts judge calls only, and the drill-down relabels both fields accordingly
  rather than presenting a model that did not produce the findings. `reviewer_context` records which
  inputs shaped the review, with environment values reduced to their names. `ReviewRecord` and
  `GateRecord` carry the same three fields; the gate especially, since it is the record C6 publishes
  on and "what measured this?" has to be answerable from it alone.

**Disk layout** (gitignored):

```
.whetstone/
  runs/<run-id>.json      # the record — files are truth
  runs.db                 # derived SQLite index, safe to delete, rebuilt by `runs.reindex()`
  gates/<gate-id>.json    # evidence for C6; the filename carries the content hash it covers,
                          # which is why this needs no index — the only query is exact-match
  reviews/<review-id>.json  # a skill's findings on a live change, plus the rulings made on them
```

---

## 9. Required core changes

Surgical, independently valuable, no UI code.

### 9.1 Capture without changing scores — `core/matching.py`, `core/scoring.py`

Add a recording variant beside the existing predicate, preserving short-circuit semantics exactly:

```python
def evaluate_expectation(findings, expectation, judge) -> ExpectationOutcome:
    """Same decision as expectation_matched, but records what was judged and why."""
    eligible = region_candidates(findings, expectation)
    verdicts = []
    for f in eligible:
        m = judge.match(f, expectation)
        verdicts.append(JudgeVerdictRecord(finding_index=findings.index(f), matched=m.matched,
                                           confidence=m.confidence, reason=m.reason))
        if m.matched:
            break                        # identical short-circuit → identical LLM cost


def expectation_matched(findings, expectation, judge) -> bool:
    return any(v.matched for v in evaluate_expectation(findings, expectation, judge).verdicts)
```

`score_trial` gains a sibling returning `(Confusion, list[ExpectationOutcome])`; the existing
signature stays. **`tests/golden/` must pass unmodified** — that is the proof C3 held.

### 9.2 Progress, parallelism, cancellation — `core/harness.py`

```python
def run_skill(skill, reviewer, judge, k=1, *,
              on_event: Callable[[RunEvent], None] | None = None,
              max_workers: int = 1,
              cancel: threading.Event | None = None) -> SkillScore: ...
```

Cases are independent, so a `ThreadPoolExecutor` over cases is safe and near-linear on network-bound
work. Defaults keep current behaviour byte-identical for the CLI and tests.

### 9.3 Git write — `src/whetstone/gitio.py` (new)

```python
def status(repo) -> RepoStatus                       # branch, clean, head sha, remote
def read_at(repo, ref, path) -> str                  # complements export_tree
def create_branch(repo, name, *, base) -> None
def write_and_commit(repo, files, message, *, branch, author, expect_head) -> str   # 409 on drift
def push(repo, branch, *, remote) -> None
def open_change_request(connector, repo, branch, title, body) -> str
```

Safety rules live in the module, not the UI: never commit to the default branch; always a
`whetstone/<kind>/<slug>` branch; refuse on a dirty tree in the touched paths; `push` is never
implicit. MR creation goes through the existing `WriteConnector` protocol (`providers/base.py`) —
designed for exactly this in M1 and left unimplemented.

### 9.4 Run persistence — `src/whetstone/runs.py` (new)

`save`, `load`, `list(filters…)`, `reindex`. Files are truth; SQLite indexes
`(skill_id, created_at, skill_hash, recall, fp_rate, model)` for history and trend queries.

### 9.5 Service additions — `service.py`

- `run_eval` / `gate_skills` gain an optional recorder and return a `RunRecord` alongside the score.
- `promote_candidate(candidate, skill, edits)` — the *edited* promote `cli.py:204` lacks. Validates
  by round-tripping through `load_skill` before writing.
- `save_skill_edit(skill_dir, guidance, meta)` — serializes frontmatter + body back to `SKILL.md`,
  preserving field order, and bumps `version`.
- `estimate_cost(skill, k)` → `CostEstimate`, from the trailing `llm_calls` mean (§8).

### 9.6 CLI additions — `cli.py`

`whetstone ui`, `whetstone runs list|show`, `whetstone report --run <id> --format html`. Console and
CLI stay feature-equivalent.

---

## 10. The screens

> **Note (later phases).** This section records the phase-0 design. Two screens landed on top of it
> since: the **Inbox** (`/`) is now the console's home — "the work queue is the landing page" below
> describes the Skills index's sort order, not the entry route — and a top-level **Status** page
> (`/status`) summarises the fleet's rot, judge accuracy and watch state. The skill-detail tabs also
> gained **Improve**, the guided score → sharpen → gate → propose loop.
>
> **The case model changed too.** Triage no longer promotes onto a `whetstone/cases/batch-N`
> branch. A promotion now writes the case to `skills/<id>/promoted_cases/` **on disk**, and a human
> **graduates** the ones that earn it into `eval_cases/` (the corpus that scores and gates). So every
> "batch branch" / "Propose N cases" reference below is retired — see the README's
> [Promoting, scoring, graduating](../README.md#promoting-scoring-graduating). Cases are the test
> suite *for* a skill, read as folders, independent of git; the [screens](../README.md#the-screens)
> section of the README is the current surface.

### 10.1 Skills index — worst first

Card per skill: id, name, owner, version, case split (`8 catch / 5 noflag`), latest recall / fp_rate,
a recall sparkline over recent runs, and a badge when the last gate failed. Default sort is
**weakest first** (lowest F2), so the work queue is the landing page. Filter by owner and trigger
path.

Answers "which of our skills is actually weak?", which today needs a CLI run per skill and eyeballing.

### 10.2 Skill detail

- **Guidance** — rendered `SKILL.md` with rule ids (`R1`, `R2`) anchored and deep-linkable. Each rule
  shows its `meta.yaml` provenance inline (`R1 ← acme/payments!812#note_44`) and which eval cases
  exercise it. A rule with no cases is flagged **untested guidance** — a gap nothing surfaces today.
- **Edit** (D4) — a markdown editor beside a live-rendered preview, with the case list pinned
  alongside so you can see what constrains the rule you're rewriting. On save:

  1. Writes to a `whetstone/skill/<id>-<slug>` branch (never the default branch).
  2. Bumps `version` automatically, recomputes `skill_hash`.
  3. **Enables nothing else until a gate run exists for that hash.** The *Propose MR* button is
     disabled, with the reason stated: *"needs a passing gate — run one."* One click launches the
     gate (edited content vs. the base ref) and, on PASS, the button lights up.

  This is C6 made concrete: the console cannot be used to route around the gate. An escape hatch —
  **Open in editor** — writes the staged file and hands off for anyone who'd rather use their own
  tools; the same gate rule still applies to the resulting branch.
- **Eval cases** — table with kind, provenance, last outcome, and cross-trial flakiness. Sort by
  most-frequently-failing.
- **Runs** — history (§10.5).
- **Metadata** — owner, references, triggers; edits follow the same branch flow.

### 10.3 Triage queue — the centrepiece

Three panes, keyboard-driven.

```
┌────────────┬───────────────────────────────┬──────────────────────┐
│ QUEUE      │ DIFF                          │ EXPECTATION          │
│            │                               │                      │
│ ▸ 812-t0   │  src/handlers/charge.rs       │ kind  ◉catch ○noflag │
│   0.9 ✱    │                               │ skill [rust-errors▾] │
│   812-t1   │  40  let row = db.get(id)     │ lines [40 – 45]      │
│   0.5      │  41      .unwrap();      ◀──  │                      │
│   813-cl0  │  42  process(row);            │ ORIGINAL COMMENT     │
│   0.3      │                               │ "nit: use ? here"    │
│            │                               │ ─────────────────────│
│ 47 pending │                               │ SEMANTIC (editable)  │
│ 12 today   │                               │ [unwrap on the DB    │
│            │                               │  result can panic on │
│            │                               │  a normal error path]│
│            │                               │ severity_min [warn▾] │
└────────────┴───────────────────────────────┴──────────────────────┘
   j/k move   a accept   x reject   e edit   ⏎ promote   b batch
```

Design decisions that matter:

- **The whole review thread is shown, not just the comment that seeded the expectation.** Amended
  after the first version shipped: the middle column led with the diff, and a queue dominated by
  `merged clean` candidates therefore read as an undifferentiated list of code changes with no
  visible connection to the review process it was supposedly learning from. A diff on its own *is*
  just a code change. What makes it a candidate is what somebody said about it, so the conversation
  leads and the diff follows.

  The thread is carried on the candidate (`Discussion` in `corpus/model.py`) and written into
  `candidate.json` at pull time rather than fetched when triage opens. Triage happens long after the
  pull, often by someone without access to the forge, and a case whose evidence is a hyperlink is a
  case nobody checks.
- **Every candidate is badged with its signal, and the queue can be filtered by it.** The row used
  to show an id, a confidence and a skill — which made a 0.30 guess-from-silence look exactly like a
  0.90 applied suggestion until you clicked it. The confidence number's *meaning* is the signal, so
  the signal leads. The filter chips double as a legend, each carrying what the signal claims.
- **The raw comment and the semantic field sit side by side, both visible, only the semantic
  editable.** This is the fix for G5: the human *rewrites* rather than retypes, and can see exactly
  what signal is being transformed into ground truth.
- **One viewport tall, three independently scrolling panes, actions pinned.** Triage is a volume
  activity; a page that scrolls as a whole puts the promote button below a long thread and lets a
  hundred queued candidates push the diff out of view.
- **Line range is dragged on the diff, not typed.** The region is the field most likely to be wrong
  in an auto-generated candidate.
- **Reject requires a reason**, stored with the candidate. Rejections are evidence for tuning the
  builder's confidence heuristics (`corpus/builder.py`); today they vanish.
- **Promote validates before writing** — the edited case round-trips through `load_skill`, and
  `SkillLoadError` renders inline against the offending field (`ui/errors.py` maps the exception's
  path prefix to a field pointer).
- **An optional rule citation.** Setting *Evidence for rule* files the source MR under that rule id
  in the skill's `meta.yaml`, committed alongside the case. That block is the only record of why a
  piece of guidance exists and it feeds `rule_ids` / `untested_rules`; leaving it to a follow-up
  commit meant it drifted from the cases it was supposed to explain. The metadata is read from disk
  (the working tree), so consecutive promotions in one session accumulate rather than overwrite.
- **Clean-merge sampling** for the `should_not_flag` candidates, which arrive at confidence 0.3 and
  are largely uniform. They are sampled — `max_clean_files`, default 5 per MR — so one large
  comment-free merge cannot bury the high-signal candidates above them.
- Each promotion writes a case to `skills/<id>/promoted_cases/` **on disk**; a human then
  **graduates** the ones that earn it into `eval_cases/`. See the README's
  [Promoting, scoring, graduating](../README.md#promoting-scoring-graduating).

### 10.3b Live review — ruling on the skill's own output

Added after the console shipped, in answer to a question the design had no answer to: *the skill
reviewed an open MR and produced N findings — how do I tell it which are right?*

Everything in §10.3 mines **history**. It reads a conversation between humans and infers what a
reviewer should have said about code it never saw. That inference is the weakest link in the whole
corpus, and it is avoidable: run the skill on a change that is open right now, show the findings,
and let a person rule on them directly.

`whetstone review` produces the record; this screen adjudicates it. Two panes — findings and diff —
with the selected finding's cited lines highlighted, because a reviewer pointing at the wrong line
is one of the ways a finding is wrong.

Design decisions that matter:

- **A ruling mints a triage candidate, not an eval case.** The extra hop looks like ceremony and is
  not. A case built straight from a confirmed finding asserts "the reviewer must say *this*", where
  *this* is the reviewer's own message — it would grade the reviewer against its own words and pass
  forever. Triage is where that gets rewritten (G5, again, in a new place).
- **A rejected finding outranks a confirmed one** (0.95 vs 0.90). "Stay silent here" is complete on
  its own and depends on no text being right; "say this" is only as good as text nobody has fixed
  yet. It also maps to `confirmed` rather than `silence` precision evidence, which is the first
  signal in the project that measures precision without measuring quietness.
- **Not a suppression list.** The obvious reading of "mark it false so it stops saying it" is a
  mute button, and a mute button hides the false positive instead of removing it. The case goes
  through the gate, so the next guidance change that reintroduces it is refused.
- **The reviewed head is pinned.** An open merge request is force-pushed, rebased and added to;
  findings carry line numbers, and a superseded head makes them point at different code.
- **Reviews of edited guidance are marked stale.** The record stores the `skill_hash` that produced
  the findings. Once the guidance changes they describe a reviewer that no longer exists.
- **A finding citing a file outside the diff is refused at ruling time**, not left for `promote` to
  reject later with less context. Reviewers do invent paths.
- **Whetstone need not be the thing that runs the reviewer.** `POST /api/reviews` ingests a review
  produced anywhere — CI, an agent harness, an editor — with the change, the findings and any
  rulings in one payload. This is probably the more common shape in practice: the skill already runs
  somewhere against the real merge request, and what has to come back is the labels. Its value here
  is the corpus and the gate, not the reviewing, and the boundary should say so.
- **The explanation is the expectation.** The optional note beside a ruling is not a comment field.
  On a confirmed finding it *becomes* the case's `semantic`, which is what breaks the circularity
  above at the moment the judgement is made rather than leaving it for triage. On a rejected one it
  becomes the rationale. Making it a free-text afterthought would have wasted the most useful
  sentence anyone types on this screen.
- **An uploaded review's guidance version is a claim, not a fact.** `skill_hash` is optional on the
  payload; without it the record marks itself `skill_hash_assumed` and the console badges it
  **version assumed**, because staleness is computed against that hash and silently assuming it
  would make "not stale" mean nothing.
- **A case belongs to the skill that produced the finding**, not to whatever `route_to_skill`'s
  globs match first. Path routing exists for mined comments, which have to be guessed at; a finding
  already knows its own skill, and in a registry where several skills answer for one language,
  letting the glob decide files every case under whichever skill sorts first.
- **A ruling on a candidate somebody already promoted is a 409, not an overwrite.** The queue hides
  decided candidates, so rewriting one is invisible — and the committed eval case would stop
  matching the record it came from. `undo_verdict` already refused the same case.
- **The list endpoint returns a summary, not the record.** A `ReviewRecord` carries the whole
  `CodeChange`; a row shows eight scalars. Same defect as the triage queue's payload, and worth
  naming twice because it is the shape that recurs whenever a list model is "the detail model".

### 10.4 Case editor — "add test data"

Same three-pane shape, two entry points:

- **From history** — pick a project and MR; the diff loads through the `SourceConnector`; drag to
  select the region; write the expectation. The good path.
- **By hand** — paste or upload a unified diff. Authoring a *synthetic* diff in a textarea is worse
  than in an editor, so this path also offers **Open in editor**, which writes a scaffolded case
  folder and hands off.

A live `case.yaml` preview sits beside the form, so what gets committed is never a surprise.

### 10.5 Run detail — "why did this fail?"

Header: score, model, effort, k, duration, LLM calls, cost, skill hash (with a stale-version warning
when applicable).

Case rows expand into the drill-down that does not exist today:

```
▾ unwrap-in-handler        should_catch      recall 0.60  (3/5 trials)   ⚠ flaky

  Trial 1  ✓ TP    Trial 2  ✓ TP    Trial 3  ✗ FN    Trial 4  ✓ TP    Trial 5  ✗ FN

  ▾ Trial 3 — FN
    Expected: "unwrap on the DB result can panic on a normal error path"
              src/handlers/charge.rs lines 40–45   severity ≥ warning

    Reviewer findings (2):
      ⊘ charge.rs:41  warning  "consider handling this error"    conf 0.4
        └ judge: NOT MATCHED — "the finding is generic and does not identify
                 the unwrap as the panic source"                 conf 0.8
      ○ charge.rs:88  info     "unused import"                   conf 0.9
        └ outside expectation region (lines 40–45) — not judged

    [ Label this verdict ]   [ Loosen expectation ]   [ Open in judge lab ]
```

This screen is the strongest justification for the console. Today a flaky case surfaces as
`recall 0.60` and nothing else — you cannot tell whether the reviewer missed it, the judge was wrong,
or the expectation is badly worded. Those have three different fixes.

Findings matching nothing get their own section with **propose as new eval case**, turning reviewer
output into corpus growth for free.

### 10.6 Compare / gate

Two runs, or two git refs, side by side:

- Left: word-level diff of the `SKILL.md` guidance.
- Right: per-case outcome delta — `✓→✓`, `✓→✗` (regression, red), `✗→✓` (improvement, green).
  Both sides are scored over the union of their eval cases, so a case that exists on only one side
  still gets a row; it is the guidance that varies between the runs, not the questions.
- Header: the `GateResult` verdict with `reasons` rendered as prose, plus `fixed_cases` — a change
  that names targeted cases has to make them pass, and this is where "it earned its keep" shows.
- **Tolerance sliders** (`recall_tol`, `fp_tol`) recompute the verdict **client-side from stored
  records** — no re-run, no spend. You can see exactly how much slack a change would need, which
  turns "what tolerance is right?" from a guess into an observation.

Makes causality legible: *you added R3; case X now passes; case Y started false-positiving.*

### 10.7 Judge lab

The judge decides every score, so an unvalidated judge corrupts everything. Today the labeled set
(`tests/fixtures/meta_eval/labeled.json`) is hand-maintained JSON.

The lab lists real verdicts captured from runs, shows finding + expectation + the judge's reason, and
offers agree / disagree. Each label appends to the fixture on a branch. Because §9.1 already captures
every verdict, **this dataset grows as a byproduct of normal use** — the guardrail strengthens itself.
Plus an accuracy trend against `JUDGE_ACCURACY_FLOOR`.

### 10.8 Settings

Backend presets (`llm/factory.py:PRESETS`) with a health-check button wired to the existing
`llm check` logic. Provider registry status. Gate defaults. Repo status (branch, clean, remote,
staged console changes). Global **practice mode** toggle (C4).

### 10.9 Proposal review *(M2-ready)*

When the proposal engine lands, its output — a skill diff plus the gate result justifying it —
renders in the §10.6 compare view with approve / reject, and approval opens an MR. Same component,
new data source. Designed for now, built when M2 exists.

---

## 11. HTTP API surface

Extends the sketch at `milestone-1-eval-harness.md:412`. Responses are existing pydantic models;
TypeScript types are generated from the emitted OpenAPI schema.

```
GET    /api/config                        → capabilities, read_only, practice_mode, principal
GET    /api/skills                        → [SkillSummary]  (+ latest score, sparkline data)
GET    /api/skills/{id}                   → Skill + case summaries + run history + head sha
PUT    /api/skills/{id}/guidance          → stage SKILL.md edit    (branch write, 409 on drift)
PUT    /api/skills/{id}/meta              → stage meta.yaml edit
GET    /api/skills/{id}/proposal          → staged diff + gate status + can_propose reason  (C6)

GET    /api/skills/{id}/cases/{case_id}   → EvalCase + diff + outcome history
POST   /api/skills/{id}/cases             → create case (validated round-trip)
PUT    /api/skills/{id}/cases/{case_id}   → stage case edit
DELETE /api/skills/{id}/cases/{case_id}   → stage deletion

GET    /api/candidates                    → triage queue (filter: skill, kind, confidence)
POST   /api/candidates/{id}/promote       → edited promote into a skill
POST   /api/candidates/{id}/reject        → reason-tagged rejection

POST   /api/reviews                       → ingest a review run anywhere: change + findings + rulings
GET    /api/reviews                       → live reviews awaiting rulings (filter: skill)
GET    /api/reviews/{id}                  → ReviewRecord + rendered diff + staleness
POST   /api/reviews/{id}/findings/{n}/verdict   → rule correct/false; mints a triage candidate
DELETE /api/reviews/{id}/findings/{n}/verdict   → undo, removing the candidate if still undecided

POST   /api/runs/estimate                 → CostEstimate — calls + spend, no LLM touched
POST   /api/runs                          → {skill, k, backend, model, practice} → run_id  (202)
GET    /api/runs                          → history, filterable
GET    /api/runs/{id}                     → RunRecord
GET    /api/runs/{id}/events              → SSE progress stream
DELETE /api/runs/{id}                     → cancel

POST   /api/gate                          → {base, candidate} → GateOutcome  (accepts run ids)
POST   /api/gate/recompute                → re-apply GateConfig to stored records, no LLM

GET    /api/meta-eval/pending             → unlabeled verdicts drawn from runs
POST   /api/meta-eval/label               → append a human label
GET    /api/meta-eval/report              → MetaEvalReport + accuracy trend

GET    /api/git/status                    → branch, clean, staged console changes
POST   /api/git/propose                   → commit staged changes, push, open MR  (C6-guarded)

GET    /api/backends                      → PRESETS + resolved env
POST   /api/backends/check                → live health check
```

**Cost control.** `POST /api/runs/estimate` returns projected call count and spend before anything is
launched; the UI requires confirmation above a threshold. `runs.max_llm_calls_per_run` aborts runaway
jobs. Practice mode reports an estimate of zero and never constructs a real client.

**Error contract.** `SkillLoadError` and pydantic `ValidationError` map to **422** with
`{field_path, message}` so the UI can attach errors to inputs instead of showing a toast.

---

## 12. Phasing

Each phase ships something usable. Estimates assume one developer.

### Phase 0 — Core foundations *(no UI)* — ✅ **done**

1. ✅ `domain/run.py` — the record types (§8), plus `skill_hash` and `RunEvent`.
2. ✅ `evaluate_expectation` + recording `score_trial` (§9.1). `tests/golden/` green **and
   unmodified**; a parity test asserts recording issues the same number of judge calls as the
   predicate it replaced.
3. ✅ `runs.py` — save/load/list/delete/reindex, JSON files + self-healing SQLite index, and
   `stale_version_ids` for the hand-maintained-`version` trap (G6).
4. ✅ `harness.run_skill` — `on_event`, `max_workers`, `cancel` (§9.2); defaults unchanged, so
   sequential call ordering (which prompt-recording fakes depend on) is preserved.
5. ✅ `gitio.py` — status, branch, write+commit with `expect_head`, push (§9.3). Commits are built
   with plumbing against a temporary index, so **the working tree and current branch are never
   touched**; `update-ref` CAS provides the optimistic concurrency of §7 for free.
6. ✅ `config.py` — `whetstone.toml` resolution (§6).
7. ✅ `service.record_eval`, `llm/counting.py`, `llm/factory.resolve_backend`, `report.py`, and the
   CLI: `whetstone runs list|show|reindex`, `whetstone report --run <id> --format html|text|json`.
   `eval run` now stores a record and prints its id.

Result: **195 tests** (from 105), `ruff` and `mypy --strict` clean. The off-ramp below is shipped —
`whetstone report --run <id>` writes a standalone HTML drill-down with no server involved.

### Phase 1 — Read-only console — ✅ **done**

1. ✅ `ui/app.py` factory, static mount, SPA fallback, OpenAPI export (`python -m
   whetstone.ui.openapi`). Unmatched `/api` paths 404 as JSON rather than falling through to
   `index.html`.
2. ✅ `deps.py` — Principal (local git identity / opt-in trusted proxy headers), Config, ReadOnly
   guard (§7). `whetstone ui` refuses a non-loopback bind without `--insecure-bind`.
3. ✅ Vite + React 19 + Tailwind v4 + Radix; TypeScript types generated from the OpenAPI schema.
4. ✅ `DiffView` with overlays keyed on new-file line numbers (selection lands in Phase 2).
5. ✅ Skills index (weakest first, sparklines), skill detail (guidance with per-rule provenance and
   **untested guidance** flags, cases, runs, metadata), case detail (diff + expectation regions +
   history), run detail drill-down (§10.5).
6. ✅ `whetstone ui`, with `--dev` for the Vite dev server.

**Two bugs the generated types caught**, both pre-existing and invisible from Python:

- `SkillScore`'s metrics were plain `@property`, so `recall`/`fp_rate`/`precision` **never
  serialized**. Every consumer — `eval run --json`, the HTTP API, anything piping to `jq` — received
  raw confusion counts and had to reimplement the denominator conventions. Now `computed_field`.
- `meta.yaml`'s `owner` and `provenance` were documented but silently dropped by the loader, so
  rule-level provenance had nowhere to come from.

Also fixed during visual verification: Tailwind v4 hoists `@theme` out of `@media`, so the dark
palette was being emitted unconditionally and light mode never rendered.

### Phase 2 — Triage queue — ✅ **done**

1. ✅ `promote.py` — `CaseEdits` → rendered `case.yaml` + `change.diff`, validated by round-tripping
   through `load_skill` before anything is written, plus a check that the expectation points at a
   file the diff actually changes.
2. ✅ `candidates.py` — the queue, strongest signal first, with promote/reject decisions recorded
   per candidate (rejections carry a required reason).
3. ✅ Drag-to-select on the diff gutter, producing a range in new-file coordinates.
4. ✅ Triage screen (§10.3): three panes, `j`/`k`/`a`/`x`/`Enter`, raw comment beside the editable
   semantic with an **unedited** badge until it is rewritten.
5. ✅ Promotion writes cases to `skills/<id>/promoted_cases/` on disk (originally batch branches
   `whetstone/cases/batch-N` + **Propose N cases**, retired for the disk / graduate model).
6. ✅ *(follow-up)* The review conversation, signal badges, and a signal filter — see §10.3. The
   first cut showed the diff and the builder's verdict but not the evidence between them, which is
   how a screen for learning from review ends up looking like a list of unexplained code changes.

**Two deliberate deviations from the sketch:**

- Batch identity is **derived from git, not stored**. A branch with a remote-tracking ref has been
  pushed, so the next promotion starts the following number. Remote-tracking refs are local, so this
  never touches the network and there is no state file to desynchronise.
- `POST /api/git/propose` **pushes and says so** rather than opening a merge request. `WriteConnector`
  is defined but unimplemented in M1 (§9.3), and faking the last step would be worse than naming it.

A **preview** route was added beyond the plan: it validates edits and returns exactly what would be
committed, without writing. It is what lets a bad region or path be reported against the field that
caused it while the person is still editing.

### Phase 3 — Authoring — ✅ **done, except the case editor**

1. ✅ `authoring.py` — frontmatter round-trip, order-preserving, version bump. `prepare_guidance` /
   `prepare_meta` render, validate through `load_skill`, and report the resulting `skill_hash`.
2. ⬜ Case editor (§10.4). **Not built.** Triage already renders a candidate into a case; authoring
   one from nothing is the same form without a source MR, and it was the piece of this phase that
   nothing else depends on.
3. ✅ Guidance editor with pinned case list (§10.2) — textarea beside a live preview, the eval
   cases that constrain the rule listed underneath.
4. ✅ **Gate-before-propose enforcement** (C6): `GET /api/skills/{id}/proposal`, disabled-button
   reason strings, and the same check at `POST /api/git/propose`.
5. ✅ Optimistic concurrency end-to-end: a stale `expect_head` is a 409, rendered as state — what
   the branch holds, what this tab expected, and an explicit "load what is on the branch".

**Four deliberate deviations from the sketch:**

- **The gate is not launched from the console.** §10.2 called for a one-click gate; running one
  needs the job orchestration that is Phase 4. Rather than fake it, the panel prints the exact
  `whetstone eval gate` invocation for the staged branch. A button that silently blocks for four
  minutes would be worse than a command someone can read.
- **C6 is enforced at the push, not only in the editor.** §10.2's *Open in editor* escape hatch
  means the branch can receive commits the console never saw, so the check belongs at the one door
  everything goes through. `POST /api/git/propose` refuses any branch that changes what a skill
  would publish without a passing gate covering the result — including a branch made entirely
  outside the console. The question is deliberately *what would this publish*, not *did `SKILL.md`
  change*: deleting the one eval case a skill keeps failing raises its score without improving
  anything, and `skill_hash` covers the cases so that it counts. Adding cases is the one exemption,
  which is what keeps triage batches pushing freely.
- **Evidence is keyed on content, not on the branch.** A gate record stores the `skill_hash` of the
  skill *as committed*, and C6 matches on it. Typing one more character retracts the permission to
  publish. Keying on the branch instead would let a passing gate become a standing licence.
- **A practice-mode gate is not evidence.** Practice mode swaps in the pattern reviewer and the
  deterministic judge (C4), so its verdict is about a regex. Accepting it would let a demo mode
  wave the whole rule through.

Editing `meta.yaml` is text-with-validation rather than a form: the provenance block is already
written structurally by triage (`promote.render_meta_yaml`), and a form covering the rest would
either duplicate that or fence in what an operator can express. Triggers stay in `SKILL.md`
frontmatter and are preserved verbatim across an edit — the editor does not expose them yet.

### Phase 3b — Live review adjudication — ✅ **done**

Unplanned, and the phasing is better for it: the loop the whole project describes was missing its
most direct link. Everything else infers the label; this asks for it.

1. ✅ `reviews.py` — `ReviewRecord` + `ReviewStore`, same plain-JSON shape as gates.
2. ✅ `service.record_review` — the reviewer over an arbitrary `CodeChange`, no judge, because there
   are no expectations to judge against.
3. ✅ `whetstone review --mr` / `--diff`, plus `GitLabConnector.get_merge_request` (the one
   connector method `list_reviewed_changes`'s `state=merged` filter could not provide).
4. ✅ `corpus.builder.candidate_from_finding` and two new `human_signal` values.
5. ✅ `/api/reviews` — ingest, list, detail, rule, undo — and the Reviews screens (§10.3b).
6. ✅ `POST /api/reviews` + `review --import`: a review run anywhere, posted here. `service.
   apply_ruling` is shared by the router and the CLI so a ruling cannot mint a candidate through one
   path and not the other.

**Deliberately reusing rather than extending:** a ruling writes into the *existing* candidates
directory, so promote (to `promoted_cases/` on disk) and C6 apply unchanged. The one new field on
`CandidateCase` is `suggested_rule_id`, which carries the rule that fired into *Evidence for rule* —
so a promoted case files its own provenance instead of relying on somebody remembering to.

**Not built:** launching a review from the console. That needs Phase 4's job orchestration, which is
why the screen begins with a record the CLI produced.

### Phase 4 — Run orchestration — 1 wk

1. `jobs.py` — worker pool, cancellation, SSE event bus.
2. Cost estimation (§9.5) + `ConfirmCost` dialog.
3. Live progress UI; run cancellation.
4. Practice-mode wiring through the whole stack (C4).
5. **Launch a review from the console** (§10.3b) — "review this MR" as a button rather than a
   command to paste, which is the last place the workflow hands you back to a terminal.

### Phase 5 — Compare & judge lab — 1 wk

1. Compare view (§10.6) with client-side tolerance recomputation.
2. Judge lab (§10.7) + labeled-set append on a branch.
3. Accuracy trend chart.

Cheap, because Phase 0 already captured the data.

### Phase 6 — Hardening — 1–1.5 wk

1. Asset build in CI; wheel packaging (§14).
2. Playwright E2E over the three critical flows, in practice mode.
3. Accessibility pass (keyboard traps, focus order, contrast, ARIA on the diff grid).
4. README + docs; `whetstone ui --help`.
5. `mypy --strict` and `ruff` clean over `src/whetstone/ui/`; `tsc --noEmit` + eslint clean.

**Total ≈ 9–11 weeks**, of which Phases 0–3 are done. Three gaps the plan was written around are now
closed: the run drill-down that did not exist (G1), the triage loop that had no tooling (G5), and
the one nothing in the plan called a gap — **the guidance itself was read-only**, so the pipeline
grew a skill's test suite while its rules could only be changed outside the tool.

**Phase 4 (run orchestration) is next**, and the guidance editor is what makes it urgent: C6 now
tells someone their change needs a gate, and the console can only hand them a command to run in a
terminal. Closing that is `jobs.py`, cost estimation, and the SSE progress stream.

**Off-ramp taken.** `whetstone report --run <id> --format html` ships as part of Phase 0 — one
self-contained file with the §10.5 drill-down, no server, no auth, drops straight into CI artifacts.
Use it before starting Phase 1 to measure how much of the demand a static report absorbs.

---

## 13. Testing

| Layer | Coverage |
|---|---|
| `tests/unit/` | `runs.py` round-trip; `gitio.py` against temp repos (incl. 409 drift, dirty-tree refusal); `evaluate_expectation` short-circuit parity; frontmatter round-trip preserving field order; cost estimator. |
| `tests/golden/` | **Unchanged and green** — the proof capture didn't move scores (C3). |
| `tests/contract/` | Unchanged. `WriteConnector` gains conformance coverage as `gitio` starts calling it. |
| `tests/api/` *(new)* | Every route via `httpx.ASGITransport` with `FakeLLMClient` and a temp git repo. No network, no model — same discipline as the rest of the suite. |
| `tests/e2e/` *(new, opt-in)* | Playwright over three flows: triage→promote→MR, author a case, run an eval and read the drill-down. Practice mode, so no spend. Gated like `tests/live/`. |

Quality gates unchanged: `pytest`, `ruff check .`, `mypy` (strict, `files = ["src"]` — `ui/` sits
inside `src/` and must satisfy it). Frontend adds `tsc --noEmit` and eslint.

---

## 14. Packaging & distribution

- `npm ci && npm run build` in CI emits `src/whetstone/ui/static/`; hatchling force-includes it in
  the wheel. `ui/static/` is gitignored — built, never committed.
- **Users need no Node.** `pip install whetstone[ui]` then `whetstone ui`.
- **Maintainers need Node.** From a source checkout without built assets, `whetstone ui` prints how
  to build, or `whetstone ui --dev` proxies to the Vite dev server with HMR.
- `whetstone ui` binds `127.0.0.1` and opens a browser. Binding a non-loopback host requires
  `--host` *and* an explicit acknowledgement flag, since there is no built-in authentication (§7).

---

## 15. Decision log

**D1 — React + TypeScript + Vite.** Chosen. The two screens justifying the console — diff region
selection in triage, and the run drill-down — are stateful and interaction-dense, exactly where
server-rendered HTML fights back. Cost accepted: maintainers own a Node toolchain; users don't.

**D2 — Local-first, with the auth seam built now.** No hand-rolled authentication ever. Loopback by
default; team deployment is an OIDC reverse proxy plus `trust_proxy_headers = true`. Building the
`Principal` dependency and read-only guard in Phase 1 costs about a day and makes D2 reversible.

**D3 — Configurable `skills_root`, defaulting to this repo.** Resolves the open question at
`milestone-1-eval-harness.md:449` without forcing the answer now: pointing at a company skills repo
is two lines of `whetstone.toml`. `gitio` targets whatever repo contains the root.

**D4 — Guidance editing is in, gated by C6.** Reverses the first draft. The objection — that a web
editor for prose in git duplicates GitLab — is answered by making the console *stricter* than
GitLab: no MR opens without a passing gate for that exact content hash. That is a capability GitLab
alone does not provide, and it turns the editor from a redundant surface into the enforcement point
for the project's founding claim.

**Risks accepted and how they're held:**

| Risk | Held by |
|---|---|
| Console becomes a second source of truth | C1; no skill-writing path that isn't `gitio`; runs explicitly derived (C2). |
| UI-triggered runs burn budget | Estimate + confirm, `max_llm_calls_per_run`, practice mode, per-run cost in every record. |
| Capture changes scores | C3 + `tests/golden/` unmodified; short-circuit preserved verbatim. |
| Concurrent edits clobber each other | Optimistic concurrency on commit sha; 409 with the competing diff; dirty-tree refusal. |
| Authoring UI worse than an editor | The edge is diff + region selection, not text editing; **Open in editor** escape hatch on both authoring paths. |
| Frontend toolchain maintenance | Accepted under D1. Generated API types keep drift a compile error rather than a runtime surprise. |
| Scope creep into monitoring | Out of scope (§2). The gate stays a CI exit code. |
