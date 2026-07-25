# Whetstone — Milestone 1: Eval / Backtest Harness (detailed plan)

**Goal of M1:** Prove we can *measure whether a skill change is a net improvement*, grounded in real
historical GitLab MRs — before we build anything that auto-edits skills. Everything downstream
(distillation, proposal engine) plugs into the gate this milestone produces.

**Scope for M1**
- Skill format spec (folders of `.md` in git) + a self-testing `eval_cases/` layout.
- Canonical domain model (provider-agnostic).
- Plugin interfaces + a **GitLab** implementation of `ReviewConnector` and `SourceConnector`.
- Corpus builder: historical MRs → candidate eval cases (human-curated into skills).
- The **eval harness**: run a reviewer with skill version V over a skill's eval set → scores.
- The **regression gate**: compare skill V_old vs V_new → pass/fail with tolerances.
- CLI-first, thin API. Full test suite (unit, fakes, contract/conformance, golden, meta-eval).

**Explicitly out of scope for M1:** distillation, proposal generation, wiki connectors,
webhooks, auto-merge, dashboards. (Designed for, not built.)

> **Amended.** A Jira connector *was* built, against this original scope line. The reasoning is
> ADR-006: recall is the harder half of the gate to evidence, review history only labels what a
> reviewer caught, and a shipped defect is the one kind of labelled *miss* available. That makes it
> a measurement-quality question, which is this milestone's subject. Wiki connectors remain out.

**Definition of done**
- `whetstone eval run --skill <id>` produces a reproducible score report from committed eval cases.
- `whetstone eval gate --skill <id> --base <ref> --candidate <ref>` returns a deterministic
  pass/fail suitable for CI on the skills repo.
- `whetstone corpus pull` turns real GitLab MR history into candidate eval cases offline.
- Core loop has **zero** GitLab imports; a `FakeProvider` runs the whole harness with no network.
- CI is green with LLM calls stubbed; a separate opt-in job exercises the live LLM path.

---

## 1. Stack recommendation (decision point)

**Recommended: Python** for the harness and connectors.
- Rationale: the eval/LLM-orchestration ecosystem (async LLM SDKs, pydantic, pytest, HTTP-record
  libs, analysis) is strongest here, and M1 is exploratory — iteration speed matters most.
- The plugin architecture is language-independent; if the org later standardizes the *core* on Rust,
  connectors stay swappable. M1 shouldn't pay that cost yet.

Concrete choices:
- CLI: `typer`. Models/validation: `pydantic v2`. Tests: `pytest`.
- HTTP: `httpx`; record/replay with `respx` (or VCR cassettes) so connector tests are hermetic.
- LLM: provider-abstracted client (see `LLMClient` below); default model per repo guidance.
- Packaging: plugins registered via Python entry points (`whetstone.providers`) → config-not-code onboarding.

> If the team prefers Rust for long-term ownership, the same interface shapes hold; swap pytest→cargo
> test and respx→wiremock/`mockito`. Flagging as a decision, not dictating.

---

## 2. Repo layout

```
whetstone/
  docs/
    milestone-1-eval-harness.md        # this file
  pyproject.toml
  src/whetstone/
    domain/                            # canonical model — NO provider code
      change.py  review.py  signal.py  skill.py  eval.py  finding.py
    core/
      loader.py                        # load skill folders → Skill objects
      harness.py                       # run eval set → SkillScore
      gate.py                          # V_old vs V_new → GateResult
      scoring.py                       # deterministic math (TP/FN/FP, F-beta)
      matching.py                      # Finding × Expectation → MatchResult
    reviewer/
      base.py                          # Reviewer interface
      llm_reviewer.py                  # LLM-backed reviewer (the thing under test)
      fake_reviewer.py                 # scripted findings for deterministic tests
    judge/
      base.py                          # Judge interface (semantic match)
      llm_judge.py
      deterministic_judge.py           # path/line/regex matcher, no LLM
    llm/
      client.py                        # LLMClient abstraction + retry/cache
      fake_client.py                   # canned completions keyed by prompt hash
    providers/
      base.py                          # capability interfaces (contracts)
      registry.py                      # discover/instantiate plugins from config
      fake/                            # in-memory provider for tests
      gitlab/                          # GitLab adapter (Review + Source)
    corpus/
      builder.py                       # MRs → candidate eval cases
    cli.py  api.py  config.py
  skills/                              # the skills repo (git-tracked; can be a submodule later)
    code-review-rust-error-handling/
    secrets-in-logs/
    ...
  tests/
    unit/  contract/  golden/  fixtures/  meta_eval/
```

