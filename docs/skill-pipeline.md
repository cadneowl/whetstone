# The skill pipeline: `evaluate/`, `improve/`, `update/`

Every skill can carry its own scripts for keeping itself sharp. They live in the skill folder, next
to the guidance they serve:

```
skills/<id>/
  SKILL.md           the rules the reviewer applies
  meta.yaml          owner, references, rule → signal provenance
  eval_cases/        the promoted cases that constrain the rules
  wiki/              repo context the reviewer reads at review time   (generated)
  evaluate/step.yaml how this skill is scored
  improve/step.yaml  how a guidance change is drafted from failures
  update/step.yaml   how the wiki is regenerated
```

`whetstone skills scaffold --skill skills/<id>` writes correct starter versions of all three. The
generated files carry their own documentation — every setting is present with its default and a
comment saying what changing it costs — so the fastest way to learn this format is to generate it
and read it.

None of the three folders is required. A skill without them behaves exactly as it did before they
existed.

---

## Why the host owns the budget

A skill mining a real MR stream does not stay small. At ten thousand promoted cases a single
`eval run` is twenty thousand model calls, and the failures from one run would not fit in any
prompt. The naive fix — hand the improve step the first N failures — is worse than useless, because
the first N alphabetically are usually N instances of the same problem.

So the division is:

- **The skill declares policy.** How to cluster failures, how many to look at, what to say to the
  model, which generator produces its wiki.
- **Whetstone enforces the budget.** It assembles the digest, clusters it, truncates the diffs,
  caps the wiki, and only then renders the prompt.

A step never walks `eval_cases/` because it is never given the chance to. That is what makes this
work identically at forty cases and at a hundred thousand — and it means a step author cannot get
the scaling wrong, because the scaling is not theirs to get wrong.

Nothing is dropped silently. The digest always reports the true failure count alongside the number
shown, and the preflight reports any wiki page the caps excluded.

---

## Before anything spends money

Every command that reaches a model prints what it is about to do and asks:

```
This step will launch LLM interactions, which might involve cost based on your configuration.

  step      eval gate
  backend   anthropic (this backend bills per call)
  model     claude-opus-4-8
  estimate  up to 12 LLM call(s)
            3 case(s) x 1 trial(s) x (1 review + up to 1.0 judge calls); judging stops at the
            first match, so real runs usually cost less
  note      both base and candidate are scored, so this is doubled
  warning   wiki caps omit 2 matching page(s) on at least one case (payments, ledger)

Proceed? [Y/n]:
```

- The estimate is an **upper bound**. Judging short-circuits at the first matching finding, so real
  runs usually come in under it.
- "Billed" is three-state: `local` for Ollama and friends, `billed` for Anthropic and OpenAI, and
  `custom endpoint — Whetstone cannot tell` for anything else. Guessing "free" for the third case
  is the guess that costs money.
- A local backend skips the prompt entirely; nothing can bill.
- **`--yes` skips the confirmation.** CI needs it. Without a confirmation and without `--yes`, the
  command aborts rather than assuming consent.
- The banner goes to stderr, so `--json` on stdout stays machine-readable.

`[runs] max_llm_calls_per_run` in `whetstone.toml` adds a warning line when the estimate exceeds it.
It warns rather than refuses, because the estimate is an upper bound and refusing on it would block
runs that would have come in under budget.

---

## `evaluate/step.yaml` — how this skill is scored

Configuration only: no prompt, no program. `whetstone eval run` and `whetstone eval gate` read it
and use it as their defaults; any flag on the command line wins.

```yaml
description: Score this skill against its promoted eval cases.

trials: 1                 # reviewer passes per case; multiplies the cost of every run and gate

sample:
  max_cases: null         # null scores everything; a number caps it
  seed: 0                 # selection is a hash of this and the case id
  stratify: true          # draw proportionally from each case kind

inputs:
  wiki:
    max_pages: 4          # repo context per review, when the skill has a wiki/
    max_bytes: 24000

model:
  llm: ollama             # pin this skill to a backend; omit a key to inherit the command's
  model: qwen2.5-coder:7b
  effort: high
```

The `model:` block is how a skill stays off a metered API by default. `eval run`, `eval gate` and
`skills improve` all read it, and `--llm` / `--model` / `--base-url` / `--effort` override it per
command — the skill sets the default, the operator running it gets the last word.

### Sampling, and why a sampled gate is still legitimate

Selection is `sha256(seed:case_id)`, never a random draw and never iteration order. Three things
follow:

- **Base and candidate see one identical draw.** The gate samples the *union* of both sides' cases
  once and hands the same set to each, so a score difference is still attributable to the guidance.
- **Re-running reproduces the result.** The same corpus and seed always draw the same cases, on any
  machine and any Python build.
- **`--targeted` cases are exempt.** A change asserting it fixes case X is always scored on X,
  whatever the draw says. If you name more targeted cases than the budget allows, all of them are
  scored anyway — silently dropping one would fail the gate for a reason nobody could see.

