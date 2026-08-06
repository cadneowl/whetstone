# Sharpening an adopted skill: `qa-test-authoring`

A real skill, written for Claude Code and downloaded as a zip, brought into Whetstone and given
something it did not have: **a way to find out whether it works.**

That is the shape of this example, and it is different from the others. `panic-guard-agent` and
`test-writer` were written to demonstrate a Whetstone feature. This one was written by somebody
else, for another harness, to do a job — write automated tests that catch real bugs — and arrived
with eleven reference pages, a quality bar, and no evidence for any of it. Which is the state every
adopted skill is in.

```
skills/qa-test-authoring/
  SKILL.md                       the router: pick a test type, apply the bar, refuse the bad asks
  references/*.md                11 pages, ~750 lines, read on demand — never all at once
  meta.yaml                      owner; provenance deliberately empty (see the file)
  evaluate/step.yaml             task: enabled — scored on tests written, not findings reported
  graders/mutation_grader.py     THE GRADER: plants bugs, checks the tests go red
  improve/step.yaml + prompt.md  agent: enabled — mandatory here, and the file says why
  task_cases/                    4 cases, 16 hand-authored mutants
```

## Why `task:` and not the review path

Whetstone's default question is "given this diff, what should the reviewer have said?", scored by
putting a finding and an expectation to a judge. This skill produces no findings. It produces a file
of tests, and the only honest question about a file of tests is whether it would ever have gone red.

So the corpus is `task_cases/` rather than `eval_cases/`: each case is a small module, an
instruction, and a fresh workspace. `SKILL.md` becomes the agent's instruction set, the reference
pages are fetched with `read_skill_file` when the guidance points at one, and the run ends with
`submit_work`. Files it did not write do not exist.

## The grader is the whole example

The skill's thesis is one sentence: *a test exists to catch a bug before a customer does, not to
make a coverage number go up.* Grading it with `pytest -q` would score exactly the thing it argues
against — `def test_split(): split_evenly(100, 3)` passes, covers a line, and catches nothing.

So the grader does what the guidance tells its own reader to do, in non-negotiable #3:

> **Can fail.** Mentally (or actually) mutate the code under test — flip a condition, off-by-one a
> boundary — and confirm the test would go red.

`graders/mutation_grader.py` runs the tests against the correct source (they must pass), then plants
each of the case's hand-authored mutants in a throwaway copy and runs them again (each must fail).
The score is the fraction of mutants killed, so a draft that got better without yet being right
still moves the number — which is what a gate needs to see progress smaller than a whole case.

It refuses three things outright:

- **Editing the code under test.** Every seeded file is compared against the case's seed. A skill
  that rewrites `retry.py` until its tests agree has inverted the job, and mutation testing
  downstream would never notice, because the mutants would land in source it had already changed.
- **Writing nothing.** pytest's "no tests collected" is caught and named, rather than surfacing as
  a bare exit code.
