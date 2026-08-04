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

| | recall | fp_rate | F2 |
|---|---|---|---|
| sidecars on | **0.800** | 0.333 | 0.800 |
| `--no-sidecars` | **0.400** | 0.333 | 0.435 |

Recall doubles. The whole gain is on the three sidecar-dependent catches (2 of 3 caught, 0 of 3
without); both controls score 1.00 in **both** arms, so nothing was diluted. False-positive rate
does not move — sidecars fix one negative case and break another, which is the next paragraph.

Read the messages, not just the score. With sidecars the reviewer says *"violates the documented
cap of 3 retries for the card processor... requires a contract change"* and *"bypasses idempotency
checks"* — reasoning that appears in no file but the sidecar. Without them it says the cap increase
*"may indicate a lack of proper retry logic"*, and on `ledger-second-writer` it says nothing at all.

**This is a mechanism test, not evidence about your codebase.** The sidecars and the cases were
written together, so the direction of the result is not a surprise and should not be quoted as one.
What it does establish is that the wire is connected end to end: the text reaches the model, the
model uses it, the controls do not degrade, and the two arms record different digests so neither can
reuse the other's baseline. The number that decides whether the tier is worth its tokens has to come
from a real corpus with sidecars written by the people who own the code.

## What it found that the design did not predict

`notification-drop-counted` is a **concurrence finding**: given an exception, the reviewer reported
a finding whose message says the code is *fine* — "increments the counter, which aligns with the
documented exception for R3" — in 3 of 3 trials. It is scored a false positive, correctly: the
review spoke where it should have been silent. From the score alone it is indistinguishable from
the reviewer *disagreeing* with the sidecar, which is a different bug with a different fix.

`_sidecar_block` now says that honouring an exception means reporting nothing. That instruction is
**not** known to be sufficient — this model produced the concurrence finding with and without it.
The case stays so the behaviour is measured rather than assumed.

## Notes on the ablation itself

`--no-sidecars` shows the reviewer the same *absence* line a genuinely bare folder gets ("no
`.agents/` notes, that is normal and complete"), not a note that context was withheld. That is the
counterfactual worth measuring — what a reviewer without this tier would see — rather than a third
state that exists nowhere in production.

The two arms carry different `reviewer_context_digest`s, because `enabled` is part of the hashed
declaration. An ablation run therefore can never be mistaken for a normal one or reuse its baseline.
