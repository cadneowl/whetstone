# Local context that lives with the code

The runnable counterpart to [`docs/design/sidecars.md`](../../docs/design/sidecars.md), and the
fixture the `--no-sidecars` ablation is measured on.

A review skill over a large codebase needs to know thousands of particulars — why the retry cap is
3, which component owns a table, which rule this one folder is excepted from. None of it fits in
`SKILL.md`. Here it lives in `.agents/` files committed beside the code it describes, and the
harness loads them from the paths in each diff.

```
source/                                       the "other repo" — what a review points at
  payments/
    .agents/context.md                        role-agnostic: the ledger invariants
    .agents/arch-review.md                    the role overlay, same folder
    gateway/.agents/arch-review.md            deeper and more specific
    reconciliation/.agents/arch-review.md     excepts R1, by name, for this package only
    refunds/processor.py                      no .agents/ — inherits from payments/
  notifications/.agents/context.md            excepts R3 for transport failures
  search/, ingest/                            no .agents/ anywhere — absence is normal
skills/hub-arch-review/
  SKILL.md                                    three general rules, and `sidecar: role: arch-review`
  evaluate/step.yaml                          `context: source_root:` — where the tree is
  eval_cases/                                 8 cases, in three deliberate groups
  tools/collect_sidecars.py                   the collector, installed verbatim for Claude Code
  tools/sidecar.json                          this skill's declaration, for the same caller
```

## Run it

The source root is machine-local, so it comes from the environment and is never committed:

```bash
# from the repository root
export HUB_ARCH_REVIEW_SOURCE="$PWD/examples/sidecar-review/source"   # Windows: $env:HUB_ARCH_REVIEW_SOURCE = "$PWD\examples\sidecar-review\source"

whetstone eval run --skill examples/sidecar-review/skills/hub-arch-review --llm ollama --model qwen3-coder:30b --trials 3
whetstone eval run --skill examples/sidecar-review/skills/hub-arch-review --llm ollama --model qwen3-coder:30b --trials 3 --no-sidecars
```

Leave the variable unset and the run is refused **at the plan**. That is deliberate: an unresolvable
source root must never degrade to an empty sidecar set, because an empty set produces a
valid-looking `context_hash` over context that was never read, and forks gate results by whose
machine ran them.

Before spending anything, see what a given path would pull in:

```bash
whetstone sidecars show --skill examples/sidecar-review/skills/hub-arch-review --path payments/gateway/stripe.py
```

## The three groups of cases

The corpus is built so the ablation can distinguish three different things.

**Sidecar-dependent catches** — the fact is not in the diff and not in `SKILL.md`.
`retry-cap-raised` is the sharpest: R2 asks for a bounded retry with a named cap, and raising
`MAX_RETRIES` from 3 to 6 *is* a bounded retry with a named cap. Nothing in the guidance objects.
Only `payments/gateway/.agents/arch-review.md` knows 3 is a ceiling. `ledger-second-writer` writes
the ledger through the repository — R1 is satisfied — and is caught only because a sidecar one
directory up says `PaymentService` owns that table.

**Sidecar-dependent silences** — the false-positive direction, which no central rule can express.
`batch-reads-directly` is inline SQL outside the repository layer, and R1 says flag it; the
reconciler's sidecar excepts R1 by name for that package. Today the only way to stop the reviewer
flagging it is to soften R1 for the whole codebase, which is how a rule set rots.

**Controls** — `handler-builds-sql` and `unbounded-poll-retry` are plain rule violations in folders
with no `.agents/` anywhere on their path, and `repository-runs-sql` is a negative in the same
position. They are what make the number mean something: without them, a recall gain and a reviewer
that simply got better are indistinguishable. A test keeps their paths bare.

## What the ablation measured

`qwen3-coder:30b` via Ollama, k=3, all 8 cases:

Two runs of each arm, shipped defaults:

| | recall | fp_rate |
|---|---|---|
| sidecars on | **0.733**, 0.733 | 0.444, 0.444 |
| `--no-sidecars` | **0.400**, 0.533 | 0.000, 0.222 |

