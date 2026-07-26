# The console demo

**Run the whole of Whetstone, with mock data, offline, for free.**

```bash
uv run python examples/console-demo/serve.py
```

That builds a throwaway skills repo under `workspace/`, starts a stub model on port 8789, and opens
the console on <http://127.0.0.1:8790>. No API key, nothing to spend, nothing reaching the network.

| flag | |
|---|---|
| `--keep` | reuse last run's workspace instead of rebuilding it |
| `--port 8790` | console port |
| `--model-port 8789` | where the stub model listens |
| `--no-open` | don't open a browser |

Delete `workspace/` (or just re-run without `--keep`) to start over.

---

## What you get

Three skills, parked at three different points in the loop — which is also the order the inbox
puts them in:

| skill | state | next action |
|---|---|---|
| `python-service-errors` | three signals mined from merge requests, nobody has ruled on them | **triage** |
| `sql-migration-safety` | never measured | **score** |
| `rust-error-handling` | measured, failing 3 of 4 cases | **improve** |

Plus one signal that matches no skill's `triggers.paths` (a Go file), so the unrouted counter has
something in it, and one review of a live merge request with two findings and no rulings yet.

The two baselines are **real runs**, not fabricated records — the seeder scores those skills through
the same `record_eval` the console's buttons call, so the per-case drill-down and the staleness
check describe something that actually happened.

## The five-minute tour

1. **Inbox → Rust error handling → Draft a change.** It states why first: *failing 3 of 4 scored
   cases: expect-in-handler, swallowed-error, unwrap-in-test*. The draft lands in the editor and the
   right-hand pane shows a **diff**, not a preview — three rules added, nothing removed.
2. **Stage on branch**, then **Run the gate**. Both sides are scored; recall goes 0.33 → 1.00 and
   fp_rate 1.00 → 0.00. `Propose MR` turns on, and the proposal card names the gate that cleared it.
3. **Propose MR** pushes to `workspace/origin.git` — a bare repo standing in for GitLab. It reports
   honestly that it cannot open the merge request itself.
4. **Inbox → SQL migration safety → Run evals.** It scores 0.5: it catches the un-`CONCURRENTLY`
   index and misses the `NOT NULL` column with no default. Now it has a baseline, and the inbox
   switches it to *Draft a change*.
5. **Inbox → Review 3 signals.** Three merge requests where a reviewer asked for `raise … from exc`.
   Promote one and watch it land in `eval_cases/` as a case the gate will enforce from then on.
6. **Reviews.** The skill's own output on a live merge request, two findings, neither ruled on. Mark
   one wrong and it mints a triage candidate — the loop closing from the other direction.

Everything that touches the model shows what it will cost before it starts, and the banner is
honest about this endpoint: *demo-stub — Whetstone cannot tell whether this bills*. It cannot,
because a custom endpoint is exactly the case it has no way to know about.

## The stub model, and what it is not

`stub_model.py` speaks the OpenAI chat-completions API, so Whetstone reaches it through
`build_llm_client` like any other backend — the real client, the real retries, the real preflight.
Nothing is monkey-patched, and no code path here is one a real deployment does not use.

**It reads the guidance.** That is the whole reason it exists rather than practice mode:
`PatternReviewer` matches fixed regexes and ignores the skill entirely, so editing `SKILL.md` cannot
move its score by 0.01 — a demo built on it would draw a flat line and prove nothing. This stub
fires a rule only if the guidance asks for it. So editing guidance in the console genuinely changes
the next run's score, and you can go and try it: open **Edit** on any skill, change a rule, stage,
and re-score.

What it keys on, so the reactions are predictable rather than magic:

| it flags | when an added line matches | if the guidance mentions |
|---|---|---|
| `R1` | `.unwrap(` | `unwrap` |
| `R1` | `.expect(` | `expect` |
| `R2` | `let _ =` | `swallow` / `discard` / `ignored` / `let _` |
| `P1` | `except …:` followed by `pass`/`return`/`continue` | `swallow` / `silently` / `discard` / `bare except` |
| `P2` | `raise SomethingError(…)` with no `from` | `` `from` `` / `chain` / `traceback` / `original exception` |
| `S1` | `CREATE INDEX` without `CONCURRENTLY` | `concurrently` / `lock` / `index` |
| `S2` | `ADD COLUMN … NOT NULL` without `DEFAULT` | `not null` / `default` / `backfill` |
| `S3` | `DROP COLUMN` | `drop column` / `still reads` / `expand` |

Rules marked test-exempt (`R1`) go quiet in test code — `*_test.rs`, `tests/`, anything containing
`#[test]` — but only once the guidance says test code is exempt: it needs a test token (`test code`,
`#[cfg(test)]`, `tests/`, …) **and** an exemption token (`does not apply`, `exempt`, `idiomatic`, …).
That pairing is what the third failing Rust case is about.

For **improve**, the stub only ever appends. A real model's rewrites are exactly where guidance
quietly loses rules, which is what the diff pane exists to catch — to see that warning, paste a
shorter body into the editor yourself and watch it say *this removes far more than it adds*.

**It is not a model.** It cannot generalise, it has no opinion about code it has no rule for, and a
score it produces is evidence about the stub. For evidence about a skill, point `WHETSTONE_LLM` at a
real backend — see `examples/sharpening-demo/` for that, which needs one.

## What the demo cannot show

- **Watching.** `[watch]` is off with no projects, because there is no forge to poll. *Check now*
  says so rather than inventing merge requests. Point `[watch] projects` and `gitlab_url` at a real
  GitLab and the inbox fills itself on a timer.
- **Whether your skills improve.** That needs your skills, your review history, and a real model.