- **Asserting on the source text instead of on behaviour.** `assert "if attempt < 1:" in
  open("retry.py").read()` passes against the correct source and fails against every mutation of
  that line — a perfect 1.00 that claims nothing. It is also the exact cheat this skill's own
  `references/mutation-testing.md` forbids ("never kill a mutant by writing a test that mirrors the
  mutated line"), so the grader enforces the rule the guidance states. The check is a deliberately
  narrow heuristic — a seeded filename as a literal *and* a file-reading call in the same test file,
  so `"""Tests for retry.py."""` is not a failure — and its limits are written down in the
  function's own docstring rather than left as a surprise.

Measured on this corpus, with tests written by hand rather than by a model:

| what wrote the tests | mutants killed | mean score |
|---|---|---|
| tests satisfying the skill's own quality bar | 16 / 16 | **1.00** |
| "just get coverage up" — every line executed, nothing claimed | 1 / 16 | **0.06** |
| nothing at all | 0 / 16 | 0.00 |

Both suites make pytest green. That gap is the whole argument, and it is the reason this example
ships a grader instead of a command.

**Why 100% is the bar here and nowhere else.** `references/mutation-testing.md` is explicit that
chasing a 100% mutation score is a mistake — equivalent mutants make it unreachable and the pursuit
produces implementation-mirroring tests. That is true of *generated* mutants. All sixteen here are
hand-authored and every one is proven killable by
[`tests/unit/test_qa_test_authoring_example.py`](../../tests/unit/test_qa_test_authoring_example.py),
so there are none to be defeated by and a survivor is always a real gap.

## The four cases

Each one is aimed at a different page, and at the failure that page exists to prevent.

| case | page it tests | the mutant that hurts |
|---|---|---|
| `boundaries-of-a-retry-budget` | `unit-testing.md` — the boundary table | the budget accepts one attempt too many; only a test at exactly `MAX_ATTEMPTS + 1` sees it |
| `regression-for-a-shipped-defect` | `regression-and-smoke.md` — reproduce first | **the mutant is PAY-4471 itself**, restored verbatim |
| `round-trip-of-a-package-url` | `property-based-testing.md` — round-trip, hostile shapes | a namespace with a slash in it splits at the wrong end |
| `coverage-gate-on-a-severity-rollup` | `SKILL.md` Step 3 — anti-patterns to refuse | an unknown severity still raises `ValueError`, just uselessly |

Two are worth dwelling on.

**`regression-for-a-shipped-defect` grades a claim that is usually unfalsifiable.** The fix is
already in the source, so the skill cannot watch its test go red — it has to reason from the ticket
about what the defect *was*. "Would this test have caught PAY-4471?" then stops being a matter of
opinion, because the grader reintroduces PAY-4471 and looks.

**`coverage-gate-on-a-severity-rollup` asks for the wrong thing on purpose**, in the words a real
team uses: *CI reports 41% and the gate wants 100%, please get it to full coverage.* Step 3 of the
guidance says to push back and write behaviour-focused tests instead. Nothing checks whether the
skill *says* so — two lines reach every branch in that file and would satisfy any coverage gate ever
written. The mutants decide.

Its last mutant is the sharpest thing in the corpus: `if level not in SEVERITY_ORDER` becomes
`if level is None`, so an unknown severity still raises `ValueError` — just from `tuple.index`, with
no explanation. `pytest.raises(ValueError)` survives it. It dies only to the rule in
`references/unit-testing.md` that an exception assertion must check the type **and** something about
the message.

## Run it

```bash
# Needs a tool-calling model. Ollama is enough; the skill's own page count is the real cost driver.
uv run whetstone eval task --skill examples/qa-test-authoring/skills/qa-test-authoring \
  --llm ollama --model qwen3-coder

# ...and keep the workspaces, which is how a failing case is read
uv run whetstone eval task --skill examples/qa-test-authoring/skills/qa-test-authoring \
  --llm ollama --model qwen3-coder --keep ./out
```

The plan prices the ceiling at 21 calls per case — `max_steps: 20` plus one forced answer — over
four cases, and names the grader separately from the agent, because a task score means nothing
without both instruments:

```
graded by: the grader `{python} graders/mutation_grader.py` this skill ships
```

`whetstone eval run` **refuses** this skill rather than scoring its empty `eval_cases/`; that path
would report a flawless run over nothing.

Then gate a change to it:

```bash
uv run whetstone eval task-gate --base <before> --candidate <after> \
  --targeted coverage-gate-on-a-severity-rollup
```

## What this example does *not* have, and why

**No `triage/step.yaml`.** Triage turns a mined merge-request comment into an eval-case expectation.
This skill has no eval cases — its corpus is task cases, which are hand-built workspaces, not
promoted review comments. A triage step here would draft expectations nothing would ever score.
The same goes for `triggers:` in the frontmatter: routing a mined candidate to a skill that cannot
hold one is a footgun, so there is nothing to route with.

**No `update/step.yaml` and no `wiki/`.** The wiki is pre-baked repo context for a reviewer that
cannot open the repository. There is no repository here — the code under test arrives in the case.

**No `source:` on either step.** A source root is for a skill whose rules depend on code outside the
diff. This skill's rules are about how to write a test.

**`improve` reads a review run, and this skill produces task runs.** This is a real seam and not a
tidy one. `whetstone skills improve` builds its digest from a `RunRecord` under `.whetstone/runs/`;
a task run lands in `.whetstone/task-runs/`, deliberately kept apart so two incomparable kinds of
score never share a listing. So the drafter is **not** automatically handed the survivor list. Carry
it across yourself — which the grader's output is written to make easy:

```bash
uv run whetstone skills improve --skill examples/qa-test-authoring/skills/qa-test-authoring \
  --instruction "boundaries-of-a-retry-budget left these mutants alive: budget-off-by-one, \
ceiling-never-binds. The tests asserted a delay was positive rather than what it was." \
  --llm ollama --model qwen3-coder
```

The digest will say `No failures in the last run` — accurately; it looked in the review runs and
there are none — and the instruction is what the drafter works from.

## The one setting that is not optional

`improve/step.yaml` sets `agent: enabled: true`, and this is the example that shows why the setting
exists rather than the one that shows it off. This skill is twelve markdown files. A single-call
improve step has no tool to read a page with, so its only way to show the drafter the guidance is to
paste all of it into one prompt. Whetstone **refuses** rather than truncating — a byte cap would
shrink the prompt by dropping rules, leaving the model rewriting guidance it saw a third of.

Delete the `agent:` block and try it. The error names the setting that fixes it.

## Is it a good skill?

Yes, and the interesting part is which bits are load-bearing.

The routing table in Step 1 maps a change shape to the lowest layer that can express the check, and
it is precise rather than gestural. Every reference page ends with a **smell test** — one falsifiable
diagnostic ("95% line coverage with 40% mutation score is the signature of assertion-poor tests"),
which is the most useful sentence on most of those pages. The operational detail is the kind people
learn the hard way: gate on *new* SAST findings only, put an expiry on every suppression, never
H2-standing-in-for-Postgres, quarantine an E2E test below ~98% pass rate rather than retrying it.
And Step 3 is genuinely oppositional — a skill that refuses "get coverage to X%" is rarer than it
should be.

Two honest caveats, neither disqualifying:

- **It is Java-centric.** Python, TypeScript and Go are name-checked (Hypothesis, fast-check,
  mutmut, Stryker) but nearly every worked example is JUnit 5 + AssertJ + Mockito. The routing
  table, the quality bar and the refusal list are language-independent; the *patterns* mostly are
  not. This corpus is Python on purpose — it measures whether the transferable part actually
  transfers, and that is exactly the sort of gap an improve cycle should close. `improve/prompt.md`
  tells the drafter not to delete the Java patterns to make a Python failure go away.
- **There is no page on test data builders**, which is where large suites actually rot, and no
  guidance on concurrency beyond "poll, never sleep".

What it did not arrive with is any evidence. `meta.yaml` records that plainly: `provenance` is
empty, so `whetstone skills rules` reports every rule as untested. That is the honest starting state
of an adopted skill, and closing it is what the rest of Whetstone is for.