`skills/` may be this repo's own folder for M1; in production it's the company's skills repo,
consumed read-only via the `SourceConnector`/git.

---

## 3. Skill format spec (folders of `.md`, self-testing)

```
skills/<skill-id>/
  SKILL.md                 # human-authored guidance (YAML frontmatter + body)
  meta.yaml                # machine metadata: owner, triggers, references, provenance
  eval_cases/
    <case-id>/
      case.yaml            # kind + expectations + provenance (source MR)
      change.diff          # unified diff under review  (or a ref-pointer, see below)
      context/             # optional: pre-change file snapshots the reviewer may read
```

**`SKILL.md` frontmatter**
```yaml
---
id: code-review-rust-error-handling
name: Rust error handling review
description: Flags panics/unwraps and swallowed errors in service code.
version: 3                       # bumped on any content change; git is source of truth
triggers:
  paths: ["**/*.rs"]
  labels: ["backend"]
owner: "@backend-guild"          # governs approval later
---
(body: the actual reviewer guidance / rules, each rule id-tagged for provenance)
- rule R1: `.unwrap()`/`.expect()` in non-test code must be justified or replaced with `?`.
- rule R2: `catch`-equivalent that discards an error without logging/propagating is a defect.
```

**`meta.yaml`**
```yaml
references:                        # resolvable, not copied — drift-checkable later
  - kind: code
    repo: gitlab:acme/payments
    path: src/error.rs
  - kind: wiki
    id: outline:eng-standards/error-handling
provenance:                        # which signals justified current rules (grows over time)
  R1: [{source: gitlab_mr, ref: "acme/payments!812#note_44"}]
```

**`eval_cases/<case-id>/case.yaml`**
```yaml
id: unwrap-in-handler
kind: should_catch                 # should_catch | should_not_flag
provenance:
  source: gitlab_mr
  ref: "acme/payments!812"
  human_signal: "reviewer requested change; suggestion applied"
change: change.diff                # relative file, or {repo, base_ref, head_ref} to fetch
expect:
  - id: e1
    must: appear                   # appear (recall) | not_appear (precision)
    where:
      path: src/handlers/charge.rs
      line_range: [40, 58]
    semantic: "unwrap on the DB result can panic on a normal error path"
    severity_min: warning
```
`should_not_flag` cases use `must: not_appear` — e.g. a diff humans approved clean, or a finding a
reviewer explicitly dismissed. These are what keep precision honest.

**Why store eval cases inside the skill:** skills become self-testing units; a change to `SKILL.md`
is gated by the cases that ship next to it; ownership and review stay colocated.

---

## 4. Canonical domain model (M1 subset)

Provider-free. All connectors normalize *into* these; the core only ever sees these.

```python
# domain/change.py
class FileChange(BaseModel):
    path: str
    old_path: str | None
    hunks: list[Hunk]              # parsed unified diff
class CodeChange(BaseModel):
    repo: RepoRef                  # provider-neutral: "gitlab:acme/payments"
    base_ref: str; head_ref: str
    files: list[FileChange]
    def added_lines(self, path) -> list[LineRef]: ...

# domain/review.py
class ReviewComment(BaseModel):
    author: str; body: str; path: str | None; line: int | None; created_at: datetime
class Suggestion(BaseModel):
    path: str; line_range: tuple[int,int]; proposed: str
    applied: bool                  # GitLab tells us this — high-signal label
class ReviewThread(BaseModel):
    comments: list[ReviewComment]; resolved: bool; suggestion: Suggestion | None

# domain/finding.py — output of a reviewer running a skill
class Finding(BaseModel):
    skill_id: str; rule_id: str | None
    path: str; line: int | None
    severity: Severity             # info|warning|error
    message: str

# domain/eval.py
class Expectation(BaseModel):
    id: str; must: Literal["appear","not_appear"]
    where: Region                  # path + optional line_range
    semantic: str; severity_min: Severity | None
class EvalCase(BaseModel):
    id: str; kind: Literal["should_catch","should_not_flag"]
    change: CodeChange; expect: list[Expectation]; provenance: Provenance
class Skill(BaseModel):
    id: str; version: int; body: str; triggers: Triggers
    references: list[Reference]; eval_cases: list[EvalCase]

# domain/signal.py — defined now, used by later milestones
class Signal(BaseModel):
    source: str; kind: str; durability: float; refs: list[str]
```

