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
  installed collector) and cannot check the third.
