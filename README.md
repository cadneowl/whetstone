# Whetstone

A system for keeping a company's agent **skills** (code review, arch review, secret-scanning, …)
continuously sharp. It learns from GitLab merge-request reviews, from the defects your tracker says
shipped anyway, and from the codebase — and, critically, **never ships a skill change it can't prove
is a net improvement**, because every change passes an evaluated regression gate first.

> **The thesis:** most AI review tools are stateless — they review each PR fresh. Whetstone treats
> the *skill* as the durable, versioned knowledge artifact and turns human review signals into
> measurable improvements to it. The output isn't a review; it's a *better reviewer for next time*,
> tool-agnostic and governed.

This repository is **Milestone 1: the eval / backtest harness + regression gate** — the measurement
substrate everything else (distillation, proposal generation, the control-plane API) plugs into.
See [`docs/milestone-1-eval-harness.md`](docs/milestone-1-eval-harness.md) for the design and
[`docs/decisions.md`](docs/decisions.md) for the architecture decisions.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Architecture](#architecture)
3. [Install & setup](#install--setup)
4. [The skill format](#the-skill-format)
5. [Eval cases](#eval-cases)
6. [Scoring model](#scoring-model)
7. [The regression gate](#the-regression-gate)
8. [CLI reference](#cli-reference)
9. [Run records & reports](#run-records--reports)
10. [The console (`whetstone ui`)](#the-console-whetstone-ui)
11. [Configuration (`whetstone.toml`)](#configuration-whetstonetoml)
12. [Programmatic API (`whetstone.service`)](#programmatic-api-whetstoneservice)
13. [Providers & the plugin architecture](#providers--the-plugin-architecture)
14. [The corpus builder](#the-corpus-builder)
15. [The LLM layer](#the-llm-layer)
16. [Reviewers & judges](#reviewers--judges)
17. [Meta-evaluation (validating the judge)](#meta-evaluation-validating-the-judge)
18. [Testing](#testing)
19. [Extending Whetstone](#extending-whetstone)
20. [Environment variables](#environment-variables)
21. [Repository layout](#repository-layout)

---

## How it works

A skill is scored by **replaying eval cases** through an LLM reviewer running that skill's guidance,
then checking the reviewer's findings against what each case expects:

```
  skill (SKILL.md guidance + eval_cases/)
        │
        ▼
  LLMReviewer(skill, change) ─────────▶ findings
        │                                  │
        │        LLMJudge(finding, expectation) ── semantic match?
        ▼                                  ▼
  per-case Confusion (TP/FN/FP/TN) ──▶ SkillScore (recall, fp_rate, precision, F2)
        │
        ▼
  gate(old_score, new_score) ──▶ PASS / FAIL   ← the CI seam
```

- **`should_catch` cases** measure **recall**: given a change with a known issue, does the reviewer
  surface it?
- **`should_not_flag` cases** measure **precision**: given a change humans approved, does the
  reviewer stay quiet?
- The **gate** compares a baseline skill version against a candidate and fails if recall drops or
  false positives rise.

Everything is deterministically testable: the two nondeterministic edges (the reviewer and the
judge) both have `Fake` implementations, so the entire harness runs with **no model and no network**.

---

## Architecture

```
src/whetstone/
  domain/       Canonical, provider-agnostic model. Imports NO provider code.
                enums, refs, change (+diff parser/reverser), finding, eval_model, skill, review,
                issue, score, run
  core/         The harness. loader · matching · scoring · gate · harness
  reviewer/     Reviewer protocol + LLMReviewer + PatternReviewer (test double)
  judge/        Judge protocol + LLMJudge + DeterministicJudge (test double)
  llm/          LLMClient protocol + factory · AnthropicClient · OpenAICompatibleClient (local) · FakeLLMClient (test)
  providers/    Capability protocols + registry + gitlab/ + jira/ adapters + fake/ provider
  corpus/       Review + defect history → candidate eval cases (human-promoted)
                builder · linking (issue ↔ merge request) · model
  meta_eval/    Validate a judge against human-labeled pairs
  runs.py       Run-record persistence (JSON files + derived SQLite index)
  candidates.py The triage queue: pending candidates + recorded promote/reject decisions
  promote.py    Edited candidate → validated eval case (round-tripped through load_skill)
  report.py     Run record → self-contained HTML / text report
  gitio.py      Git write primitives (branch, commit, push) that never touch the working tree
  config.py     `whetstone.toml` loading
  service.py    Operable API layer (used by the CLI and the console)
  ui/           FastAPI console — app · deps (identity/authz) · routers · built SPA assets
  cli.py        `whetstone` command-line interface
skills/         The skill registry (folders of SKILL.md + meta.yaml + eval_cases/)
ui/             Console frontend source (React + Vite); builds into src/whetstone/ui/static/
tests/          unit · api · contract (provider conformance) · golden · live (opt-in) · fixtures
docs/           Milestone plan + decision record + console UI plan
```

The **plugin boundary**: the core loop depends only on capability `Protocol`s in
`providers/base.py`; it never imports GitLab code. Providers normalize *their* world into the
canonical `domain` model at the edges. A single **contract conformance suite** runs against every
provider, so adding GitHub later is a new adapter, not a core change.

---

## Install & setup

Requirements: **Python 3.13**, **[uv](https://docs.astral.sh/uv/)**, and (for live model calls)
Anthropic credentials.

```bash
uv sync --extra dev          # create the venv and install runtime + dev deps
uv run pytest                # 75 tests, deterministic, no network
uv run ruff check .          # lint
uv run mypy                  # strict type-check (src/)
```

The `whetstone` console script is installed into the environment:

```bash
uv run whetstone --help
```

(Everywhere below, prefix commands with `uv run` unless you've activated the venv.)

---

## The skill format

A skill is a **folder** under `skills/`. It is self-testing: the eval cases that gate a change to
its guidance live right next to that guidance.

```
skills/<skill-id>/
  SKILL.md              # YAML frontmatter + human-authored review guidance
  meta.yaml             # owner, references, provenance (machine metadata)
  eval_cases/
    <case-id>/
      case.yaml         # what this case asserts
      change.diff       # the code change under review (unified diff)
```

### `SKILL.md`

```markdown
---
id: code-review-rust-error-handling
name: Rust error handling review
description: Flags panics/unwraps and swallowed errors in non-test service code.
version: 1
triggers:
  paths: ["**/*.rs"]
  labels: ["backend"]
---

# Rust error handling review

- **R1 — no unchecked panics in service code.** `.unwrap()` / `.expect()` outside tests must be
  replaced with `?` and a mapped error, or justified in a comment.
- **R2 — no swallowed errors.** ...
```

Frontmatter fields (all optional except `id`, which defaults to the folder name):

| Field | Meaning |
|---|---|
| `id` | Stable identifier. Defaults to the folder name. |
| `name` / `description` | Human labels; `name` is used in the reviewer prompt. |
| `version` | Integer, bumped on any content change. Git is the source of truth. |
| `triggers.paths` | Glob patterns (`PurePosixPath.full_match`, so `**/*.rs` works) used to route eval cases to this skill. |
| `triggers.labels` | Merge-request labels the skill answers to — the fallback when the subject isn't visible in a file path (a `security` label over a `values.yaml`). Paths win when both match. |

The markdown **body** below the frontmatter is the actual guidance handed to the reviewer.

### `meta.yaml`

Machine metadata. `references` are resolvable pointers (drift-checkable later), not copied text;
`provenance` records which signals justified each rule — written by triage when a promotion cites a
rule, and read back by `rule_ids` / `untested_rules`.

```yaml
owner: "@backend-guild"
references:
  - kind: code
    repo: "gitlab:acme/payments"
    path: "src/error.rs"
  - kind: wiki
    id: "outline:eng-standards/error-handling"
provenance:
  R1:
    - source: gitlab_mr
      ref: "acme/payments!812#note_44"
```

### Loading skills programmatically

```python
from whetstone.core.loader import load_skill, load_skills

skill = load_skill("skills/code-review-rust-error-handling")   # one folder
skills = load_skills("skills")                                  # every folder under a root
```

---

## Eval cases

Each `eval_cases/<case-id>/case.yaml` describes one assertion about a change.

```yaml
id: unwrap-in-handler
kind: should_catch            # should_catch | should_not_flag
repo: "gitlab:acme/payments"  # optional; defaults to local:<skill-id>
base_ref: ""                  # optional
head_ref: ""                  # optional
change: change.diff           # relative path to the diff file (default: change.diff)
provenance:
  source: gitlab_mr
  ref: "acme/payments!812"
  human_signal: "reviewer requested change; suggestion applied"
expect:
  - id: e1
    must: appear              # appear (recall) | not_appear (precision)
    where:
      path: src/handlers/charge.rs
      line_range: [40, 45]    # optional; omit to match the whole file
    semantic: "unwrap on the DB result can panic on a normal error path"
    severity_min: warning     # optional: info | warning | error
    pattern: "unwrap"         # optional regex used only by the DeterministicJudge
```

**Case kinds**

| `kind` | Expectations use | Contributes to |
|---|---|---|
| `should_catch` | `must: appear` | recall (TP if surfaced, FN if missed) |
| `should_not_flag` | `must: not_appear` | precision (FP if wrongly flagged, TN if silent) |

**Expectation fields**

| Field | Meaning |
|---|---|
| `must` | `appear` or `not_appear`. |
| `where.path` | File the expectation is about. |
| `where.line_range` | Inclusive `[lo, hi]`. A finding must fall inside it to be eligible. Omit to accept anywhere in the file. |
| `semantic` | Natural-language description of the issue. Used by the **LLM judge** to decide a match. |
| `severity_min` | If set, findings below this severity are ineligible. |
| `pattern` | Optional regex. Used only by the **DeterministicJudge** (tests); the LLMJudge ignores it. |

`change.diff` is a standard unified diff. Whetstone parses it to recover **added lines with their
new-file line numbers**, which is how findings are matched to expectation line ranges.

---

## Scoring model

Findings are matched to expectations in two steps (`core/matching.py`):

1. **Structural prefilter** — a finding is eligible for an expectation only if it's on the same
   file, within the line range (if any), and meets `severity_min` (if any).
2. **Semantic judge** — among eligible findings, the `Judge` decides whether any actually describes
   the same underlying issue.

Each `(trial, expectation)` becomes one cell of a confusion matrix (`domain/score.py`):

| Expectation `must` | Judge matched | Outcome |
|---|---|---|
| `appear` | yes | **TP** |
| `appear` | no | **FN** |
| `not_appear` | yes | **FP** (falsely flagged) |
| `not_appear` | no | **TN** |

Metrics, with documented conventions so a gate "fail" is never a division artifact:

| Metric | Formula | Convention when denominator is 0 |
|---|---|---|
| `recall` | TP / (TP+FN) | `1.0` (nothing to catch) |
| `fp_rate` | FP / (FP+TN) | `0.0` (nothing to falsely flag) |
| `precision` | TP / (TP+FP) | `1.0` (nothing flagged) |
| `f_beta(β=2)` | weighted P/R, recall-favoring | `0.0` when P=R=0 |

A `SkillScore` also exposes **per-trial stdev** of recall and fp_rate. Deterministic reviewers use
`k=1`; the LLM reviewer uses `k>1` (multiple trials) so you can see variance and gate on tolerance
bands rather than a single point estimate.

```python
from whetstone.core.harness import run_skill
score = run_skill(skill, reviewer, judge, k=5)
score.recall, score.fp_rate, score.precision, score.f_beta(), score.recall_stdev
```

---

## The regression gate

`gate(old_score, new_score, cfg)` in `core/gate.py` compares a baseline skill version against a
candidate. It **PASSes only if all guards hold**:

- `recall_new >= recall_old - recall_tol`
- `fp_rate_new <= fp_rate_old + fp_tol`
- no committed case that *passed* under the baseline may start *failing* (beyond
  `max_case_regressions`)
- every case named in `targeted_cases` passes under the candidate

`GateConfig` (all defaults are strict):

| Field | Default | Meaning |
|---|---|---|
| `recall_tol` | `0.0` | Allowed recall drop. |
| `fp_tol` | `0.0` | Allowed false-positive-rate rise. |
| `max_case_regressions` | `0` | How many previously-passing cases may regress. |
| `case_recall_floor` | `0.999` | A case "passes" if its recall ≥ this. |
| `case_fp_ceiling` | `0.001` | …and its fp_rate ≤ this. |
| `targeted_cases` | `[]` | Cases this change claims to fix; each must pass. |

`GateResult` reports `passed`, `reasons` (human-readable failure list), `regressed_cases`,
`fixed_cases` / `unfixed_cases`, and the before/after recall and fp_rate. This is the seam the CI job
and every future proposal engine call.

### Both sides answer the same questions

`service.gate_skills` scores the baseline and the candidate over the **union** of their eval cases,
so the only thing that differs between the two runs is the guidance. This matters most for the case
the corpus loop is built to produce: one that documents an issue the reviewer currently *misses*.
Scored over its own case set, the candidate's pooled recall would drop against a baseline that never
had to answer the new case, and the gate would read that as a regression — rejecting exactly the
change `corpus pull` exists to propose. Under union scoring both sides miss it equally, so promoting
the case is neutral, and fixing it later is an improvement the gate can see.

### Making a change earn its keep

Left alone the gate only ever asks *did anything break?*, so a guidance edit that does nothing at all
passes. Name the cases a change is supposed to fix and it has to deliver them:

```bash
whetstone eval gate --base-ref main --candidate-ref my-branch \
  --repo . --skill-path skills/code-review-rust-error-handling \
  --targeted unwrap-in-handler --targeted swallowed-error
```

Each targeted case must **pass** under the candidate; naming a case that isn't in the eval set is a
failure rather than a silently satisfied requirement. `fixed_cases` reports the ones that were
failing before, so a PASS says what the change actually bought.

`whetstone eval gate` takes `recall_tol` and `fp_tol` from `[gate]` in `whetstone.toml`, so a repo
sets its tolerance once instead of repeating flags in every CI invocation. `--recall-tol` /
`--fp-tol` override the file. `--targeted` is deliberately *not* read from config — which cases a
change must fix is a property of that change, not a repo-wide default.

---

## CLI reference

Top-level groups: `eval`, `corpus`, `skills`, `providers`, `llm`, `runs` — plus `report`.

```bash
whetstone --help
whetstone <group> --help
```

### `whetstone eval run`

Run a skill's eval set through the LLM reviewer + judge and print the score. Calls a model — Anthropic
by default, or any local/OpenAI-compatible backend via `--llm` (see [The LLM layer](#the-llm-layer)).

| Option | Default | Meaning |
|---|---|---|
| `--skill PATH` | *(required)* | Skill folder to score. |
| `--llm TEXT` | `anthropic` | Backend preset: `anthropic`, `openai`, `ollama`, `lmstudio`, `vllm`, `llamacpp`, `custom` (env `WHETSTONE_LLM`). Any other name + `--base-url` = a custom harness. |
| `--model TEXT` | preset default | Model id (env `WHETSTONE_LLM_MODEL`). Required for local/OpenAI backends. |
| `--base-url TEXT` | preset default | OpenAI-compatible endpoint (env `WHETSTONE_LLM_BASE_URL`) — point at a remote box. |
| `--api-key-env TEXT` | preset default | Name of the env var holding the API key, if the server needs one. |
| `--effort TEXT` | `high` | Reviewer effort: `low`/`medium`/`high`/`xhigh`/`max` (Anthropic only). |
| `--trials INT` | `1` | Trials per eval case (≥1); >1 surfaces variance. |
| `--workers INT` | `1` | Evaluate this many cases concurrently. |
| `--save / --no-save` | on | Store a run record for later inspection (see [Run records](#run-records--reports)). |
| `--runs-dir PATH` | config | Where run records are stored. |
| `--dry-run` | off | Validate & summarize the skill; **no model call**, no credentials. |
| `--json` | off | Emit the full `SkillScore` as JSON instead of a summary. |

```bash
whetstone eval run --skill skills/code-review-rust-error-handling --trials 5
```

```
Skill code-review-rust-error-handling v1  (k=5)
  recall 1.000   fp_rate 0.000   precision 1.000   F2 1.000
  stdev: recall 0.000  fp_rate 0.000
  cases:
    [catch ] unwrap-in-handler              recall 1.00
    [noflag] unwrap-in-test                 fp_rate 0.00
    [noflag] error-mapped-question-mark     fp_rate 0.00

run 20260725T040013Z-code-review-rust-error-handling-349fb3  (12 llm calls, 8.4s)
  whetstone report --run 20260725T040013Z-code-review-rust-error-handling-349fb3
```

### `whetstone eval gate`

Compare a candidate skill folder against a baseline. **Exits non-zero if the candidate regresses** —
drop it into CI on the skills repo.

Each side is a skill folder (`--base`/`--candidate`) **or** a git ref (`--base-ref`/`--candidate-ref`
with `--repo`/`--skill-path`) — e.g. gate a branch against `main`. Backend selection matches
`eval run`.

| Option | Default | Meaning |
|---|---|---|
| `--base PATH` | — | Baseline skill folder. |
| `--candidate PATH` | — | Candidate skill folder. |
| `--base-ref TEXT` / `--candidate-ref TEXT` | — | Git refs to gate instead of folders (needs `--repo`/`--skill-path`). |
| `--repo PATH` / `--skill-path TEXT` | `.` / — | Repo and skill path within it, for the `--*-ref` modes. |
| `--llm` / `--model` / `--base-url` / `--api-key-env` | preset | Backend selection — same as `eval run`. |
| `--trials INT` | `1` | Trials per case. |
| `--recall-tol FLOAT` | `0.0` | Allowed recall drop. |
| `--fp-tol FLOAT` | `0.0` | Allowed false-positive-rate rise. |
| `--targeted TEXT` | *(none)* | Case id this change must fix; repeatable. Fails unless it passes. |
| `--dry-run` | off | Validate both sides; **no model call**. |
| `--json` | off | Emit the full `GateOutcome` as JSON. |

Both sides are scored over the union of their eval cases — see
[the regression gate](#the-regression-gate).

```bash
whetstone eval gate \
  --base   skills/code-review-rust-error-handling \
  --candidate /tmp/candidate-skill
echo "exit code: $?"   # 0 = PASS, 1 = FAIL
```

```
Gate: PASS
  recall  1.000 -> 1.000
  fp_rate 0.500 -> 0.000
```

### `whetstone corpus pull`

Walk a GitLab project's reviewed MRs into **candidate eval cases** for a human to review. Reads the
token from the environment variable named by `--token-env`.

| Option | Default | Meaning |
|---|---|---|
| `--base-url TEXT` | *(required)* | GitLab base URL, e.g. `https://gitlab.acme.com`. |
| `--project TEXT` | *(required)* | Project path, e.g. `acme/payments`. |
| `--since [%Y-%m-%d]` | *(required)* | Only MRs merged on/after this date. |
| `--out PATH` | *(required)* | Directory to write candidate folders into. |
| `--token-env TEXT` | `GITLAB_TOKEN` | Env var holding the GitLab token. |
| `--skills-root PATH` | *(none)* | Skills root; used to route each candidate to a skill by trigger globs and MR labels. |
| `--max-clean-files INT` | `5` | Cap on `should_not_flag` candidates sampled from one comment-free MR. |
| `--refresh` | off | Rewrite candidates already in the queue (never ones already decided). |
| `--jira-url TEXT` | *(none)* | Jira base URL. Enables the escaped-defect signal. |
| `--jira-project TEXT` | *(none)* | Jira project key, e.g. `PAY`. Required with `--jira-url`. |
| `--jira-email TEXT` | *(none)* | Cloud account email (Basic auth). Omit for a Server/DC bearer token. |
| `--jira-token-env TEXT` | `JIRA_TOKEN` | Env var holding the Jira token. |
| `--max-defect-files INT` | `3` | Cap on candidates sampled from one defect fix. |

```bash
export GITLAB_TOKEN=glpat-...
whetstone corpus pull \
  --base-url https://gitlab.acme.com \
  --project acme/payments \
  --since 2026-01-01 \
  --out ./candidates \
  --skills-root skills
```

With a tracker, so shipped defects become cases too:

```bash
export GITLAB_TOKEN=glpat-...  JIRA_TOKEN=...
whetstone corpus pull \
  --base-url https://gitlab.acme.com --project acme/payments \
  --jira-url https://acme.atlassian.net --jira-project PAY \
  --jira-email you@acme.com \
  --since 2026-01-01 --out ./candidates --skills-root skills
```

Each candidate is written to `./candidates/<id>/` as `case.yaml` + `change.diff` (ready to promote)
plus `candidate.json` (kind, confidence, suggested skill, rationale) for triage. Nothing enters a
skill automatically.

**Safe to re-run.** Ids are scoped by project (`acme-payments-812-t0`), so pulling several projects
into one directory can't collide, and overlapping `--since` windows — the normal way to run this —
skip what's already queued. A candidate somebody has already promoted or rejected is never rewritten,
not even with `--refresh`: reviving a rejected candidate as a fresh-looking one would quietly undo a
human decision. The summary line reports what was skipped rather than leaving it implicit.

### `whetstone corpus promote`

Copy a reviewed candidate into a skill's `eval_cases/`.

| Option | Default | Meaning |
|---|---|---|
| `--candidate PATH` | *(required)* | A candidate case folder produced by `corpus pull`. |
| `--skill PATH` | *(required)* | Target skill folder. |

```bash
whetstone corpus promote \
  --candidate ./candidates/812-t0 \
  --skill skills/code-review-rust-error-handling
```

### `whetstone skills list`

List skills under a root, their eval-case counts, and how well their precision cases are evidenced.

| Option | Default | Meaning |
|---|---|---|
| `--root PATH` | `skills` | Skills root folder. |

```bash
whetstone skills list --root skills
# code-review-rust-error-handling  v1  (3 eval cases)
# secrets-in-logs                  v4  (22 eval cases)
#     ⚠ 18 of 20 precision case(s) rest on nobody having commented
```

That warning is not cosmetic: `fp_rate` averages over every `should_not_flag` case, and one built
from a clean merge establishes only that nobody said anything. See
[precision evidence](#precision-evidence-that-isnt-just-silence).

### `whetstone providers list`

List registered provider plugins (no options).

```bash
whetstone providers list
# fake
# gitlab
# jira
```

### `whetstone llm list`

List the model-backend presets (the `--llm` values) and their default endpoints (no options).

```bash
whetstone llm list
# anthropic  Anthropic (cloud, default)        (SDK default)
# custom     Custom OpenAI-compatible endpoint (set --base-url)
# ollama     Ollama                            http://localhost:11434/v1
# lmstudio   LM Studio                         http://localhost:1234/v1
# ...
```

### `whetstone llm check`

Send **one tiny structured request** to confirm a backend is reachable and returns valid JSON —
the fastest way to validate a local model is wired up. Takes the same `--llm`/`--model`/`--base-url`/
`--api-key-env` options as `eval run`. Exits `0` on success, `1` with a `FAIL:` line on any error.

```bash
whetstone llm check --llm ollama --model qwen2.5-coder:7b
# OK: backend returned ok=True note='ready'
```

### `whetstone runs list` / `show` / `reindex`

Inspect stored run records.

| Command | Options | Purpose |
|---|---|---|
| `runs list` | `--skill`, `--limit` (20), `--runs-dir` | Recent runs, newest first. |
| `runs show <run-id>` | `--json`, `--runs-dir` | One run's summary, or the full record. |
| `runs reindex` | `--runs-dir` | Rebuild the index from the stored files. |

```bash
whetstone runs list --skill code-review-rust-error-handling
# 20260725T040013Z-...-349fb3  2026-07-25 04:00  code-review-rust-error-handling v1  recall 1.000  fp 0.000  k=3
```

A run whose `version` is shared by another run with *different* content is flagged
`⚠ version reused for different content` — the common failure of editing guidance without bumping
`version`, which would otherwise make two unlike runs look comparable.

### `whetstone report`

Render a stored run as a **self-contained HTML report** — one file, no external assets, no
JavaScript. Open it from disk, attach it to a CI job, or paste it into a merge request.

| Option | Default | Meaning |
|---|---|---|
| `--run TEXT` | *(required)* | Run id (see `runs list`). |
| `--format TEXT` | `html` | `html`, `text`, or `json`. |
| `--out PATH` | stdout | Write to a file instead. |
| `--runs-dir PATH` | config | Where run records are stored. |

```bash
whetstone report --run 20260725T040013Z-...-349fb3 --out report.html
```

---

## Run records & reports

`SkillScore` answers *what* a skill scored. A **run record** answers *why*: it keeps every finding
the reviewer produced and every verdict the judge returned, so a failing case can be diagnosed long
after the run.

Without it, a flaky case surfaces as `recall 0.60` and nothing else — and you cannot tell whether
the reviewer missed the issue, the judge ruled wrongly, or the expectation is badly worded. Those
have three different fixes.

```
▾ unwrap-in-handler   should_catch   recall 0.60 (3/5 trials)   ⚠ flaky
  ▾ Trial 3 — FN
    Expected: "unwrap on the DB result can panic on a normal error path"
              src/handlers/charge.rs lines 40–45   severity ≥ warning
    Reviewer findings (2):
      charge.rs:41  warning  "consider handling this error"   conf 0.4
        └ judge: NOT MATCHED — "the finding is generic and does not identify
                 the unwrap as the panic source"              conf 0.8
      charge.rs:88  info     "unused import"                  conf 0.9
        └ outside expectation region (lines 40–45) — not judged
```

**Recording is free.** Semantic matching short-circuits at the first match, and capture preserves
that exactly — a recorded run makes precisely the same model calls as an unrecorded one. There is
therefore no "fast path" that skips it.

**Records are derived artifacts, not source of truth.** They live in `.whetstone/runs/<id>.json`
(gitignored) with a disposable SQLite index beside them; deleting the directory costs history only.
Git remains canonical for skills.

Findings that matched *no* expectation are retained and surfaced separately — they are either
unlabeled true positives (worth promoting to a `should_catch` case) or noise (worth pinning with a
`should_not_flag` case).

```python
from whetstone.runs import RunStore
from whetstone.report import render_run_html

store = RunStore()
record = store.latest("code-review-rust-error-handling")
open("report.html", "w", encoding="utf-8").write(render_run_html(record))
```

---

## The console (`whetstone ui`)

A local web console for the two jobs the CLI does badly: **diagnosing why an eval case failed**, and
**turning review history into eval cases**. Everything else it does — browsing skills, reading run
history — the CLI can do too; these two it genuinely cannot.

It is a thin HTTP layer over `whetstone.service` plus a prebuilt single-page app. It holds no state
of its own: skills are read from disk on every request, runs come from `.whetstone/runs/`, and every
write it makes lands as a git commit on a branch.

**Contents:** [Prerequisites](#prerequisites) · [Install](#install) · [Starting it](#starting-it) ·
[Configuration](#console-configuration) · [First run](#first-run-a-five-minute-tour) ·
[Screens](#the-screens) · [Reading a failure](#reading-a-failure-the-run-drill-down) ·
[Triage](#triage-the-full-workflow) · [Security](#security-and-deployment) ·
[HTTP API](#http-api) · [Developing](#developing-the-console) ·
[Troubleshooting](#console-troubleshooting) · [Not built yet](#not-built-yet)

---

### Prerequisites

| | Needed for | Notes |
|---|---|---|
| Python 3.13 + `uv` | everything | Same as the CLI. |
| The `ui` extra | running the console | `fastapi` + `uvicorn`. Not installed by default. |
| `git` | triage, repo status | Read-only browsing works without a repo, degraded. |
| **Node 20+** | **only** rebuilding the frontend | Wheels ship built assets. End users never need it. |

You do **not** need model credentials to use the console. It never calls a model — runs are launched
by the CLI (`whetstone eval run`) and the console reads what they recorded.

---

### Install

**From a published wheel** — nothing to build:

```bash
pip install 'whetstone[ui]'
whetstone ui
```

**From a source checkout** — the built assets are gitignored, so build them once:

```bash
uv sync --extra dev          # includes the ui extra
cd ui && npm install && npm run build && cd ..
whetstone ui
```

`npm run build` type-checks, runs the frontend tests, and writes to
`src/whetstone/ui/static/`. It takes a few seconds and is only needed after a frontend change.

If you skip it, `whetstone ui` still starts and the API works — the browser shows a page explaining
what to run. That is deliberate: a missing frontend build should not look like a broken install.

---

### Starting it

```bash
whetstone ui                      # http://127.0.0.1:8787, opens a browser
whetstone ui --port 9000          # a different port
whetstone ui --read-only          # browse without any write affordances
whetstone ui --no-open            # do not launch a browser (scripts, remote shells)
```

| Option | Default | Meaning |
|---|---|---|
| `--host TEXT` | `127.0.0.1` | Bind address. Non-loopback requires `--insecure-bind`. |
| `--port INT` | `8787` | Bind port. |
| `--read-only` | off | Disable every mutating route, server-side. |
| `--open` / `--no-open` | `--open` | Open a browser on start. |
| `--dev` | off | Serve only the API, for the Vite dev server to proxy. See [Developing](#developing-the-console). |
| `--insecure-bind` | off | Acknowledge binding a publicly reachable address. |

On start it prints where it is serving from, so a misconfigured skills root is obvious immediately:

```
Whetstone console on http://127.0.0.1:8787
  skills   /home/costa/work/whetstone/skills
  runs     /home/costa/work/whetstone/.whetstone/runs
```

Stop it with `Ctrl-C`. Nothing is left running and nothing needs cleaning up.

---

### Console configuration

Every setting resolves **CLI flag → environment variable → `whetstone.toml` → default**.

```toml
[ui]
host = "127.0.0.1"
port = 8787
read_only = false
practice_mode = false            # reserved; see the note below
trust_proxy_headers = false      # must be true to accept identity headers

[skills]
root = "skills"                  # what the console browses
repo = "."                       # the git repo it commits into

[candidates]
dir = "candidates"               # the triage queue

[runs]
dir = ".whetstone/runs"          # where run records are read from
```

| Environment variable | Overrides |
|---|---|
| `WHETSTONE_UI_HOST` / `WHETSTONE_UI_PORT` | `[ui] host` / `port` |
| `WHETSTONE_READ_ONLY` | `[ui] read_only` (`1`/`true`/`yes`/`on`) |
| `WHETSTONE_PRACTICE_MODE` | `[ui] practice_mode` |
| `WHETSTONE_SKILLS_ROOT` / `WHETSTONE_SKILLS_REPO` | `[skills] root` / `repo` |
| `WHETSTONE_CANDIDATES_DIR` | `[candidates] dir` |
| `WHETSTONE_RUNS_DIR` | `[runs] dir` |

Relative paths in `whetstone.toml` resolve against the file's own directory; paths from environment
variables resolve against the current working directory, as environment variables conventionally do.

> **`practice_mode` is declared but inert.** It is reported to the UI and shown as a badge, but
> nothing consumes it yet, because the console does not launch runs — that is Phase 4. Setting it
> today changes only what the header displays.

**Pointing at a separate skills repo:**

```toml
[skills]
root = "../company-skills/skills"
repo = "../company-skills"
```

`root` must be *inside* `repo`, since promotions commit files by repo-relative path. If they are
unrelated directories the console says so with a 500 and names both — a misconfiguration, not
something a user request can fix.

---

### First run: a five-minute tour

From a fresh checkout, with no model credentials:

```bash
# 1. Build the frontend once.
cd ui && npm install && npm run build && cd ..

# 2. Start the console. The skills index will show one skill, "never evaluated".
whetstone ui
```

To get a run to look at, you need a model — Anthropic, or anything local:

```bash
# 3. Score the bundled skill. Any backend works; a local model is fine.
whetstone eval run --skill skills/code-review-rust-error-handling --trials 5 \
  --llm ollama --model qwen2.5-coder:7b

# 4. Refresh the console. The skill now has a score, a sparkline, and a run to open.
```

`--trials 5` is worth it for a first look: multiple trials are what make flakiness visible, and the
flaky cases are the interesting ones.

To try triage without a GitLab instance, point `[candidates] dir` at any directory containing
`corpus pull` output. With a real instance:

```bash
export GITLAB_TOKEN=glpat-...
whetstone corpus pull --base-url https://gitlab.acme.com --project acme/payments \
  --since 2026-01-01 --out candidates --skills-root skills
```

---

### The screens

| Route | Screen |
|---|---|
| `/` | Skills index |
| `/skills/<id>` | Skill detail — guidance, cases, runs, metadata |
| `/skills/<id>/cases/<case-id>` | Eval case — diff, expectations, history |
| `/triage` | Candidate queue |
| `/runs` | Run history |
| `/runs/<run-id>` | Run drill-down |

Every URL is deep-linkable. Paste a run link into a merge request and it opens where you left it.

#### Skills index

One row per skill, **weakest first** — the landing order answers "which of our skills is actually
weak?", which otherwise takes a CLI run per skill and eyeballing.

- **`8 catch / 5 noflag`** — the case split. A skill with no `should_not_flag` cases has nothing
  keeping its precision honest.
- **`R` / `FP`** — recall and false-positive rate from the most recent run.
- **Sparkline** — recall over recent runs, oldest to newest. Direction, not precision.
- **`version reused`** — another run shares this `skill_version` with different content, so the two
  are not comparable despite appearances. Almost always means guidance was edited without bumping
  `version` in frontmatter.
- **`never evaluated`** — no runs. These sort *after* scored skills: a measured F2 of 0 is a more
  urgent problem than an unknown.

#### Skill detail

**Guidance** renders `SKILL.md` and, under each rule, the review signals that justified it from
`meta.yaml` — `R1 ← acme/payments!812#note_44`. Rules the reviewer never cited in the latest run are
badged **untested guidance**: if no finding ever cites a rule, any cases guarding it passed without
exercising it, so they would pass whether or not the guidance works.

**Eval cases** lists each case with its kind, the file it concerns, its provenance, and how it fared
last run. A **flaky** badge means trials disagreed — unstable, as opposed to simply wrong.

**Runs** is this skill's history, newest first. **Metadata** shows owner, declared rules, trigger
labels, and references.

#### Case detail

The diff with expectation regions highlighted **by new-file line number** — the same coordinates
`Region.line_range` uses, so what you see is what the matcher sees. The sidebar shows each
expectation in full and how the case has scored across recent runs, with `⚠` on runs where trials
disagreed.

#### Runs

Every run, newest first: when, which skill and version, recall, fp rate, `k`, and the model. Badged
`version reused` and `practice` where applicable.

---

### Reading a failure: the run drill-down

This is the screen the console exists for. A flaky case shows up everywhere else as `recall 0.60`
and nothing more — which is indistinguishable between three different problems with three different
fixes. The drill-down separates them.

Expand a case, then a trial:

```
▾ unwrap-in-handler       should catch      FN FN TP FN TP    2/5   ⚠ flaky

  ▾ Trial 1 of 5                                                FN

    FN  must appear  (missed — the reviewer should have flagged this)
        “unwrap on the DB result can panic on a normal error path”
        src/handlers/charge.rs lines 40–45   severity ≥ warning

        No finding was eligible — nothing the reviewer reported reached the judge here.

        Filtered out before judging:
          src/handlers/charge.rs:41  info  "consider handling this error"  conf 0.35
            not judged — below the required severity
```

Read it top-down:

1. **The chips** (`FN FN TP FN TP`) are the per-trial outcome. Disagreement across trials is
   flakiness; uniform `FN` is a consistent miss.
2. **The quoted text** is what the expectation asserted, recorded with the run. It is copied, not
   referenced, so the record stays readable even after the skill is edited.
3. **The findings**, each with the judge's verdict, confidence, and one-sentence reason.
4. **Filtered out before judging** — findings the structural prefilter dropped, and why.

**Diagnosing from what you see:**

| What the trial shows | What went wrong | Where to fix it |
|---|---|---|
| No findings at all | The reviewer missed it entirely | The skill's guidance |
| A finding, `judge: NOT MATCHED` with a reason you disagree with | The judge ruled badly | The expectation's wording, or the judge (see meta-eval) |
| A finding filtered out — *below the required severity* | The reviewer found it but rated it lower than the case demands | `severity_min` on the case, or the guidance |
| A finding filtered out — *outside the expected line range* | The region is wrong, or the reviewer anchored elsewhere | The case's `line_range` |
| `Findings matching no expectation` | The reviewer said something nothing asserts | Promote it to a new case, or pin it with `should_not_flag` |

That last section is worth attention: those findings are either unlabelled true positives worth
capturing, or noise worth pinning. Either way they are free corpus growth.

**Standalone report ↗** in the header renders the same drill-down as a single self-contained HTML
file — no server, no assets — for attaching to a CI job or pasting into a merge request. It is the
same output as `whetstone report --run <id>`.

---

### Triage: the full workflow

`corpus pull` proposes candidate eval cases; a person decides which are real. This is the screen
that exists because the CLI genuinely could not do the job.

**Why it needs a UI at all.** `corpus/builder.py` sets a candidate's expectation to the **raw body of
the first review comment** — in real repositories that is "nit: use `?` here", "see above", "👍", or
a paragraph about something else. That text becomes the ground truth the LLM judge scores every
finding against. `whetstone corpus promote` is a verbatim `copyfile` and has no way to express a
correction, so the human step that must happen has nowhere to happen.

#### Filling the queue

```bash
whetstone corpus pull \
  --base-url https://gitlab.acme.com \
  --project acme/payments \
  --since 2026-01-01 \
  --out candidates \
  --skills-root skills          # routes each candidate to a skill by trigger globs
```

Then open **Triage**. The console reads `[candidates] dir` (default `candidates/`).

#### The three panes

```
┌────────────┬───────────────────────────────┬──────────────────────┐
│ QUEUE      │ DIFF                          │ EXPECTATION          │
│            │                               │                      │
│ ▸ 812-t0   │  src/handlers/charge.rs       │ kind  ◉catch ○noflag │
│   0.90     │                               │ skill [rust-errors▾] │
│   812-t1   │  40  fn charge(id: Id) {      │ case id [812-t0    ] │
│   0.50     │  41 +  let row = db.get(id)   │ region [41]–[43]     │
│   813-cl0  │  42 +      .unwrap();         │ severity [none    ▾] │
│   0.30     │  43    process(row);          │                      │
│            │                               │ ORIGINAL COMMENT     │
│ 3 pending  │  drag line numbers to select  │ "nit: use `?` here"  │
│ 1 promoted │                               │ ─────────────────────│
│ 1 rejected │                               │ SEMANTIC  [unedited] │
│            │                               │ [                  ] │
└────────────┴───────────────────────────────┴──────────────────────┘
   j/k move   a promote   x reject   Enter promote
```

**Queue** is ordered by the corpus builder's confidence — applied suggestions (0.9) before resolved
comments (0.5) before clean merges (0.3). That is the order attention is worth spending in.

**Diff** highlights the current region. Drag across the **line numbers** to change it.

**Expectation** is the form. The **original comment** and the editable **semantic** field sit side by
side, both visible, and the field is badged **unedited** until you change it. The job is to rewrite
the signal, not to accept it.

#### Step by step

1. **Pick a candidate** — `j`/`k`, or click.
2. **Check the kind.** `should catch` asserts the reviewer must flag this; `should not flag` asserts
   it must stay quiet. The expectation's `must` follows automatically — a `should_catch` case whose
   expectation says `not_appear` is incoherent, so the UI cannot express it.
3. **Confirm the target skill.** Auto-routed by trigger globs and MR labels; blank if nothing
   matched.
4. **Fix the region.** Drag on the diff, or type the line numbers. Clear either end for "whole file".
   This is the field most likely to be wrong in an auto-generated candidate.
5. **Rewrite the semantic.** Describe the issue as the judge should understand it — a standalone
   sentence, not a reply to a thread. *"unwrap on the DB result can panic on a normal error path"*,
   not *"nit: use `?` here"*.
6. **Optionally set a severity floor**, if findings below it should not count.
7. **Optionally cite a rule.** See below.
8. **Validate** (optional) to check without writing, or **Promote** to commit.

#### Evidence for rule

`meta.yaml` carries a `provenance` block mapping each rule id to the review signals that justified
it — the only record of *why* a piece of guidance exists, and the thing `rule_ids` and
`untested_rules` read to report on a skill's rule set. It was hand-maintained, so it drifted.

Put a rule id (`R1`, `SEC2` — uppercase, ending in a digit, matching how rules are tagged in
`SKILL.md`) in **Evidence for rule** and promoting the case files the source MR under that rule in
the same commit. The evidence lands with the case that demonstrates it rather than in a follow-up
nobody makes. Leave it empty when a case tests the skill without justifying any single rule; that is
common and perfectly fine.

Re-promoting two cases out of one MR won't cite it twice, and a second promotion in the same session
builds on the first — the metadata is read from the batch branch, not the working tree, so nothing
is dropped.

#### Keyboard

| Key | Action |
|---|---|
| `j` / `k` | Next / previous candidate |
| `a` or `Enter` | Promote |
| `x` | Open the reject form |

Shortcuts are suppressed while a text field is focused, so typing never triggers an action.

#### Validation, and what the errors mean

**Validate** and **Promote** both render the case and load it back through the real `load_skill`
parser before anything is written. A case the console accepts is by construction one the harness can
run. Errors are reported against the field that caused them:

| Message | Cause |
|---|---|
| `no target skill chosen` | The skill dropdown is blank |
| `expectation points at 'x.rs', which this diff does not change` | Path typo; the message lists the files the diff does touch |
| `expectation covers lines 5000–6000 …, it changes lines 40–43` | The region misses every hunk, so the case could never pass |
| `line range 45–40 is inverted` | First line after the last |
| `case id '…' is not usable as a folder name` | Ids become directory names; letters, digits, `.`, `-`, `_` only |
| `rule id '…' should look like R1 or SEC2` | Rule ids must match the tag shape used in `SKILL.md` |
| `missing diff file 'change.diff'` | The candidate folder is incomplete |

#### Rejecting

Rejections **require a reason** and are kept in `decision.json` beside the candidate. The corpus
builder assigns confidence by signal strength and nothing currently tells it whether those guesses
were any good — the noes are that evidence. A bare "no" teaches it nothing.

Both decisions are reversible: reopening a candidate is a `DELETE` on its decision. Undoing a
promotion does **not** revert its commit — the branch is the record of what was proposed, and
rewriting it silently would be worse than a duplicate.

#### Batches and proposing

Promotions accumulate on **one branch** — `whetstone/cases/batch-N` — so a triage session produces
one merge request rather than one per case. The header shows the current branch and a **Propose N
cases** button.

Which batch is open is derived from git, never stored: a branch that already has a remote-tracking
ref has been pushed, so the next promotion starts the following number. That lookup is local, so it
never touches the network.

**Propose** pushes the branch. It does **not** open the merge request — that needs a provider
implementing `WriteConnector`, which Milestone 1 defines but does not implement. The response says
where the branch went rather than pretending. Without a remote configured it refuses and tells you
the work is safe locally.

#### What triage never does

- Never touches your working tree or switches your branch. Commits are built with git plumbing
  against a temporary index.
- Never commits to `main`/`master`, or any branch in `[git] protected_branches`.
- Never pushes implicitly, and never pushes a branch it did not create. `Propose` refuses anything
  protected or outside `[git] branch_prefix`, so a stray request cannot publish whatever happens to
  be sitting on your local `main`.
- Refuses to write when the working tree is dirty in the paths it would touch — otherwise your
  uncommitted local edits would be swept into a console commit and attributed to you.

---

### Security and deployment

**The console has no authentication of its own, and will not grow one.** A half-built auth system is
worse than an explicit boundary.

**Local (the default).** Binds `127.0.0.1` and trusts the local git identity for attribution.
Binding a publicly reachable address requires `--insecure-bind`, deliberately:

```
$ whetstone ui --host 0.0.0.0
Error: refusing to bind 0.0.0.0 without --insecure-bind: the console has no
authentication of its own and would be reachable by anyone on the network
```

**For a team,** put an authenticating reverse proxy (OIDC) in front and opt in to its headers:

```toml
[ui]
trust_proxy_headers = true    # identity from X-Forwarded-User / X-Forwarded-Email
```

Until that flag is set those headers are **ignored** — otherwise forging one would be an
authentication bypass rather than a convenience. With it set and no headers present, the caller is
anonymous.

**Read-only mode** (`--read-only`, or `[ui] read_only = true`) disables every mutating route
server-side. The UI also hides write affordances, so nobody discovers the 403 by clicking — but the
guard is the server's, not the interface's.

---

### HTTP API

Interactive docs at **`/docs`**; the schema at **`/openapi.json`**. Responses are the same pydantic
models the CLI uses.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/config` | Capabilities, principal, read-only and practice flags, backends |
| `GET` | `/api/git/status` | Branch, head, cleanliness, remote |
| `POST` | `/api/git/propose` | Push a batch branch |
| `GET` | `/api/skills` | Index rows, weakest first |
| `GET` | `/api/skills/{id}` | Guidance, cases, runs, rules, untested rules |
| `GET` | `/api/skills/{id}/cases/{case_id}` | Eval case, diff, history |
| `GET` | `/api/runs` | Run history, `?skill_id=`, `?limit=` |
| `GET` | `/api/runs/{id}` | Full record — findings and verdicts |
| `GET` | `/api/runs/{id}/report` | Standalone HTML report |
| `GET` | `/api/candidates` | Triage queue + counts |
| `GET` | `/api/candidates/batch` | The branch the next promotion lands on |
| `GET` | `/api/candidates/{id}` | One candidate + its pre-filled edit form |
| `POST` | `/api/candidates/{id}/preview` | Validate edits, write nothing |
| `POST` | `/api/candidates/{id}/promote` | Commit the edited case to the batch branch |
| `POST` | `/api/candidates/{id}/reject` | Record a reasoned rejection |
| `DELETE` | `/api/candidates/{id}/decision` | Return a candidate to the queue |

Errors are `{"message": …, "path": …}`. `422` is a validation failure with `path` naming the field;
`404` is a missing resource; `409` is a concurrent-write conflict; `403` is read-only mode; `500` is
a misconfiguration.

---

### Developing the console

Two terminals, with hot reloading:

```bash
whetstone ui --dev     # API only on :8787, no browser
cd ui && npm run dev   # Vite on :5173, proxying /api
```

Open **http://localhost:5173**.

**Types are generated, not written.** `ui/src/api/schema.d.ts` comes from the app's OpenAPI schema:

```bash
cd ui && npm run gen:api
```

Run it after any API change and commit the result. A change to a pydantic model then surfaces as a
TypeScript compile error rather than a runtime surprise — it has already caught real bugs.

**Tests:**

```bash
uv run pytest tests/api      # every route, temp repo + temp run store, no network
cd ui && npm test            # frontend logic (vitest)
```

See [`ui/README.md`](ui/README.md) for frontend conventions and the dependency-advisory assessment.

---

### Console troubleshooting

| Symptom | Cause and fix |
|---|---|
| `the console needs the 'ui' extra` | `uv sync --extra ui`, or `pip install 'whetstone[ui]'` |
| Browser shows "Console assets not built" | `cd ui && npm install && npm run build`. The API is fine. |
| Skills index is empty | `[skills] root` points somewhere without skill folders. The start-up banner prints the resolved path. |
| Triage says "No candidate directory" | Run `whetstone corpus pull --out candidates`, or point `[candidates] dir` at existing output. |
| `⚠ version reused for different content` | Guidance changed without bumping `version` in `SKILL.md`. Bump it; comparison keys on content hash, not the number. |
| Promote fails with `uncommitted changes in …` | Commit or stash your local edits to those files first. The console will not sweep them into its commit. |
| Promote fails with a `409` | Another writer moved the branch. Reload and retry. |
| `Propose` fails with `no git remote` | Nothing is lost; the branch exists locally. Add a remote or push by hand. |
| `Propose` fails with `refusing to push` | The branch is protected or outside `[git] branch_prefix`. The console only publishes branches it created. |
| Header shows a `•` next to the branch | The working tree is dirty. Informational. |
| `did not respond within 30s` | The console process died. Check the terminal running `whetstone ui`. |
| Port already in use | `whetstone ui --port 9000` |

---

### Not built yet

The console covers browsing, diagnosis, and triage. Still to come (see
[`docs/ui-console.md`](docs/ui-console.md) for the plan and its phasing):

- **Authoring** — editing skill guidance and hand-writing eval cases, gated by a rule that no merge
  request may open without a passing gate for that exact content hash.
- **Run orchestration** — launching evals from the console, with progress, cancellation, cost
  estimation, and a working practice mode.
- **Compare & judge lab** — diffing two runs with client-side tolerance tuning, and labelling judge
  verdicts to grow the meta-eval set.

Until then, runs are launched with `whetstone eval run` and gates with `whetstone eval gate`.

---

## Configuration (`whetstone.toml`)

Optional. Discovered by walking up from the working directory; every field resolves
**flag → environment → file → default**. Relative paths resolve against the file's own directory.

```toml
[skills]
root = "skills"                  # the skill registry
repo = "."                       # git repo containing it; may be a separate checkout

[candidates]
dir = "candidates"               # where `corpus pull` writes and Triage reads

[git]
branch_prefix = "whetstone/"       # console branches; also the only ones it will push
default_base = "main"
push_remote = "origin"
author = "principal"               # "principal" credits whoever clicked; "console" uses the
                                   # repo's own git identity
protected_branches = ["main", "master"]

[runs]
dir = ".whetstone/runs"
max_llm_calls_per_run = 2000       # reserved; see the note below

[gate]                             # defaults for `whetstone eval gate`; --recall-tol overrides
recall_tol = 0.0
fp_tol = 0.0
```

> **`max_llm_calls_per_run` is declared but inert.** It is parsed and reported, and nothing enforces
> it: the console cannot launch runs yet, and a CLI run's budget is the operator's own shell. It
> becomes a real backstop in the phase that adds run orchestration.

Pointing at a separate company skills repo is `repo = "../company-skills"` — no code change.

---

## Programmatic API (`whetstone.service`)

The CLI is a thin wrapper over these functions. Every one takes an **injected `LLMClient`**, so you
can drive the whole system from code — with the real model or a fake.

```python
from whetstone.core.loader import load_skill
from whetstone.llm.anthropic_client import AnthropicClient
from whetstone.service import run_eval, gate_skills, pull_corpus, format_score, format_gate

client = AnthropicClient(model="claude-opus-4-8")

# Score a skill
score = run_eval(load_skill("skills/code-review-rust-error-handling"), client, trials=5)
print(format_score(score))

# Gate a candidate against a baseline
outcome = gate_skills(load_skill("skills/base"), load_skill("skills/candidate"), client)
print(format_gate(outcome))
assert outcome.result.passed
```

| Function | Signature | Returns |
|---|---|---|
| `run_eval` | `(skill, client, *, trials=1, reviewer_effort="high", judge_effort="medium")` | `SkillScore` |
| `record_eval` | `(skill, client, *, trials=1, backend="", model="", on_event=None, max_workers=1, cancel=None, …)` | `RunRecord` (score + findings + verdicts) |
| `gate_skills` | `(base, candidate, client, *, cfg=None, trials=1)` | `GateOutcome` (`.result`, `.base`, `.candidate`) |
| `pull_corpus` | `(connector, project, since, skills=None)` | `list[CandidateCase]` |
| `format_score` | `(SkillScore)` | `str` |
| `format_gate` | `(GateOutcome)` | `str` |

Because the client is injected, tests pass a `FakeLLMClient` (below) and run the exact same code
paths with no network.

---

## Providers & the plugin architecture

Providers are the only place that knows about GitLab or Jira (and, later, GitHub, wikis). They
implement narrow **capability protocols** (`providers/base.py`) and normalize provider payloads into
the canonical `domain` model.

```python
class Capability(StrEnum): source; review; tracker; write

class SourceConnector(Protocol):
    def capabilities(self) -> set[Capability]: ...
    def get_file(self, repo, ref, path) -> FileBlob | None: ...
    def get_change(self, repo, base, head) -> CodeChange: ...

class ReviewConnector(Protocol):
    def capabilities(self) -> set[Capability]: ...
    def list_reviewed_changes(self, repo, since) -> list[MergeRequestRef]: ...
    def get_review(self, mr) -> ReviewedChange: ...

class IssueConnector(Protocol):        # trackers: the escaped-defect signal
    def capabilities(self) -> set[Capability]: ...
    def list_resolved_issues(self, project, since) -> list[IssueRef]: ...
    def get_issue(self, ref) -> Issue: ...

class WriteConnector(Protocol):        # interface only in M1
    def open_change_request(self, repo, branch, title, body) -> str: ...
```

Capabilities are split so a provider implements only what it can: GitLab has no incidents, Jira has
no diffs, and neither should have to pretend otherwise.

### The registry (config-not-code onboarding)

```python
from whetstone.providers import build_provider, available_providers

available_providers()                  # {"fake", "gitlab", "jira"}
conn = build_provider({"kind": "gitlab", "base_url": "https://gitlab.acme.com"})
```

### GitLab connector

Implements `SourceConnector` + `ReviewConnector` against GitLab API v4. Owns auth, **429/5xx retry
with backoff**, and **`x-next-page` pagination** internally, so the core never sees a rate limit or
a page header. Maps GitLab's `suggestions[].applied` flag onto `Suggestion.applied` — the cleanest
accept/reject training signal there is.

```python
from whetstone.providers.gitlab.provider import GitLabConnector

conn = GitLabConnector.from_config({
    "base_url": "https://gitlab.acme.com",
    "token_env": "GITLAB_TOKEN",   # token read from this env var
})
```

### Jira connector

Implements `IssueConnector` — the **tracker** capability — against Jira REST. Deliberately separate
from `ReviewConnector`: a tracker knows nothing about diffs and a forge knows nothing about
incidents, so pairing them is the corpus builder's job (`corpus/linking.py`), not a provider's.

```python
from whetstone.providers.jira.provider import JiraConnector

conn = JiraConnector.from_config({
    "base_url": "https://acme.atlassian.net",
    "token_env": "JIRA_TOKEN",
    "email": "you@acme.com",       # present → Cloud Basic auth; absent → Server/DC bearer
    "defect_types": ["bug", "incident", "sev1"],   # your instance's names for "went wrong"
    "jql_filter": 'labels = "production"',          # optional extra AND clause
    "search_path": "/rest/api/2/search",            # override for Server/Data Center
})
```

What it owns internally, so the core never sees it:

- **Both auth schemes.** Cloud authenticates an API token as Basic against the account email;
  Server/DC uses a bearer PAT. Which one is in play follows from whether an email was configured.
- **Both pagination styles.** Cloud's newer search returns an opaque `nextPageToken`; Server/DC
  returns `startAt`/`total`. Detected per response rather than declared, plus a hard limit so a
  mistyped JQL cannot page through an entire instance.
- **ADF flattening.** Cloud v3 returns descriptions as a nested node tree, Server returns a string.
  Both come out as text, and inline runs are joined without separators so a sentence containing a
  code-formatted identifier does not come back with gaps in it.
- **JQL construction**, with the project key validated — it arrives from the command line and lands
  inside a quoted JQL string.
- **Best-effort remote links.** Plenty of instances have no forge integration; a 404 there returns
  no links rather than aborting a backfill. The primary join is the issue key mentioned in the merge
  request, which needs no Jira call at all.

### Issue ↔ merge-request linking

`corpus/linking.py` joins the two sides on evidence both already publish:

1. **The issue key in the merge request** — title, description, or branch name (`feature/PAY-812`).
   Near-universal and needs nothing configured.
2. **The tracker's own remote links** — authoritative when present, frequently absent. Matched on
   the MR number *and* the project path, since `!910` exists in every repository.

Either is enough. When several merge requests reference one issue — a fix plus its follow-up, or a
backport — all of them are returned rather than guessing which was the real fix.

### FakeProvider

An in-memory implementation of every capability (`providers/fake/`). Seed it with `add_file`,
`add_change`, `add_review`, `add_issue`; the whole harness and corpus builder run against it with no
network.

### Contract conformance suite

`tests/contract/conformance.py` defines the behavioral contract **once** as mixin classes —
`SourceContract`, `ReviewContract`, `IssueContract`. Providers subclass them and pass identical
assertions (GitLab and Jira through recorded `respx` cassettes in `tests/fixtures/`). Any new
provider must pass the same suite — this is how "plugin-ready" is enforced rather than hoped.

---

## The corpus builder

`corpus/builder.py` turns review history and resolved defects into **candidate eval cases**. What a
signal is evidence *of* depends on its outcome, not merely its existence:

| Signal | Case kind | Confidence |
|---|---|---|
| **Escaped defect** — a tracker bug, its fix reversed | `should_catch` | 0.95 |
| Suggestion **applied** | `should_catch` | 0.9 |
| **The accepted fix** — that suggestion, applied | `should_not_flag` | 0.85 |
| Escaped defect from a sprawling multi-file fix | `should_catch` | 0.75 |
| Suggestion **declined** (thread resolved, not applied) | `should_not_flag` | 0.6 |
| Resolved diff comment | `should_catch` | 0.5 |
| Merged with no diff-anchored feedback | `should_not_flag` (sampled per file) | 0.3 |
| Diff comment on a still-**open** thread | `should_catch` | 0.2 |

Several of those rows exist to avoid overclaiming. A thread nobody resolved is an argument in
progress — evidence of attention, not of a verdict — so it lands at the bottom of the queue labelled
`reviewer comment left open` rather than masquerading as a settled catch. A suggestion the author
closed *without* taking is the cleanest negative label GitLab gives: the reviewer raised a point and
the team declined it. (Not certain — an author who made the same fix by hand also leaves the
suggestion unapplied — hence 0.6 and a human to confirm.)

### Escaped defects: the strongest recall signal

Review history tells you what a reviewer *caught*. A tracker tells you what everybody **missed**.
Recall asks "would we have caught this?", and for a defect that shipped the honest answer is already
known to be no — which makes it the most valuable case in the corpus.

Pair a resolved defect with the merge request that fixed it and **reverse that fix**: where the fix
removed `.unwrap()` and added `?`, the reversal adds `.unwrap()` back. That reversed diff is exactly
the change a reviewer should have objected to.

```
PAY-812 "Charge handler panics…"  ──┐
                                    ├─▶ reverse(fix)  ──▶  should_catch @ 0.95
acme/payments!910 (the fix)       ──┘                      semantic = the issue summary
```

The expectation's ground truth is the **issue summary** — "Charge handler panics when the DB row is
missing" — which is written to be understood on its own, unlike the review-comment bodies the MR
path has to work with.

Guards, because reversal is not always meaningful:

- A fix that only *adds* lines (a new guard clause) reverses to a pure deletion, leaving no line in
  the new file to anchor an expectation to. Those are skipped rather than turned into a case that can
  never match.
- A fix touching many files is a fix mixed with refactoring; reversing all of it reintroduces things
  nobody called a bug. Confidence drops to 0.75 and the sample is capped at `--max-defect-files`.
- Only issues whose *type* means "something was wrong with the product" qualify — see
  `defect_types`.

### Precision evidence that isn't just silence

`should_not_flag` cases built from clean merges have a soft spot: nobody commenting is not the same
as there being nothing to flag, so an `fp_rate` computed mostly from those rewards a reviewer that
says nothing. Two things address it.

**The accepted fix.** An applied suggestion carries its own replacement text, endorsed twice — the
reviewer proposed it, the author took it. Applying it to the same hunk yields code a reviewer must
stay quiet about, so flagging it is a false positive on the exact pattern the rule targets. That is
*confirmed* evidence rather than inferred silence, and it was free: `Suggestion.proposed` was already
being parsed and discarded. Every applied suggestion now yields a catch/no-flag pair.

**Saying so when it isn't.** `precision_evidence(skill)` counts a skill's negative cases by strength
— `confirmed`, `silence`, `unclassified` (hand-written cases are not guessed at). `whetstone skills
list` and the console's skill page flag a skill whose precision rests mostly on silence, because its
`fp_rate` should be read with suspicion. The inference cannot be repaired; hiding it was the part
that was fixable.

Key functions:

```python
from whetstone.corpus.builder import (
    pull_candidates, build_candidates, classify, route_to_skill, write_candidate,
    pull_defect_candidates, defect_candidates,
)

candidates = pull_candidates(connector, repo, since, skills)   # walk a repo's reviews
candidates = build_candidates(reviewed_change, skills)         # one MR
signal     = classify(thread)                                  # kind, confidence, label
skill_id   = route_to_skill("src/handlers/charge.rs", skills, labels)
write_candidate(candidate, "eval_cases/<id>")                  # serialize to disk

# The tracker side: resolved defects paired with the merge requests that fixed them.
candidates = pull_defect_candidates(reviews, tracker, repo, "PAY", since, skills)
candidates = defect_candidates(issue, fixing_change, skills)   # one issue + its fix
```

Design guarantees:

- **Human-in-the-loop:** the builder only *proposes*. A person promotes candidates into a skill.
- **Focused cases:** each candidate is narrowed to the single file the thread anchors on.
- **Faithful diffs:** `CodeChange.to_unified_diff()` reconstructs a real `change.diff` (using the
  provider's captured `raw_diff`), so a promoted candidate round-trips through `load_skill` as a
  valid `EvalCase`.
- **Auto-routing:** `route_to_skill` matches the changed path against skill `triggers.paths`, then
  falls back to the MR's labels against `triggers.labels`. Path wins, because it describes the file
  the case is about while a label describes the whole merge request. Defect candidates additionally
  route on the issue's own labels and components.
- **Bounded sampling:** a comment-free merge contributes at most `max_clean_files` (default 5)
  `should_not_flag` candidates, preferring files that route to a skill. Uncapped, one 200-file
  refactor buried every high-signal candidate under the weakest signal the builder produces. When
  the sample is capped the candidate's own rationale says so (`Sampled 5 of 200 changed files`) —
  a truncated sample that reads like a complete one invites "this MR was clean across the board".

**Remaining bias.** The clean-merge inference is still an inference: `--max-clean-files 0` turns it
off entirely if you would rather have a smaller corpus than a flattering one. What the accepted-fix
counterparts change is that a corpus no longer *has* to lean on it for precision.

---

## The LLM layer

`llm/base.py` defines a single abstraction:

```python
class LLMClient(Protocol):
    def structured(self, system: str, user: str, schema: type[T], *, effort="high") -> T: ...
```

Given a system + user prompt and a pydantic schema, return a **validated** instance of that schema.

### `AnthropicClient` (real)

`llm/anthropic_client.py`. Uses `messages.parse(output_format=...)` for structured output, adaptive
thinking, and the effort knob. **Not imported by the `llm` package `__init__`, and the SDK import is
lazy inside the constructor** — so importing the layer never requires `anthropic`, and the SDK is
only needed when you actually construct this client.

```python
from whetstone.llm.anthropic_client import AnthropicClient
client = AnthropicClient(model="claude-opus-4-8", max_tokens=8192)
```

Credentials resolve from the environment (`ANTHROPIC_API_KEY`) or an `ant auth login` profile — see
the [Anthropic docs](https://platform.claude.com/docs).

### `OpenAICompatibleClient` (local & OpenAI-compatible)

`llm/openai_client.py`. Talks to **any OpenAI-compatible `/v1/chat/completions` endpoint** over the
`httpx` already in the tree — no extra dependency. This is the path for **local models** (a Raspberry
Pi or a workstation running Qwen, Llama, etc.) via Ollama, LM Studio, llama.cpp server, vLLM, or
LocalAI — for **custom harnesses** (a bespoke server, an internal gateway) — and for OpenAI itself.

Local models follow schemas less reliably than a frontier model, so structured output is hardened:
the target JSON Schema is embedded in the system prompt, `response_format` requests a JSON object
(dropped automatically if the server rejects it), and the reply is parsed, schema-validated, and
**retried with the validation error fed back** until it conforms. Temperature is `0` for stability.

You rarely construct it directly — use the factory below.

### Choosing a backend — `build_llm_client` and `--llm`

`llm/factory.py` is the one convenient seam for picking a backend, cloud or local. Every field
resolves **arg → environment variable → preset default**:

```python
from whetstone.llm.factory import build_llm_client

build_llm_client()                                     # Anthropic (default)
build_llm_client("ollama", model="qwen2.5-coder:7b")   # local Qwen via Ollama
build_llm_client("lmstudio", model="qwen2.5-coder-7b-instruct")
build_llm_client("ollama", model="qwen2.5-coder:7b",
                 base_url="http://raspberrypi.local:11434/v1")  # a remote Pi
```

Presets (see `whetstone llm list`): `anthropic` (default), `openai`, the local runners `ollama`,
`lmstudio`, `vllm`, `llamacpp`, and a generic `custom` slot. Override any preset's `base_url` to
reach another host.

#### Custom harnesses (a Pi server, a `codex` gateway, anything OpenAI-compatible)

Any name that **isn't** a known preset is accepted as a custom OpenAI-compatible harness **as long as
you supply a base URL** — the name is then just a label. So a bespoke server has access with no code
change:

```bash
whetstone llm check --llm codex --model codex-mini --base-url http://pi.local:8080/v1
whetstone eval run  --skill skills/... --llm pi --model qwen2.5-coder:7b \
  --base-url http://raspberrypi.local:11434/v1
```

```python
build_llm_client("codex", model="codex-mini", base_url="http://pi.local:8080/v1")
build_llm_client("custom", model="...", base_url="http://gateway.internal/v1")
```

Two safety properties for custom harnesses:

- **No credential leak.** The `custom` slot (and any unrecognized name) assumes **no** API key, so a
  stray `OPENAI_API_KEY` in your environment is **never** sent to your box. If your gateway needs a
  token, name the env var holding it: `--api-key-env MY_GATEWAY_TOKEN`.
- **Typos still fail loudly.** An unknown name *without* a base URL is treated as a typo and rejected
  with the list of valid presets — you don't silently hit the wrong endpoint.

Slow hardware? Raise the per-request timeout (a Pi running a 7B model can take minutes):
`--llm ollama ... ` with `WHETSTONE_LLM_TIMEOUT=600` (seconds), or `build_llm_client(..., timeout=600)`.

From the CLI, `eval run` and `eval gate` take the same options:

```bash
# Score a skill against a local Qwen served by Ollama
whetstone eval run --skill skills/code-review-rust-error-handling \
  --llm ollama --model qwen2.5-coder:7b

# Point at a Raspberry Pi on the LAN
whetstone eval run --skill skills/... --llm ollama \
  --model qwen2.5-coder:7b --base-url http://raspberrypi.local:11434/v1
```

Or configure it once via environment, and every command uses it with no flags:

```bash
export WHETSTONE_LLM=ollama
export WHETSTONE_LLM_MODEL=qwen2.5-coder:7b
export WHETSTONE_LLM_BASE_URL=http://raspberrypi.local:11434/v1   # optional; preset default otherwise
whetstone eval run --skill skills/...
```

Verify a backend is reachable and returns valid JSON before running a real eval:

```bash
whetstone llm check --llm ollama --model qwen2.5-coder:7b
# OK: backend returned ok=True note='ready'   (exit 0; exit 1 + FAIL on any error)
```

### `FakeLLMClient` (test)

`llm/fake_client.py`. A handler maps `(system, user, schema) → schema instance`. It records every
call in `.calls` (so tests can assert the assembled prompts) and validates the returned type.

```python
from whetstone.llm import FakeLLMClient

def handler(system, user, schema):
    ...  # return an instance of `schema`
client = FakeLLMClient(handler)
```

---

## Reviewers & judges

Both are `Protocol`s with a real (LLM) and a fake (deterministic) implementation, so the harness runs
either way.

**Reviewer** — `review(skill, change) -> list[Finding]`:

- `LLMReviewer(client, *, effort="high")` — builds a prompt from the skill body + the change's
  unified diff, gets structured findings, maps them to domain `Finding`s. Prompted for **coverage,
  not filtering** (report everything with confidence + severity; a later step filters).
- `PatternReviewer(skill_id, rules)` — a deterministic test double that flags added lines matching a
  regex. Used to pin the harness/gate math in golden tests without a model.

**Judge** — `match(finding, expectation) -> Match`:

- `LLMJudge(client, *, effort="medium")` — decides whether a finding refers to the same underlying
  issue an expectation describes. Region/severity prefiltering happens upstream; the judge makes only
  the semantic call.
- `DeterministicJudge` — a keyword/regex stand-in (uses the expectation's optional `pattern`). Used
  in deterministic tests.

```python
from whetstone.reviewer import LLMReviewer
from whetstone.judge import LLMJudge
from whetstone.core.harness import run_skill

score = run_skill(skill, LLMReviewer(client), LLMJudge(client), k=5)
```

---

## Meta-evaluation (validating the judge)

The LLM judge decides every match — so its verdicts are only trustworthy if they agree with humans.
`meta_eval/` measures that agreement against a labeled dataset.

```python
from whetstone.meta_eval import (
    load_meta_eval_cases, evaluate_judge, JUDGE_ACCURACY_FLOOR,
)
from whetstone.judge import LLMJudge

cases = load_meta_eval_cases("tests/fixtures/meta_eval/labeled.json")
report = evaluate_judge(LLMJudge(client), cases)
assert report.accuracy >= JUDGE_ACCURACY_FLOOR   # default floor: 0.8
```

- `MetaEvalCase` = `(finding, expectation, is_match)` where `is_match` is the **human** label.
- `evaluate_judge` returns `MetaEvalReport(total, correct)` with an `.accuracy` property.
- The **accuracy math** is unit-tested deterministically with a stub judge; the **real judge** is
  measured against the labeled fixture in the opt-in live job. An unvalidated judge silently corrupts
  every `SkillScore` — this closes that hole.

Add labeled pairs to `tests/fixtures/meta_eval/labeled.json` (each entry: a `finding`, an
`expectation`, and `is_match`) to strengthen the guardrail.

---

## Testing

```bash
uv run pytest              # everything deterministic (default; no network, no model)
uv run pytest tests/unit
uv run pytest tests/contract   # provider conformance (Fake + GitLab via cassettes)
uv run pytest tests/golden     # end-to-end gate math with a deterministic reviewer
```

Test layers:

| Directory | What it covers |
|---|---|
| `tests/unit/` | Scoring, gate, matching, diff parser, loader, corpus builder, LLM reviewer/judge, meta-eval, run capture, run store, git I/O, config, report, service, CLI. |
| `tests/api/` | Every console route, via `TestClient` against a temp git repo and a temp run store. No network, no model. |
| `ui/src/**/*.test.ts` | Frontend logic (`npm test`, vitest) — the diff parser that produces the line numbers everything else anchors to. |
| `tests/contract/` | The provider conformance suite, run against `FakeProvider` and the GitLab adapter. |
| `tests/golden/` | The full harness + gate driven by a `PatternReviewer`, asserting exact scores (guards against silent scoring drift). |
| `tests/live/` | **Opt-in** real-model tests (judge meta-eval). Skipped unless `WHETSTONE_LIVE_LLM=1`. |
| `tests/fixtures/` | GitLab cassettes and the meta-eval labeled set. |

Run the live tests (needs Anthropic credentials):

```bash
WHETSTONE_LIVE_LLM=1 uv run pytest tests/live
```

Quality gates the CI should run: `uv run pytest`, `uv run ruff check .`, `uv run mypy`.

---

## Extending Whetstone

### Add a new provider (e.g. GitHub)

1. Create `src/whetstone/providers/github/provider.py` implementing the capability `Protocol`s
   (`capabilities`, `get_file`/`get_change` and/or `list_reviewed_changes`/`get_review`) and
   normalizing GitHub payloads into `domain` types (`CodeChange`, `ReviewThread`, `Suggestion`, …).
   Add a `from_config(cls, config)` classmethod.
2. Register it in `providers/registry.py` (`_builders()` map).
3. Add a `TestGitHubConformance` module that subclasses the mixins in
   `tests/contract/conformance.py` and supplies `connector` + scenario fixtures. It must pass the
   **same** assertions the Fake and GitLab providers pass.

No core code changes — that's the point of the boundary.

### Add a skill

```
skills/<new-id>/
  SKILL.md          # frontmatter (id, triggers.paths) + guidance body
  meta.yaml         # owner, references (optional)
  eval_cases/       # start empty; add cases below
```

### Add an eval case

- **Manually:** create `eval_cases/<case-id>/case.yaml` + `change.diff` following
  [Eval cases](#eval-cases).
- **From history:** run `whetstone corpus pull` to generate candidates from real MRs, review the
  `candidate.json` triage metadata, then `whetstone corpus promote` the good ones.

Aim for a **balanced set** — enough `should_catch` cases to measure recall and enough
`should_not_flag` cases to keep precision honest.

---

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `AnthropicClient` | Anthropic credentials (or use an `ant auth login` profile). |
| `WHETSTONE_LLM` | `build_llm_client` | Backend preset when `--llm` is omitted: `anthropic` (default), `openai`, `ollama`, `lmstudio`, `vllm`, `llamacpp`, `custom` — or any custom-harness label. |
| `WHETSTONE_LLM_MODEL` | `build_llm_client` | Model id when `--model` is omitted (required for local/OpenAI/custom backends). |
| `WHETSTONE_LLM_BASE_URL` | `build_llm_client` | OpenAI-compatible endpoint when `--base-url` is omitted — e.g. a remote Pi or custom harness. |
| `WHETSTONE_LLM_API_KEY_ENV` | `build_llm_client` | Name of the env var holding the API key, if the backend needs one. |
| `WHETSTONE_LLM_TIMEOUT` | `build_llm_client` | Per-request timeout in seconds for OpenAI-compatible backends (raise it for slow local hardware). |
| `GITLAB_TOKEN` | GitLab connector | Personal/project access token. The env-var **name** is configurable via `--token-env` / `token_env`. |
| `WHETSTONE_LIVE_LLM` | `tests/live/` | Set to `1` to run the opt-in live-model tests. |
| `WHETSTONE_SKILLS_ROOT` | `config` | Skill registry path, overriding `whetstone.toml`. |
| `WHETSTONE_SKILLS_REPO` | `config` | Git repo containing the registry. |
| `WHETSTONE_RUNS_DIR` | `config` | Where run records are stored. |
| `WHETSTONE_CANDIDATES_DIR` | `config` | Where the triage queue is read from. |

---

## Repository layout

```
whetstone/
  pyproject.toml            # deps, console script, ruff/mypy/pytest config
  README.md                 # this file
  docs/
    milestone-1-eval-harness.md   # detailed M1 design
    decisions.md                  # architecture decision record
  src/whetstone/            # see Architecture
  skills/                   # the skill registry
  tests/                    # unit · contract · golden · live · fixtures
```

---

## Where this is going

Milestone 1 delivers **measurement before automation**: the `gate` function, callable from code,
CLI, and (trivially wrapped) HTTP. The next milestones plug into that same seam:

- **Distillation + proposal engine** — cluster review signals into candidate learnings and generate
  skill diffs, each of which must pass `eval gate` before it can merge.
- **Control-plane API + governance** — the `POST /sharpen` surface, approval workflow, and drift
  detection.

Nothing ships that the gate can't show is a net improvement.