---

## 5. Plugin interfaces (contracts) + registry

Capability-split so a provider implements only what it can. Core depends on these ABCs, never on
`providers/gitlab`.

```python
# providers/base.py
class SourceConnector(Protocol):
    async def get_file(self, repo: RepoRef, ref: str, path: str) -> FileBlob | None: ...
    async def get_change(self, repo: RepoRef, base: str, head: str) -> CodeChange: ...
    def capabilities(self) -> set[Capability]: ...

class ReviewConnector(Protocol):
    async def list_reviewed_changes(self, repo: RepoRef, since: datetime) -> AsyncIterator[MergeRequestRef]: ...
    async def get_review(self, mr: MergeRequestRef) -> tuple[CodeChange, list[ReviewThread]]: ...

class WriteConnector(Protocol):        # not exercised in M1, interface only
    async def open_change_request(self, repo: RepoRef, branch: str, title: str, body: str) -> str: ...
```

Plugin responsibilities (owned inside the adapter, invisible to core): **auth, pagination,
rate-limit/backoff, retry, idempotency keys, payload→canonical normalization, capability manifest.**

Registry: providers register via entry point `whetstone.providers`; instantiated from config:
```yaml
# config.yaml
providers:
  gitlab:
    kind: gitlab
    base_url: https://gitlab.acme.com
    token_env: GITLAB_TOKEN
    projects: ["acme/payments", "acme/gateway"]
```
Adding GitHub later = a new package implementing the same Protocols + a config block. No core change.

---

## 6. GitLab adapter (M1)

Implements `SourceConnector` + `ReviewConnector` against GitLab API v4.
- `list_reviewed_changes`: MRs (merged, in window) that have discussion notes / suggestions.
- `get_review`: MR diff → `CodeChange`; discussions → `ReviewThread[]`; map GitLab `suggestion`
  applied/not-applied → `Suggestion.applied` (**the clean accept/reject label — weight it heavily**).
- `get_file`/`get_change`: repository files at a ref (for building the change + reviewer context).
- Hermetic tests: all HTTP recorded via `respx` cassettes in `tests/fixtures/gitlab/`.

---

## 7. Corpus builder (history → candidate eval cases)

`corpus/builder.py`: for each reviewed MR, emit **candidate** cases the human then curates into a
skill's `eval_cases/`.
- A thread whose suggestion was **applied**, or that was resolved after a change → `should_catch`
  candidate (the human caught a real issue). Expectation region = the commented lines.
- A merged-clean diff, or a comment marked *won't fix / not an issue* → `should_not_flag` candidate.
- Auto-classify likely skill by `triggers` (path/label) — human confirms.
- Output: `candidates/<mr>/…` mirroring the `eval_cases` layout, plus a `provenance` block linking
  back to the MR/note. **Nothing enters a skill without human labeling** — corpus builder proposes,
  a person promotes (`whetstone corpus promote`).

This keeps signal quality high (recall-worthy, low-noise) and gives every eval case a citation.

---

## 8. The eval harness (core deliverable)

**Under test:** `Reviewer.review(skill, change) -> list[Finding]`.
**Judge:** `Judge.match(finding, expectation) -> Match(matched: bool, confidence: float)`.

Flow per eval case:
1. Run reviewer with skill V over `case.change` → findings (repeat K trials; LLMs are nondeterministic).
2. For each `Expectation`, deterministic pre-filter (path/line region) narrows candidate findings,
   then `Judge` does semantic match (does a finding describe the same underlying issue?).
3. Classify:
   - `should_catch` / `must: appear` → **TP** if matched in a trial, else **FN**.
   - `should_not_flag` / `must: not_appear` → **FP** if a matching finding appears, else **TN**.

**Scoring (`core/scoring.py`, pure/deterministic):**
- Recall = TP / (TP+FN); FP-rate = FP / (FP+TN); Precision on flagged set.
- Composite = F_beta (β configurable; default β>1 to favor recall for review skills).
- Across K trials report **mean, variance, and pass@k / consistency** — not a single point number.

