# Agentic reviewer example — a reviewer that reads the source

Most Whetstone skills score with the **built-in reviewer**: one LLM call given the guidance and the
diff. This example shows the other path — a skill whose `evaluate` step names its **own reviewer
program**, which reaches outside the diff into the actual source tree to decide.

See `docs/design/agentic-reviewers.md` for the full design; this is the working, runnable version.

## What it does

The skill `panic-guard-review` flags a change that calls a function the codebase documents as able
to **panic** (its source docstring says `PANICS`). Whether a call is dangerous depends on the
*called* function's definition, which lives in the source, not the diff — so the reviewer opens the
source to find out.

```
skills/panic-guard-review/
  SKILL.md                      the guidance (human-facing; the program decides how to use it)
  conventions.md                a committed reference, passed as a `file` context value
  evaluate/
    step.yaml                   run: [python, reviewer.py]  +  the context: bag
    reviewer.py                 THE reviewer — reads context.source_root, returns findings
  eval_cases/
    calls-panicky-fn/           should_catch: adds `load_config()` (source says it PANICS)
    calls-safe-fn/              should_not_flag: adds `safe_get()` (source says it is safe)
source/
  lib.py                        the "repo" the reviewer reads — NOT shown to it as a diff
```

## The mechanism

`evaluate/step.yaml` has a `run:` and a `context:` bag:

```yaml
run: ["python", "reviewer.py"]
context:
  source_root: { env: PANIC_GUARD_SOURCE, required: true }   # where the source is (per machine)
  conventions: { file: ./conventions.md }                     # committed, hashed by content
  project: hub-backend                                        # a literal
```

Whetstone resolves that bag (environment / file / literal), refuses the run at the plan if a
`required` var is unset, and forwards it to `reviewer.py` on stdin along with the diff:

```jsonc
{ "guidance": "...", "diff": "...numbered diff...", "change": { ... },
  "context": { "source_root": "/…/source", "conventions": "...", "project": "hub-backend" } }
```

`reviewer.py` opens `source_root`, learns `load_config` and `open_ledger` are `PANICS`, sees the
diff call `load_config()`, and returns one finding on stdout:

```json
{ "findings": [ { "path": "app/config.py", "line": 2, "severity": "warning",
                  "rule_id": "PG1", "message": "`load_config()` can panic — lib.py documents it PANICS. …",
                  "confidence": 0.9 } ] }
```

That is the entire contract. Whetstone scores and gates those findings exactly as it does the
built-in reviewer's — a custom reviewer is named in the run record, and nothing else changes.

**The judgement comes from the source, not the diff.** Delete the `PANICS` line from
`source/lib.py` and the same `calls-panicky-fn` diff scores clean — proof the reviewer looked beyond
the change. `tests/unit/test_agentic_example.py` asserts exactly that.

## Run it

```bash
cd ui && npm install && npm run build     # once: build the console
uv run python examples/agentic-reviewer/serve.py
```

Open the skill, run its evals, and read the cost plan before it runs: it names the reviewer
(`subprocess: python reviewer.py`) and shows the resolved context (`source_root` as `<env:…>`,
`project`, `conventions` as `<file:…>`). Run `--no-source` to watch `required: true` refuse the run
at the plan.

The stub model here answers only the **judge** — the reviewer is your program. So this is the
production path end to end: real resolution, real preflight, real scoring, a real subprocess.

## Writing your own

1. Add `run: ["your", "program"]` to `evaluate/step.yaml` (no `prompt:` — the reviewer prompt is the
   harness's).
2. Declare whatever your program needs under `context:` — `{ env: NAME, required: true }` for a
   machine path or secret (commit the *name*, never the value), `{ env: NAME, pin: true }` for a
   pinned ref that should be recorded and (in a later phase) hashed, `{ file: ./x }` for committed
   material, or a bare literal.
3. Read the JSON payload on stdin, do your work (read the source, call your own model with tools,
   whatever), and print `{"findings": [ … ]}` on stdout. A non-zero exit or bad JSON fails the run.

**Determinism note.** A gate blames a score change on the guidance, assuming everything else the
reviewer saw was identical between the two runs. A reviewer that reads a *moving* source can violate
that, so the console warns when a custom reviewer is gated; pin the source to a fixed snapshot
(`{ env: …, pin: true }`) for gated runs. See the design doc's §6.