`stratify: true` allocates the budget across `should_catch` / `should_not_flag` proportionally.
Turn it off and a sample of a corpus that is 90% positive will sometimes contain no negative cases
at all — and a false-positive rate measured over zero negative cases is a flattering zero.

Override per run: `--sample 200 --sample-seed 7`.

---

## `improve/step.yaml` — drafting a guidance change

```yaml
description: Draft a guidance change from the failures of the last run.

inputs:
  failures:
    max: 12               # cluster representatives, not the first 12
    cluster_by: rule      # rule | expectation | path | none
    max_diff_bytes: 2000  # diff shown per failure
    outcomes: [fn, fp]    # learn from misses, false positives, or both
  wiki:
    max_pages: 4
    max_bytes: 24000

prompt: prompt.md
```

Run it:

```bash
uv run whetstone skills improve --skill skills/<id> --apply     # stage it, ready to gate
uv run whetstone skills improve --skill skills/<id> --dry-run   # see the prompt; no model call
uv run whetstone skills improve --skill skills/<id> \
  --instruction "focus on false positives in test files"
```

It reads the skill's most recent stored run (`--run <id>` to pick another), builds the digest, and
returns a complete rewritten guidance body, a rationale, and the eval case ids the change is meant
to fix.

### `--apply`, and why you want it

`--apply` stages the proposal on `whetstone/skill/<id>` — the same branch the console's guidance
editor writes to, through the same `prepare_guidance` path. That means the frontmatter is preserved,
the version is bumped once per proposal, the result is validated by loading it back, and your
working tree is untouched. It then prints a gate command you can run **as printed**:

```
staged v2 on whetstone/skill/code-review-rust-error-handling (5ae78876bb)

gate it, then Propose MR in the console unlocks:
  whetstone eval gate --repo . --skill-path skills/code-review-rust-error-handling \
    --base-ref main --candidate-ref whetstone/skill/code-review-rust-error-handling \
    --targeted unwrap-in-handler
```

Without `--apply` you get the raw body on stdout (or in `--out`) and the job of splicing it into a
`SKILL.md` yourself. **That is a body, not a file.** Overwriting `SKILL.md` with it drops `id`,
`version` and `triggers` — the id then falls back to the folder name, and a gate run on that folder
records its evidence under a skill that does not exist, which C6 can never match. The command says
so when you use `--out`, but `--apply` is the path that cannot go wrong.

### The run has to describe the skill you have

If the skill has been edited since the run was scored, `skills improve` refuses:

```
run 2026…-6d01a7 scored a different version of this skill (33d4959ad7, now 68c51c5159).
Its failures describe a reviewer that no longer exists — the guidance, the eval cases or
the wiki changed since. Re-run `whetstone eval run` first, or pass --stale-ok.
```

Improving from stale failures produces a confident proposal aimed at a problem that may already be
fixed. `--stale-ok` proceeds anyway when you know better.

A run with no failures at all does not spend a call either — there is nothing to learn from. Pass
`--instruction` if you want the guidance rewritten regardless.

### Clustering is the whole point

Failures are grouped by cause and one representative is taken per group, largest group first. Twelve
failures chosen that way are twelve *different things wrong with the guidance*; twelve chosen by
slicing are usually one thing said twelve times. Cluster size is also the best available proxy for
what a rule change is worth, which is why the biggest group is what the model reads first.

`cluster_by` options:

| value | groups by | use when |
|---|---|---|
| `rule` | the rule id the reviewer cited, falling back to the expectation | default; rules are the thing you are editing |
| `expectation` | the specific expectation that failed | expectations map cleanly to distinct behaviours |
| `path` | the top-level directory | failures track subsystems more than rules |
| `none` | nothing; representatives are individual failures | small corpora where every failure is distinct |

### Template variables

`prompt.md` is rendered with `{{name}}` substitution. An unknown placeholder is an **error**, not an
empty string — a prompt saying `{{failurs}}` would otherwise render as literal text and the model
would cheerfully improve a skill it was shown nothing about.

| variable | is |
|---|---|
| `{{skill_id}}` | the skill's id |
| `{{guidance}}` | the current `SKILL.md` body |
| `{{failures}}` | the rendered cluster representatives |
| `{{failure_count}}` | how many failures there really were |
| `{{shown_count}}` | how many reached the prompt |
| `{{cases_total}}` | eval cases the skill has |
| `{{cases_scored}}` | eval cases the run actually scored |
| `{{recall}}` / `{{fp_rate}}` | the run's scores, or `n/a` |
| `{{wiki}}` | repo context for the files the run failed on |
| `{{instruction}}` | whatever `--instruction` passed, or empty |

`{{instruction}}` lets the template decide where a one-off steer is read. Leave it out and a passed
instruction is appended at the end instead — it is never silently dropped, because a flag that
sometimes does nothing is worse than no flag.

Whetstone supplies the output *structure* (body, rationale, targeted cases), so `prompt.md` only has
to say how to think about the change. Case ids the model returns are validated against the skill;
ones that do not exist are dropped and reported rather than becoming a `--targeted` flag that fails
the gate for the wrong reason.