**Determinism controls (so the gate is trustworthy):**
- Pin model + temperature (low), fixed seeds where supported; cache completions by prompt hash.
- K trials + tolerance bands rather than exact equality.
- The **judge is itself validated** — see meta-eval (§10) — before its verdicts are trusted.

```python
# core/harness.py (shape)
async def run_skill(skill, reviewer, judge, k=5) -> SkillScore:
    per_case = []
    for case in skill.eval_cases:
        trials = [await reviewer.review(skill, case.change) for _ in range(k)]
        per_case.append(score_case(case, trials, judge))   # scoring.py, deterministic given matches
    return aggregate(per_case)
```

---

## 9. The regression gate (what M1 ultimately ships)

```python
# core/gate.py
def gate(old: SkillScore, new: SkillScore, cfg: GateConfig) -> GateResult:
    # PASS requires ALL:
    #  - recall_new    >= recall_old    - cfg.recall_tol
    #  - fp_rate_new   <= fp_rate_old   + cfg.fp_tol
    #  - no committed case flips catch→miss beyond cfg.per_case_tol
    #  - if targeted cases given: they must improve (the change must earn its keep)
    ...
```
- `whetstone eval gate --skill X --base <git-ref> --candidate <git-ref>`: loads skill at both refs,
  runs both over the *union* of committed eval cases, applies `gate`. Deterministic pass/fail → CI.
- This is the seam every later milestone plugs into: the proposal engine's output must pass this gate.

---

## 10. Test strategy (the harness is software — test it hard)

**a. Unit (no LLM, no network) — the bulk.**
- `scoring.py`: TP/FN/FP math, F-beta, aggregation — table-driven, exhaustive edge cases.
- `matching.py` with `DeterministicJudge`: region overlap, severity thresholds.
- `gate.py`: every pass/fail branch, tolerance boundaries.
- diff parsing, skill loader (frontmatter, malformed skills, missing eval cases).

**b. Fakes for the two nondeterministic edges.**
- `FakeReviewer`: returns scripted `Finding[]` per (skill, case) → lets us assert exact scores/gate
  outcomes with zero LLM. This makes the *entire harness* deterministically testable.
- `FakeLLMClient`: canned completions keyed by prompt hash → exercises `LLMReviewer`/`LLMJudge`
  code paths (prompt assembly, parsing, retry) without real calls.

**c. Contract / conformance suite (critical for plugin architecture).**
- `tests/contract/provider_conformance.py`: a single parametrized suite every `ReviewConnector` /
  `SourceConnector` must pass — pagination, empty results, rate-limit retry, normalization shape,
  idempotency. Run it against **both** `FakeProvider` and the GitLab adapter (GitLab via cassettes).
  When GitHub arrives, it must pass the *same* suite. This is how "plugin-ready" is enforced, not hoped.

**d. GitLab adapter integration (hermetic).**
- `respx` cassettes of real GitLab payloads → assert correct `CodeChange`/`ReviewThread`/`Suggestion`
  (esp. `applied` mapping). Recording script committed; refreshable.

**e. Golden tests (end-to-end, deterministic).**
- Fixed mock skills + committed eval cases + `FakeReviewer` → asserted `SkillScore` snapshot and
  `GateResult`. Guards against silent scoring drift when we refactor.

**f. Meta-eval (validate the LLM judge itself).**
- `tests/meta_eval/`: a small human-labeled set of (finding, expectation, is_match) pairs. Measure
  the `LLMJudge`'s agreement with humans; fail CI if judge accuracy drops below a floor. An unvalidated
  judge silently corrupts every score — this is the guardrail.

**g. CI shape.**
- Default job: everything above with LLM + network stubbed → fast, deterministic, required.
- Opt-in `live-llm` job: runs `LLMReviewer`/`LLMJudge` against the real model on the golden skills;
  tolerance-banded (not exact) to absorb nondeterminism; informational/nightly, not blocking.

---

## 11. Mock skills (ship these as fixtures + working examples)

### `skills/code-review-rust-error-handling/`
`SKILL.md` (frontmatter as §3) with rules R1 (`unwrap`/`expect` in non-test code) and R2 (swallowed
errors). Eval cases:
- `eval_cases/unwrap-in-handler/` → **should_catch**. `change.diff` adds `let row = db.get(id).unwrap();`
  in a request handler; expectation `appear` at those lines, severity ≥ warning. Provenance: a real
  (or synthetic-for-fixture) MR where a reviewer flagged exactly this.
