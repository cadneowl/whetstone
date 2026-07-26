# The sharpening demo

**Does Whetstone actually make a skill better?** This answers that with a number, on one laptop, in
about a minute of model time.

```bash
uv run python examples/sharpening-demo/demo.py --plan       # print the commands, spend nothing
uv run python examples/sharpening-demo/demo.py              # Anthropic (ANTHROPIC_API_KEY)
uv run python examples/sharpening-demo/demo.py --llm ollama --model qwen2.5-coder:7b
```

Every command is printed before it runs, so the transcript is also the runbook you adapt for a repo
this script never sees.

---

## Read this before you run it

**A real model is required. Practice mode cannot demonstrate improvement.**

`PatternReviewer.review(skill, change)` ignores its `skill` argument — it matches regexes fixed when
it was constructed. So in practice mode, editing `SKILL.md` cannot move the score by even 0.01. Only
`LLMReviewer` puts the guidance into the prompt. An offline run of this demo would draw a flat line
and prove nothing, which is worse than not running it.

Cost is roughly **24 model calls** for the whole thing. On a small local model that is free and slow;
on a cloud model it is cents.

---

## What it does

It builds a throwaway git repo containing `demo-rust-errors`, a skill whose v1 guidance is
**deliberately narrow** — one rule, naming `.unwrap()` and nothing else:

> **R1 — no `.unwrap()` in service code.** … Replace it with `?` and a mapped error.

Against four eval cases, that guidance has three holes, and each one is a different kind of failure:

| Case | Kind | Why v1 should fail it |
|---|---|---|
| `unwrap-in-handler` | should catch | — (this one it *should* get) |
| `expect-in-handler` | should catch | guidance names `unwrap`, so a literal reading has no reason to object to `.expect()` |
| `swallowed-error` | should catch | no rule about discarded `Result`s exists at all |
| `unwrap-in-test` | should **not** flag | R1 says "service code" but never says what that excludes, so `#[test]` gets flagged |

The first three are **recall** holes; the last is a **precision** hole. v2 fixes all four by changing
nothing but the prose: it names `.expect()` alongside `.unwrap()`, adds a swallowed-error rule, and
says test code is exempt.

Then it gates v2 against v1 and prints the difference.

## What you're looking for

Step 2 prints the baseline. Step 4 prints the comparison, and this is the whole point:

```
PASS
  recall  0.33 -> 1.00
  fp_rate 1.00 -> 0.00
  fixed: expect-in-handler, swallowed-error, unwrap-in-test
```

Exact numbers depend on the model. What matters is the **direction**, and that nothing changed
except English prose in a markdown file.

Step 5 imports a review produced somewhere else — two findings, one ruled correct and one ruled a
false positive — and both rulings become eval cases in the triage queue. That is the other half of
the loop: the corpus growing from real review output rather than from hand-written fixtures.

## If the numbers don't move

That is a result, not a broken demo. The likely causes, in order:

- **The baseline already scored 1.00.** The model applied its own knowledge of Rust instead of only
  the guidance. Real, and worth knowing: it means your skill's guidance is doing less work than you
  think, and the eval is measuring the model rather than the skill. Make v1 narrower, or use a
  smaller model.
- **The gate FAILed.** v2 lost something v1 caught. Open the run in the console and look at the
  regressed case — this is exactly the thing the gate exists to stop, working correctly.
- **Both sides scored 0.00.** The model is not returning structured findings. Check
  `whetstone llm check`.

## Then look at it

```bash
cd examples/sharpening-demo/workspace
uv run whetstone ui
```

- **Reviews** — the imported review, its two rulings, and the notes that justify them.
- **Triage** — the two candidates those rulings minted, ready to promote onto a batch branch.
- **Skills → demo-rust-errors → Runs** — the baseline, with per-case drill-down.
- **Skills → demo-rust-errors → Edit** — the guidance editor, and the gate verdict that decides
  whether *Propose MR* is enabled.

---

## Adapting it to your own repo

The demo's structure is the thing to copy, not its content. On a machine with your real skills:

**1. Establish a baseline before changing anything.**

```bash
whetstone eval run --skill path/to/your-skill
```

If this is under ~0.6 recall, your eval set is probably measuring the wrong thing, or is too small
to measure anything. Fix that before touching guidance — a gate over three cases will pass almost
any change.

**2. Get real cases in, cheaply.** Two routes, and the second is usually faster to a first result:

```bash
# mine history — bounded hard the first time, or it walks your entire MR history
whetstone corpus pull --base-url … --project … --since 2026-06-01 \
  --out candidates --skills-root skills --max-clean-files 0

# or: post a review your own harness already produced, with your assessment of each finding
whetstone review --import review.json
```

`--max-clean-files 0` matters. Without it every comment-free merge contributes up to five weak
`should_not_flag` candidates and the queue fills with diffs nobody commented on.

`review.json`'s shape is in this directory, and documented under *Uploading a review run elsewhere*
in the main README. Your explanation of **why** a finding was right becomes the eval case's
expectation — that is the field worth spending a sentence on.

**3. Change one thing, and gate it.**

```bash
whetstone eval gate --repo path/to/skills-repo --skill-path skills/your-skill \
  --base-ref main --candidate-ref your-branch \
  --targeted the-case-you-are-trying-to-fix
```

`--targeted` is what turns "did not regress" into "actually fixed the thing" — without it a change
that improves nothing still passes.

**4. Iterate on the guidance, not the cases.** If a change fails the gate, the interesting question
is which case regressed and why. Editing the case to make the gate pass is how an eval set stops
meaning anything.
