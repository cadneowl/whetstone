# Note: `self_collected: true` — sidecars on a skill that reviews itself

Written for whoever picks this up next, on another machine, when something breaks. Plain language on
purpose. The design reasoning is in `docs/design/sidecars.md`; this is the operating manual.

## The problem it solves

A skill can be reviewed in three ways: by Whetstone's built-in reviewer, by its own **agent**, or by
its own **program** (`run:` in `evaluate/step.yaml`).

Sidecars are `.agents/context.md` and `.agents/<role>.md` files that live next to the code in the
repo being reviewed. For the built-in reviewer, Whetstone finds them itself and pastes them into the
prompt. That is called **injection**, and everything injected is hashed into the run's
`reviewer_context_digest`, so a passing gate can be tied to the exact context that produced it.

An agent or a program picks its own reads. Whetstone cannot hash what it did not resolve. So the old
rule was: if a skill declares a `sidecar:` role **and** is reviewed by its own agent or program, that
is **refused at the plan**. The refusal was right, but it was too wide — it also refused to *show*
you the files. Reading is not hashing. On a real deployment that meant the Sidecar tab, the graph and
`whetstone sidecars ...` were all dark for exactly the skills that had the most notes.

`self_collected: true` narrows the refusal to what it was always about.

## What it does, in one sentence

**Injection stays refused and no hash changes; the files become readable.**

```yaml
# SKILL.md frontmatter
sidecar:
  role: arch-review
  self_collected: true
```

## The one distinction that explains almost every bug you will hit

There are now two objects, and they are not interchangeable:

| | `SidecarPlan` | `SidecarView` |
|---|---|---|
| set on | `choice.sidecar` | `choice.sidecar_view` |
| for | the built-in reviewer | an agent or program that collects for itself |
| has `loader()` | yes — this is what injects | **no** |
| has `provenance` | yes — this is what a run record attributes a score to | **no** |
| has `enabled` (the `--no-sidecars` flag) | yes | **no** |
| affects any hash | yes | **no** |

Both live in `src/whetstone/reviewer/factory.py`.

`SidecarView` is deliberately missing those three things. It is not an oversight and please do not
add them. If a view could be handed to `run_eval`, a future caller would inject context into a
reviewer that already collected its own, and attach a provenance saying nothing shaped the run. The
missing methods are the guardrail. There is a test that asserts they stay missing:
`test_a_view_carries_nothing_that_could_inject_or_identify`.

**Rule of thumb when you touch code that reads `choice.sidecar`:** ask "does this *read* the files,
or does it *send* them?"

- Reads → use `bound = choice.sidecar or choice.sidecar_view`. Already done in: the graph route, the
  sidecar-file route, `_sidecar_status`, `whetstone sidecars sweep`, `whetstone sidecars graph`, and
  the triage sidecar target in `candidates.py`.
- Sends, hashes, or records → use `choice.sidecar` **only**. Leave it alone.

## Where each piece lives

| What | File |
|---|---|
| the flag | `src/whetstone/domain/skill.py` — `SidecarSpec.self_collected` |
| all the accept/refuse logic | `src/whetstone/reviewer/factory.py` — `_self_collected()` |
| `SidecarView` | `src/whetstone/reviewer/factory.py` |
| "is a collector installed at all" | `src/whetstone/sidecars/__init__.py` — `collector_installed()` |
| the cost-plan wording | `src/whetstone/preflight.py` — `_describe_self_collected()` |
| the API field | `src/whetstone/service.py` — `SidecarStatus.self_collected` |
| the panel | `ui/src/components/LocalContext.tsx` |
| the two tab intros | `ui/src/routes/SkillDetail.tsx` |
| tests | `tests/unit/test_sidecars_wiring.py`, `tests/api/test_sidecar_graph_routes.py` |

## The five things that are refused, and why each one

All in `_self_collected()`. Each of these used to be silent, which is worse than an error message:
the page would just say the skill reads no local context and nothing anywhere would say why.

1. **No `self_collected: true`** — the original refusal, unchanged. The message now names the flag,
   so the way out is discoverable.
2. **A task skill** — a task skill is scored on work it produces and no review path can run it, so
   there is no review for a collector to be called at the start of.
3. **`--no-sidecars`** — the ablation withholds what *Whetstone* injects, which here is nothing. Left
   to run it would produce a measurement identical to a normal run, call it an ablation, and (since
   the declaration is not in the digest) leave the two indistinguishable afterwards. Ablate inside
   your own reviewer instead.