- `eval_cases/unwrap-in-test/` → **should_not_flag**. Same `unwrap` but inside `#[cfg(test)]`;
  expectation `not_appear`. Guards precision (the rule must not fire in tests).
- `eval_cases/error-mapped-with-question-mark/` → **should_not_flag**. Diff uses `?` + a mapped error;
  reviewer must stay quiet.

### `skills/secrets-in-logs/`
Rule: no logging of tokens/passwords/PII. Cases:
- `eval_cases/token-in-info-log/` → **should_catch**: `log::info!("auth {}", token)`.
- `eval_cases/redacted-log/` → **should_not_flag**: logs `redact(token)`.
- `eval_cases/variable-named-token-but-safe/` → **should_not_flag**: a `token` count/length logged,
  not the value — tests semantic judgment, not keyword matching.

### `skills/mock-noop/` (test-only)
A trivial skill with a `FakeReviewer` mapping → used by golden tests to pin exact scores/gate math
independent of any model. Lives under `tests/fixtures/skills/`, not shipped.

These doubles as (1) documentation of the format, (2) golden-test inputs, (3) the first real skills the
company can adopt.

---

## 12. CLI & thin API surface (M1)

CLI (primary):
```
whetstone corpus pull   --provider gitlab --project acme/payments --since 2026-01-01
whetstone corpus promote --candidate <id> --skill <skill-id>        # human curation step
whetstone eval run      --skill code-review-rust-error-handling [--model ... --trials 5]
whetstone eval gate     --skill <id> --base <git-ref> --candidate <git-ref>
whetstone eval report   --run <id> --format html|json
whetstone providers list                                            # show discovered plugins + caps
```
API (thin wrapper, for later automation):
```
POST /eval/run     {skill, model?, trials?}      -> run_id
GET  /eval/runs/{id}                             -> SkillScore + report
POST /eval/gate    {skill, base_ref, candidate}  -> GateResult
```
CLI-first; the API just exposes the same core functions so the future proposal engine and CI can call in.

---

## 13. Work breakdown (suggested sequencing)

1. **Domain model + skill loader** (+ unit tests). No I/O. Establishes canonical types.
2. **Scoring + matching + gate** with `DeterministicJudge` (+ exhaustive unit tests). Pure logic first.
3. **FakeReviewer + FakeProvider + golden tests.** Whole harness runs deterministically end-to-end.
4. **Provider contract suite**; make FakeProvider pass it.
5. **GitLab adapter** (Source + Review) against the contract suite + cassette integration tests.
6. **Corpus builder** (candidates + promote flow) over cassette MRs.
7. **LLMClient + LLMReviewer + LLMJudge** behind `FakeLLMClient`; wire real model via opt-in job.
8. **Meta-eval** set + judge-validation gate.
9. **CLI + thin API.** Ship the two mock skills as real, populated skills.

Steps 1–4 deliver a *working, testable harness with no external dependency*; 5–6 ground it in real
GitLab history; 7–8 turn on the real reviewer/judge with guardrails.

---

## 14. Risks & open questions

- **Judge trust.** If the LLM judge is weak, every score is noise. Mitigated by meta-eval gate (§10f);
  start with a tight deterministic pre-filter + narrow semantic judging.
- **Corpus bias.** History over-represents what humans *did* comment on; silent misses aren't labeled.
  ~~Accept for M1~~ — **addressed**: tracker defects are labelled misses (ADR-006), and applied
  suggestions now also yield their accepted fix as a confirmed negative (ADR-007). What remains is
  the clean-merge inference, which is now reported rather than averaged in silently.
- **Nondeterminism vs a binary gate.** Handled via K-trials + tolerance bands + caching; document the
  bands so a "fail" is real, not variance.
- **GitLab suggestion coverage.** Not all valuable feedback is a structured suggestion; free-text
  threads need classification. Weight structured `applied` signal first, expand later.
- **Open:** target model + temperature for the reviewer; is `skills/` this repo or a submodule of the
  company skills repo for M1; how large a seed corpus (# MRs) to hand-label for a credible first gate.

**Decisions needed from you:** (a) Python vs Rust for the harness; (b) which 1–2 GitLab projects to
seed the corpus from; (c) rough count of hand-labeled eval cases you can commit to for the first gate
(I'd aim for ~15–25 across 2–3 skills to make the gate meaningful).
