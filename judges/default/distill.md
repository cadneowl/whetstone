# Distilling a tier-1 judge

Judge calls are Whetstone's largest cost line: they scale as cases × trials × both gate sides,
and at thousands of cases they dominate a run. This recipe replaces the bulk of them with a small
local model fine-tuned to reproduce the trustworthy judge's verdicts — a *validated cache* of the
grounded judge's behavior, permanently re-validated by the meta-eval bar and permanently backed
by tier-2 escalation. The fine-tune itself happens outside Whetstone; everything before and after
it is machinery that already exists.

Do this only after the teacher is worth copying: the doctrine measured against the labeled corpus
(`whetstone judge eval`), the ratchet bar established, and ideally the cascade running so tier-2
grounded verdicts — the highest-grade labels — exist in your run records.

## 1. Export the training set

```bash
uv run whetstone judge export --out judge-triples.jsonl
```

One JSON line per judged (finding, expectation) pair, filtered to a single judge identity — by
default the identity your newest run recorded (`--judge-hash` to pick another; mixing judges
would distill an instrument nobody ever ran). Each triple carries what the judge saw (finding
message and location, expectation text and region, the case's diff hunk when the case still
exists on disk) and what it said (`matched`, `confidence`, `reason`, `tier`). Escalated verdicts
carry `prior` — what tier 1 got wrong before the grounded tier corrected it.

## 2. Fine-tune (outside Whetstone)

Shape each triple into a chat example whose system prompt is your doctrine (`JUDGE.md` body —
the student must run under the same words the identity hashes) and whose user message matches
the pairwise template: expected issue + location, reviewer finding + location. The completion is
the JSON verdict `{"matched": …, "confidence": …, "reason": …}`.

Practical notes, learned the expensive way:

- **Train toward the teacher's final verdicts.** For escalated rows, the label is the tier-2
  verdict; include the tier-1 `prior` rows as extra hard examples with the *corrected* label.
  Oversample them — they are precisely the calls the cheap judge gets wrong.
- **Hold out by case id, not by row.** The same case contributes near-identical rows across
  trials and runs; a random row split leaks them and flatters the eval.
- **Keep the output strictly JSON.** The `OpenAICompatibleClient` retries malformed JSON, but a
  student that needs retries is spending the latency you distilled to save.
- A 1–4B parameter base is plenty for a yes/no-with-reason task; serve it wherever you already
  serve embeddings (Ollama: `ollama create judge-distilled -f Modelfile`).

## 3. Validate against the ratcheted bar

```bash
uv run whetstone judge eval --llm ollama --model judge-distilled
```

This scores the candidate against every labeled pair the deployment has and exits non-zero below
the bar. The bar ratchets — once any judge demonstrated an accuracy over enough pairs, no later
one clears meaningfully below it — so a distilled model cannot lower the standard by being cheap.
Do not deploy a model that has not cleared this.

## 4. Deploy as cascade tier 1

In the skill's `evaluate/step.yaml`:

```yaml
judge:
  escalate_below: 0.8        # the distilled judge's unsure calls go to the grounded teacher
  tier1:
    llm: ollama
    model: judge-distilled
```

The reviewer and the grounded tier 2 stay on the run's own backend. The resolved tier-1 model
folds into every run's `judge_hash`, so trend lines break at the swap instead of drawing through
it, and the Judge page's escalation rate shows how often the student defers. Rollback is deleting
the `tier1:` block.

## 5. Record the outcome

Run one unsampled full-corpus eval before and after and record `llm_calls` × backend cost for
both in ANTI_ROT_PLAN.md's status block — the plan's done-when asks for the measured drop, not
the expectation of one. Watch the escalation rate: below ~5% the student has learned the easy
calls; above ~30% you are paying for both tiers on too many verdicts to have saved much.