4. **No `context: source_root:`, or a root that is not a directory** — Whetstone needs the tree to
   find the files for the page.
5. **No installed collector** — for the built-in reviewer a missing `tools/collect_sidecars.py` is
   only a warning, because Whetstone scores with its own canonical copy. Here it *is* the mechanism:
   the flag claims your reviewer calls a file that does not exist. Run `whetstone sidecars install`
   and commit the result.

## Setting it up on a real skill

1. `whetstone sidecars install --skill skills/<id>` and commit `tools/collect_sidecars.py` and
   `tools/sidecar.json`.
2. Call it from your reviewer, once per review, with the diff's changed paths:
   `python tools/collect_sidecars.py --root "$SOURCE_ROOT" <changed paths>`
3. Add `role:` and `self_collected: true` to the `sidecar:` block in `SKILL.md`.
4. Add `context: source_root: { env: YOUR_VAR, required: true }` to `evaluate/step.yaml`.

The console's Sidecar tab walks through exactly these steps for any agent or program skill that has
no role yet.

## Troubleshooting

**"The Sidecar tab still says this skill reads no local context."**
Open the tab — the reason is printed in the problems list. It will be one of the five above. If the
list is empty and the tab is still dark, the skill has no `sidecar:` block at all.

**"I set the flag and the eval digest changed."**
It should not have. `self_collected` is not in `declaration_of()` (`sidecars/__init__.py`), which is
an explicit allow-list of what identifies a measurement. If the digest moved, something added it to
that function — that is the bug. `test_the_flag_stays_out_of_the_installed_declaration` and
`test_self_collected_leaves_the_reviewer_digest_exactly_where_it_was` both guard this.

**"`whetstone sidecars install` now says the collector is stale on every skill."**
Same cause. `install()` and `installed_state()` both write and compare `declaration_of(spec)`. Adding
a field to that dict rewrites every skill's `tools/sidecar.json`.

**"The UI says the harness injects the context."**
Some string was added without a `self_collected` branch. Every claim about *who reads* has to fork on
it. Known places, all already forked: the Sidecar tab intro and the Guidance-tab one-liner (both in
`SkillDetail.tsx`), the panel paragraph, the `scope`/`budget`/`max_files` tooltips, and the
`confirmations` item in `LocalContext.tsx`.

**"`confirmations: true` does nothing."**
Correct, and the panel says `confirmations n/a`. The confirmation question is written into the
built-in reviewer's prompt (`reviewer/llm_reviewer.py`). Whetstone does not write your reviewer's
prompt, so it cannot ask on your behalf. Use `whetstone sidecars sweep` to fill the claim ledger.

**"`budget` / `max_files` are being ignored."**
They are enforced by whoever collects. Your reviewer runs the installed script, which reads the caps
from `tools/sidecar.json`. If you edited the caps in `SKILL.md` and did not re-run
`whetstone sidecars install`, the script is still using the old ones — the panel shows a
`collector stale` badge when that happens.

## Known rough edges, deliberately left

- The **graph tooltips** in `SidecarGraph.tsx` say things like "still injected into every review".
  For a self-collecting skill the verb is loose, but the substance holds: those tooltips are about
  what the *collector* returns (an `unconfirmed` folder is dropped by `collect.py` regardless of who
  runs it), and that is identical either way. Not worth threading a flag through the graph component.
- **`whetstone sidecars show`** builds a `SidecarLoader` by hand for a self-collecting skill
  (`cli.py`), because the view has none. That is intentional: the command answers "what would the
  collector return for these paths", injects nothing, and the installed copy is byte-identical.
- Nothing **verifies** that your reviewer actually calls the collector. The flag is a claim by the
  author. Whetstone checks the two ways it can be false in its own terms — no tree, no collector —
  and cannot check the third.

## How this was verified (2026-08-08)

`ruff` clean · `mypy` 16 errors, unchanged from the `main` baseline · full `pytest` suite green ·
`tsc` clean · `vitest` green · `prettier` clean · `vite build` succeeds.

Live, against a copy of `examples/sidecar-review/` converted to an agent reviewer: the graph drew 5
folders and 7 claims for a self-collecting skill (dark before the change), the file route opened a
note, the panel read "Collected by this skill's own reviewer", `confirmations n/a` rendered, the
setup panel taught the new path, `whetstone eval run --no-sidecars` was refused before spending, and
the cost plan printed "Whetstone resolves none of it, so it is in no hash". Removing the flag put the
original refusal back, now naming `self_collected: true` as the way out.
