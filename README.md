# Whetstone

A system for keeping a company's agent **skills** (code review, arch review, secret-scanning, …)
continuously sharp. It learns from GitLab merge-request reviews and the codebase, and — critically —
**never ships a skill change it can't prove is a net improvement**, because every change passes an
evaluated regression gate first.

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
9. [Programmatic API (`whetstone.service`)](#programmatic-api-whetstoneservice)
10. [Providers & the plugin architecture](#providers--the-plugin-architecture)
11. [The corpus builder](#the-corpus-builder)
12. [The LLM layer](#the-llm-layer)
13. [Reviewers & judges](#reviewers--judges)
14. [Meta-evaluation (validating the judge)](#meta-evaluation-validating-the-judge)
15. [Testing](#testing)
16. [Extending Whetstone](#extending-whetstone)
17. [Environment variables](#environment-variables)
18. [Repository layout](#repository-layout)

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
                enums, refs, change (+diff parser), finding, eval_model, skill, review, score
  core/         The harness. loader · matching · scoring · gate · harness
  reviewer/     Reviewer protocol + LLMReviewer + PatternReviewer (test double)
  judge/        Judge protocol + LLMJudge + DeterministicJudge (test double)
  llm/          LLMClient protocol + factory · AnthropicClient · OpenAICompatibleClient (local) · FakeLLMClient (test)
  providers/    Capability protocols + registry + gitlab/ adapter + fake/ provider
  corpus/       MR history → candidate eval cases (human-promoted)
  meta_eval/    Validate a judge against human-labeled pairs
  service.py    Operable API layer (used by the CLI and any future HTTP layer)
  cli.py        `whetstone` command-line interface
skills/         The skill registry (folders of SKILL.md + meta.yaml + eval_cases/)
tests/          unit · contract (provider conformance) · golden · live (opt-in) · fixtures
docs/           Milestone plan + decision record
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
| `triggers.labels` | MR labels the skill applies to. |

The markdown **body** below the frontmatter is the actual guidance handed to the reviewer.

### `meta.yaml`

Machine metadata. `references` are resolvable pointers (drift-checkable later), not copied text;
`provenance` records which signals justified each rule.

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

`GateConfig` (all defaults are strict):

| Field | Default | Meaning |
|---|---|---|
| `recall_tol` | `0.0` | Allowed recall drop. |
| `fp_tol` | `0.0` | Allowed false-positive-rate rise. |
| `max_case_regressions` | `0` | How many previously-passing cases may regress. |
| `case_recall_floor` | `0.999` | A case "passes" if its recall ≥ this. |
| `case_fp_ceiling` | `0.001` | …and its fp_rate ≤ this. |

`GateResult` reports `passed`, `reasons` (human-readable failure list), `regressed_cases`, and the
before/after recall and fp_rate. This is the seam the CI job and every future proposal engine call.

---

## CLI reference

Top-level groups: `eval`, `corpus`, `skills`, `providers`.

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
| `--dry-run` | off | Validate both sides; **no model call**. |
| `--json` | off | Emit the full `GateOutcome` as JSON. |

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
| `--skills-root PATH` | *(none)* | Skills root; used to route each candidate to a skill by trigger globs. |

```bash
export GITLAB_TOKEN=glpat-...
whetstone corpus pull \
  --base-url https://gitlab.acme.com \
  --project acme/payments \
  --since 2026-01-01 \
  --out ./candidates \
  --skills-root skills
```

Each candidate is written to `./candidates/<id>/` as `case.yaml` + `change.diff` (ready to promote)
plus `candidate.json` (kind, confidence, suggested skill, rationale) for triage. Nothing enters a
skill automatically.

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

List skills under a root and their eval-case counts.

| Option | Default | Meaning |
|---|---|---|
| `--root PATH` | `skills` | Skills root folder. |

```bash
whetstone skills list --root skills
# code-review-rust-error-handling  v1  (3 eval cases)
```

### `whetstone providers list`

List registered provider plugins (no options).

```bash
whetstone providers list
# fake
# gitlab
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
| `gate_skills` | `(base, candidate, client, *, cfg=None, trials=1)` | `GateOutcome` (`.result`, `.base`, `.candidate`) |
| `pull_corpus` | `(connector, project, since, skills=None)` | `list[CandidateCase]` |
| `format_score` | `(SkillScore)` | `str` |
| `format_gate` | `(GateOutcome)` | `str` |

Because the client is injected, tests pass a `FakeLLMClient` (below) and run the exact same code
paths with no network.

---

## Providers & the plugin architecture

Providers are the only place that knows about GitLab (and, later, GitHub, Jira, wikis). They
implement narrow **capability protocols** (`providers/base.py`) and normalize provider payloads into
the canonical `domain` model.

```python
class Capability(StrEnum): source; review; write

class SourceConnector(Protocol):
    def capabilities(self) -> set[Capability]: ...
    def get_file(self, repo, ref, path) -> FileBlob | None: ...
    def get_change(self, repo, base, head) -> CodeChange: ...

class ReviewConnector(Protocol):
    def capabilities(self) -> set[Capability]: ...
    def list_reviewed_changes(self, repo, since) -> list[MergeRequestRef]: ...
    def get_review(self, mr) -> ReviewedChange: ...

class WriteConnector(Protocol):        # interface only in M1
    def open_change_request(self, repo, branch, title, body) -> str: ...
```

### The registry (config-not-code onboarding)

```python
from whetstone.providers import build_provider, available_providers

available_providers()                  # {"fake", "gitlab"}
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

### FakeProvider

An in-memory implementation of every capability (`providers/fake/`). Seed it with `add_file`,
`add_change`, `add_review`; the whole harness and corpus builder run against it with no network.

### Contract conformance suite

`tests/contract/conformance.py` defines the behavioral contract **once** as mixin classes. Both the
Fake and GitLab providers subclass them and pass the identical assertions (GitLab through recorded
`respx` cassettes in `tests/fixtures/gitlab/`). Any new provider must pass the same suite — this is
how "plugin-ready" is enforced rather than hoped.

---

## The corpus builder

`corpus/builder.py` turns the `ReviewedChange` objects the connector produces into **candidate eval
cases**, by signal strength:

| GitLab signal | Case kind | Confidence |
|---|---|---|
| Suggestion **applied** | `should_catch` | 0.9 |
| Resolved diff comment | `should_catch` | 0.5 |
| Merged with no diff-anchored feedback | `should_not_flag` (per file) | 0.3 |

Key functions:

```python
from whetstone.corpus.builder import (
    pull_candidates, build_candidates, route_to_skill, write_candidate,
)

candidates = pull_candidates(connector, repo, since, skills)   # walk a repo
candidates = build_candidates(reviewed_change, skills)         # one MR
skill_id   = route_to_skill("src/handlers/charge.rs", skills)  # match trigger globs
write_candidate(candidate, "eval_cases/<id>")                  # serialize to disk
```

Design guarantees:

- **Human-in-the-loop:** the builder only *proposes*. A person promotes candidates into a skill.
- **Focused cases:** each candidate is narrowed to the single file the thread anchors on.
- **Faithful diffs:** `CodeChange.to_unified_diff()` reconstructs a real `change.diff` (using the
  provider's captured `raw_diff`), so a promoted candidate round-trips through `load_skill` as a
  valid `EvalCase`.
- **Auto-routing:** `route_to_skill` matches the changed path against skill `triggers.paths`.

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
| `tests/unit/` | Scoring, gate, matching, diff parser, loader, corpus builder, LLM reviewer/judge, meta-eval, service, CLI. |
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