**Recall goes up, and false positives go up with it.** Neither half is noise. The recall gain lands
entirely on the two sidecar-dependent catches — `ledger-second-writer` and `retry-cap-raised` both
go from 0.00 to 1.00 — and the false-positive cost lands almost entirely on one case, for one
understood reason, below.

Read the messages, not just the score. With sidecars the reviewer says *"violates the documented
cap of 3 retries for the card processor... requires a contract change"* and *"bypasses idempotency
checks"* — reasoning that appears in no file but the sidecar. Without them it says the cap increase
*"may indicate a lack of proper retry logic"*, and on `ledger-second-writer` it says nothing at all.

**This is a mechanism test, not evidence about your codebase.** The sidecars and the cases were
written together, so the direction of the recall result is not a surprise and should not be quoted
as one. What it establishes is that the wire is connected end to end: the text reaches the model,
the model reasons from it, and the two arms record different digests so neither can reuse the
other's baseline. The number that decides whether the tier is worth its tokens has to come from a
real corpus with sidecars written by the people who own the code.

## Two costs it found that the design did not predict

**Concurrence findings.** Given an exception, the reviewer reports a finding whose message says the
code is *fine* — "increments the counter, which aligns with the documented exception for R3" — and
that is scored a false positive, correctly: the review spoke where it should have been silent.
`notification-drop-counted` produces it in 3 of 3 trials, and it is most of the false-positive gap
in the table above. From the score alone it is indistinguishable from the reviewer *disagreeing*
with the sidecar, which is a different bug with a different fix; only the message tells them apart.

`_sidecar_block` now says that honouring an exception means reporting nothing. That instruction is
**not** known to be sufficient — this model produced the concurrence finding with and without it.
The case stays so the behaviour is measured rather than assumed.

**Asking for claim confirmations costs recall.** `sidecars.md` §8 argues the confirmation loop is
close to free, because the run already holds both the sidecar and the code. True of tokens, false
of attention:

| `sidecar: confirmations:` | recall |
|---|---|
| `false` — the default | 0.733, 0.733 |
| `true` | 0.600, 0.600 |

Two runs each, identical both times, and the case it loses is `retry-cap-raised` — the
sidecar-dependent catch the tier exists for. So the field defaults to **off**, and it sits in the
hashed declaration, so turning it on retracts baselines rather than quietly changing what was
measured. Turn it on where you have measured that your model absorbs the extra question.

## The rest of the loop, against this same tree

**Where claims come from.** Triage has three destinations that write, not one. `rule` is the old
behaviour. `context` and `exception` also file a claim beside the code — and produce a *patch*,
never a write: the file belongs to the reviewed repository and Whetstone holds no credentials there.

```bash
# a claim's target folder is the parent of the case's path, so this lands in
# payments/reconciliation/.agents/ — and the eval case is still written either way
whetstone sidecars show --skill examples/sidecar-review/skills/hub-arch-review --path payments/reconciliation/job.py
```

**Keeping them honest.** The maintainer sweep reads a folder and writes an account of what a reader
would need to be told — **without seeing the claims** — and only then compares. One call asking "is
this still true?" would anchor and confirm.

```bash
whetstone sidecars verify --skill examples/sidecar-review/skills/hub-arch-review --llm ollama --model qwen3-coder:30b
whetstone sidecars claims --disputed
```

Change `MAX_RETRIES` to 6 in `source/payments/gateway/stripe.py` and re-run the first command: both
claims in that folder come back `contradicted`, cited against the line that moved. Nothing is
rewritten — confirmation is automatic, correction is a human's.

**The floor.** Everything decidable without a model, cheap enough for a pre-commit hook:

```bash
whetstone sidecars check --root examples/sidecar-review/source           # exits 1 on any problem
git diff | whetstone sidecars check --root . --patch -                   # agents may not write claims
```

## Notes on the ablation itself

`--no-sidecars` shows the reviewer the same *absence* line a genuinely bare folder gets ("no
`.agents/` notes, that is normal and complete"), not a note that context was withheld. That is the
counterfactual worth measuring — what a reviewer without this tier would see — rather than a third
state that exists nowhere in production.

The two arms carry different `reviewer_context_digest`s, because `enabled` is part of the hashed
declaration. An ablation run therefore can never be mistaken for a normal one or reuse its baseline.
