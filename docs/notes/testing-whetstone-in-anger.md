# Note for whoever is running Whetstone on a real deployment

You are on the machine where Whetstone reviews an actual codebase with actual skills. That makes you
the only one who finds a particular class of bug. This note says what just changed, what to check
first, and — the useful part — *where the bugs are*, based on the two you have already surfaced.

Plain language on purpose. Written 2026-08-09, against `main` at `0dccb36`.

---

## 1. Pull first, and drop one local patch

Three things landed:

| PR | What |
|---|---|
| #60 | Sidecar graph: URL-backed position, breadcrumb, centre/up, claim verdicts drawn on the map |
| #61 | `self_collected: true` — sidecars visible for a skill reviewed by its own agent |
| #62 | Transcripts no longer crash agent-mode runs |

**Do not merge the `managed: true` patch.** It was the right idea and #61 is the same feature built
against a different seam. If you still have it locally, drop it and use `self_collected: true`.
The refusal message names the flag now, so a skill that hits it tells you what to do.

Why `managed` was replaced rather than fixed: it bound a `SidecarPlan`, which is the object that
*injects* context and supplies a run record's provenance. Five things downstream then said false
sentences — the worst being `--no-sidecars`, which reported an ablation that withheld nothing and
left the two runs indistinguishable by digest. `SidecarView` has no `loader()` and no `provenance`,
so those sentences are now unwriteable rather than merely unwritten. Details in
`docs/notes/self-collected-sidecars.md`.

## 2. Transcripts work in agent mode now — and the file format changed

Before #62, `[runs] transcripts = true` killed every agent-reviewed skill at its first model call
with `AttributeError: 'RecordingClient' object has no attribute 'converse'`. If you had recording on,
that is why nothing ran.

The transcript now contains **two kinds of line**. Tell them apart by the `first` field:

- **`Exchange`** — one `structured` call. `system`, `user`, `response`. Unchanged.
- **`AgentTurn`** — one `converse` call. Has `first`, `total`, `messages`, `tools`, `force_tool`.

An `AgentTurn` carries only the messages *that turn added*, not the whole conversation. To read a
conversation, fold the file forward:

```python
folded = []
for line in lines:                       # in file order
    if "first" not in line:              # an Exchange, not an agent turn
        continue
    assert line["first"] == len(folded)  # anything else is a gap — say so, do not paper over it
    folded.extend(line["messages"])
```

`system` appears in full only on a conversation's first turn (`first == 0`) because it does not
change within one. This is not premature cleverness: recording the whole history per turn wrote
435 KB for 63 KB of content on a 12-step agent, and that file is the source code of the repo under
review.

## 3. Where the bugs are

Both bugs you have found so far are the same shape, and it is worth naming because it predicts the
next one.

**Whetstone has three ways to review a skill — built-in, agent, program — and the built-in path is
the one everything is tested and worded against.** Your deployment uses agents. So:

- 15 of 126 test files touch agent mode at all.
- There are 12 places in `src/` that branch on which kind of reviewer is running.
- Every one of those is a place where "works for built-in, broken or lying for agent" can live, and
  a fully green suite will not tell you.

`grep -rn "choice.agent is not None\|choice.task is not None\|\.agent is None" src/whetstone` is the
list. When something behaves oddly on an agent skill, start there.

### The specific traps

**Client decorators that implement part of the protocol.** `RecordingClient` and `CountingClient`
both wrap a client. Neither has a `__getattr__` passthrough — deliberately, because for a recorder a
silent passthrough means the next protocol method works fine and goes unrecorded, leaving a file
that looks complete and is not. The cost of that choice is that each decorator must be taught each
call.