### The escape hatch

Replace `prompt:` with `run:` and Whetstone invokes your program instead:

```yaml
run: ["python", "run.py"]
```

The digest arrives as JSON on stdin; print `{"body": ..., "rationale": ..., "targeted_cases": [...]}`
on stdout. The working directory is the step folder. A non-zero exit surfaces your stderr.

`prompt:` and `run:` are mutually exclusive. Use `run:` when a prompt genuinely will not do — the
declarative form is what most skills want, and it is the one people will copy.

---

## `update/step.yaml` — regenerating the repo wiki

Whetstone does not summarize repositories. This step invokes the generator you already run — a
LangChain openwiki, an internal doc pipeline — and takes responsibility for the part that is
Whetstone's job: checking the output is usable, indexing it so retrieval is deterministic, and
making sure the refresh retracts any gate passed against the old context.

```yaml
description: Regenerate the repo wiki from the openwiki generator.

run: ["openwiki", "build", "--repo", "{{repo}}", "--out", "{{out_dir}}"]
timeout_s: 900

index:
  - page: auth-service
    paths: ["src/auth/**"]
```

```bash
uv run whetstone skills update --skill skills/<id> --repo /path/to/source/repo
```

The generated wiki is **staged on `whetstone/skill/<id>`**, not written into your checked-out
folder — the same branch guidance edits go to, so the console and the CLI never disagree about what
this skill's content is. `--working-tree` writes the files out instead when you just want a look;
a wiki left only in the working tree is invisible to the console, which reads the branch first.

Substituted into `run`: `{{repo}}`, `{{out_dir}}`, `{{skill_id}}`. It is a **list of arguments,
never a string** — nothing is re-split on spaces and no shell is involved, so a path containing a
space works and a config value can never become two arguments.

### What your generator must leave in `{{out_dir}}`

```
pages/<name>.md    one markdown file per subject; the first `# heading` becomes its title
index.yaml         which source paths each page describes
```

`index.yaml`:

```yaml
source:
  generator: openwiki
  repo: git@gitlab:team/service.git
  revision: abc123f         # shown in the preflight, so a stale wiki is recognisable
pages:
  - page: auth-service
    paths:
      - "src/auth/**"
```

Two supported arrangements:

1. **Your generator writes `index.yaml` itself** — preferred. The tool that knows which source files
   a page describes is the tool that wrote the page. Delete the `index:` block from `step.yaml`.
2. **Your generator writes only pages** — declare the mapping under `index:` in `step.yaml` and
   Whetstone writes `index.yaml` for you.

A generator producing neither is an error naming both options. The alternative would be a wiki that
loads as empty and a reviewer that silently lost all its context.

---

## How the wiki reaches the reviewer

Retrieval is **by source path, not by meaning**. The index maps globs to pages, a change names files,
and the pages covering those files are injected. No embeddings, no similarity search.

That is not a shortcut. A gate compares base and candidate over the same cases, so if retrieval
could return different context on the two sides, a score difference would stop meaning what it is
supposed to mean. Path retrieval is a pure function of the diff.

- Globs use `**` for "any depth" and `*` for "one segment", so `src/auth/*` does **not** match
  `src/auth/nested/thing.rs`.
- Pages are ranked by how many of the changed paths each covers, ties broken by index order.
- The caps apply per review. Over the page cap, the excess is dropped and named. Over the byte cap,
  the most relevant page is truncated rather than dropped — half of the right page is context, none
  of it is not.
- Wiki text is injected **after** the guidance, labelled explicitly as background and not as rules,
  so it cannot be read as guidance to apply.

### The wiki is part of `skill_hash`

Regenerating the wiki changes what the reviewer sees, so it **retracts a passing gate** and the
skill must be re-gated before it can be proposed. Without that, an `update` run could change the
reviewer's context while a stale gate still said the skill was safe to publish — exactly the hole
C6 exists to close.

A skill with no `wiki/` folder hashes exactly as it did before this feature existed, so landing it
invalidated no stored gate result.

Steps themselves are **not** hashed. They describe how to run things, not what the reviewer reads,
so editing a sample size does not retract a gate. The line is: does it change what the model sees
when it reviews?

---

## The loop, end to end

```bash
# 1. Score it. Records the run the improve step will read.
uv run whetstone eval run --skill skills/<id>

# 2. Draft a change from what it got wrong and stage it on the skill's branch.
uv run whetstone skills improve --skill skills/<id> --apply

# 3. Run the gate command it just printed, verbatim. It carries --targeted already.
uv run whetstone eval gate --repo . --skill-path skills/<id> \
  --base-ref main --candidate-ref whetstone/skill/<id> --targeted <case>

# 4. A passing gate is what unlocks Propose MR in the console.
```

Everything after step 1 addresses the skill by id and lands on one branch, so the gate evidence is
filed where C6 looks for it. Review what was staged with `git diff main whetstone/skill/<id>`, or
open the guidance editor in the console — it reads the same branch.

Check what a skill defines, and that all of it loads:

```bash
uv run whetstone skills steps --skill skills/<id>
```