`RecordingClient` now knows `structured` and `converse`. **`CountingClient` still only knows
`structured`**, and that is correct *today* only because it never wraps an agent — `service.py`
says so explicitly ("an agent spends the run's backend on its own client, which `counted` never
sees"). If anyone ever routes an agent through `CountingClient`, it will fail exactly as
`RecordingClient` did. Nothing enforces this. If you see that AttributeError again with a different
class name, this is why.

**Sentences that are true for one configuration.** The cost plan, the Sidecar tab, the Guidance tab
and the setup panel all describe *who reads what*. Several of them said "the harness injects this"
over a skill that collects its own. When you read a claim on screen, ask which reviewer kind it was
written for. Wrong prose here is not cosmetic — the cost plan is the egress disclosure an operator
decides on before spending.

**`--no-sidecars` on anything self-collecting** is refused, on purpose. It would withhold nothing.

## 4. What makes a fix land here

The suite is 2286 tests and the bar is not "it passes".

- **Reproduce before fixing.** Both recent bugs had a five-line repro. Write it, watch it fail, then
  fix. A fix without a demonstrated failure is a guess.
- **Mutation-test your test.** Break the fix again and confirm the new test fails. A test that
  passes against the bug is worse than none — it certifies the gap. For #62 I checked two mutations:
  removing `converse` (7 tests fail) and recording the whole history (4 fail).
- **`mypy` has a baseline of 16 errors** in 6 files, all pre-existing. Your change should leave it
  at exactly 16 — not zero, and not 17.
- **`ruff check src tests` must be clean**, and line length is 100.
- **UI:** `npx tsc --noEmit`, `npx vitest run`, `npx prettier --check <files>`, `npm run build`. If
  you touched a route or a response model, run `npm run gen:api` — `schema.d.ts` is generated and
  has silently lagged before.
- **Run it for real.** Ollama on `127.0.0.1:11434` is enough for an end-to-end leg and costs
  nothing: `WHETSTONE_LLM=ollama WHETSTONE_LLM_MODEL=qwen3-coder:30b whetstone eval run --skill …
  --transcript --yes`. Note that `qwen2.5-coder:14b` emits tool calls as text and fails with
  `ToolsUnsupported` — that is the model, not your change.
- **Say what you did not verify.** A green suite is not a working feature; that is the whole lesson
  of these two bugs.

## 4b. Sidecars are now wired through the whole console

Five gaps closed. The short version of each, and what to look at when it misbehaves:

| What | Where it shows |
|---|---|
| Agent runs record what they read | Case page → **Local context**; `CaseSidecars.resolved_by` |
| The improve step is shown the notes | Improve tab → *Show prompt*; the `{{sidecars}}` block |
| The drafter can dispute a claim | Job log on an improve run; `whetstone sidecars claims --disputed` |
| The maintainer sweep runs from the console | Sidecar tab → **Verify claims** |
| Mechanical defects are drawn on the graph | Sidecar tab → the **N with defects** badge; click it to filter |
| A lesson goes to the folder or to the guidance | Improve job log; `--sidecar-patches <dir>` on the CLI |

**The routing is the part to watch.** A skill with sidecars has two places a lesson can live, and
the drafter now picks per lesson: a fact about one folder becomes a proposed claim, anything true
everywhere stays guidance. Watch for two things:

- **`MISROUTED` in the log.** It means the new guidance names a folder the old one did not — a rule
  softened to fit one place, which is weaker in every place. Read that diff before accepting it.
  A weak model does this often; it is the commonest way to get the split wrong.
- **Claims are patches, not writes.** They land nowhere until a person applies them. On the CLI,
  `--sidecar-patches <dir>` writes one `.patch` per claim for a PR against the *source* repo.

Verified live with a local model: a false positive on a handler behind an auth gateway produced
`api/handlers/.agents/arch-review.md` with `Excepts R4`, cited to the case that fails without it,
and `git apply --check` accepted it. The same run left R4 itself untouched, which is the point.

**`resolved_by` is the one field to understand.** `harness` means the built-in reviewer and the set
is exhaustive and hashed. `reviewer` means an agent — then it is what the reviewer was *seen* to
open, `context_hash` is empty on purpose, and the path list is a **lower bound**. Your deployment
is all agents, so every record you write is the second kind. A case page reading *"the reviewer
opened none of the notes"* is a real answer, not a bug: it is the diagnosis that was impossible
before, and on our test run it was correct — the agent spent its whole step budget without opening
anything.

**Disputes are filed, never written.** §7 forbids a skill maintaining the sidecars it later reads.
A drafter that spots a wrong claim files it to the ledger and a human promotes the correction. A
claim must be quoted verbatim or it is dropped *and reported* — on our live run the model
paraphrased, and the log said so rather than filing a claim nobody wrote.

## 5. Things known to be missing

Not bugs — decisions, so you do not spend time rediscovering them:

- The sidecar graph has **no zoom or pan**. Above roughly 60 nodes it is hard to read. Yours is
  around 78, so this will bite; it is the largest known gap.
- Above 14 nodes, **only folders and the selected node are labelled**. Hover gives any dot's
  identity. This is deliberate — labels overlap into mush otherwise — but it means a dense graph
  reads as anonymous dots until you hover.
- The **detail card renders below the result list**, which at 78 matches is a long way down. Double
  clicking a node centres it without needing the card.
- **Nothing verifies that a `self_collected` reviewer actually calls the collector.** The flag is
  the author's claim. Whetstone checks the two ways it can be false in its own terms (no tree, no
  installed collector) and cannot check the third. The case page's **Local context** is now the
  closest thing to a check: a reviewer that never opens an `.agents/` file shows an empty observed
  set, which is evidence, not proof — a reviewer that shells out reads nothing we can see.
- **Claim disputes from the improve step are not on a screen of their own.** They land in the
  ledger and surface on the Sidecar tab's claim panel and on the graph, mixed in with what the
  sweep and the consuming runs filed. `whetstone sidecars claims --disputed` is still the queue.
