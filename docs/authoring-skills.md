# Writing a Whetstone skill

A working reference for authoring `SKILL.md`, `step.yaml` and the prompts beside them. Written for
whoever writes these files next — a person or a model — and organised around the mistakes that are
silent, because those are the ones that cost a corpus rather than a minute.

Every claim here is asserted against behaviour in `tests/unit/test_docs_match_reality.py`. If this
document and the code disagree, that suite fails.

---

## 1. What a skill is

A **folder**, not a file:

```
skills/<skill-id>/
  SKILL.md              guidance + frontmatter — the entry point
  meta.yaml             owner, references, provenance   (NOT readable by the agent — §7)
  references/*.md       guidance that outgrew SKILL.md
  tools/*.py            programs the skill brings       (cwd = the skill root — §6)
  eval_cases/<id>/      case.yaml + change.diff — what gates a change to the guidance
  evaluate/step.yaml    how it is scored
  improve/step.yaml     how a guidance change is drafted   (+ prompt.md)
  triage/step.yaml      how a mined signal becomes a case  (+ prompt.md)
  update/step.yaml      how the wiki is regenerated
  wiki/                 repo context: index.yaml + pages/*.md   (optional — §8)
```

`whetstone skills scaffold --skill skills/<id>` writes correct starters for the step files.

**A skill is split across files precisely so that it is never all in one context at once.** That is
the single idea the rest of this follows from. `SKILL.md` says what to consult and when; the harness
serves the rest a page at a time.

---

## 2. The first decision: agent or one prompt

```yaml
# evaluate/step.yaml
agent:
  enabled: true
  max_steps: 12
  source: { env: MY_REPO, required: true }
```

| | `agent:` on | `agent:` off (the default) |
|---|---|---|
| `SKILL.md` | the instruction set | pasted into the prompt |
| `references/*.md` | fetched with `read_skill_file` when the guidance points at them | all concatenated into the same prompt |
| source tree | `read_file`, `list_dir`, `grep` | unreachable |
| cost per review | up to `max_steps + 1` calls | 1 call |

**Choose `agent:` if the skill is more than one file, or if any rule depends on code the diff does
not contain.** That is most real review skills: whether a call is dangerous is a fact about the
callee, and the callee is not in the change.

Valid on `evaluate`, `improve` and `triage`. Not on `update`, which invokes a generator your
deployment owns and has no skill judgement in it to give tools to.

### The refusal you will hit

A skill with companion pages whose `improve` step is **not** an agent is **refused**, not truncated:

```
architect-skill is a folder: 3 companion page(s), 114,967 bytes, and this improve step would
paste every one of them into a single prompt. … Set `agent: enabled: true` in improve/step.yaml
and the drafter reads them with a tool instead.
```

A single call has no tools, so its only way to show a folder is to concatenate it. There is no size
at which that becomes right, which is why this is a refusal rather than a cap — a cap would shrink
the prompt by *dropping rules*, leaving the model rewriting guidance it saw a fraction of.

Single-file skills are unaffected. Pasting one file is correct.

### The warning you may hit

`[runs] large_prompt_chars` (default 40,000, `0` disables) warns — on the skill page, in the cost
preflight, and in the drafting-prompt preview — when a non-agent step's guidance is that large.
Nothing is ever truncated to fit. It catches the case the refusal does not: one `SKILL.md` that has
simply grown too big to keep pasting.

---

## 3. `source:` — and the one word that matters

```yaml
agent:
  source: { env: MY_REPO, required: true }
```

**Always write `required: true`.** Without it, an unset variable is not an error:

```
refuses at the plan? NO — it runs
source_root         : None
tools offered       : ['read_skill_file']     ← no grep, no read_file
```

The step runs with no source access, every rule about checking the code becomes unverifiable, and
the run looks completely normal. With `required: true`, both the CLI and the console refuse at the
plan, before anything is spent, naming the variable. A source root that is *set but is not a
directory* is refused the same way, for the same reason: every tool would answer "no such file",
which reads exactly like a clean codebase.

**The absolute path never reaches the model.** It is excluded from the prompt deliberately — an
`env:` value is as often a token as a path. What the agent is told is:

> A read-only checkout is available through `read_file`, `list_dir` and `grep`. Paths are relative
> to its root.

Everything is relative to that root and sandboxed to it, symlinks included. If the agent should know
*which* repository it is looking at, say so as a literal in `context:` (§4).

Each step declares its own `source:`. Nothing is inherited — a step that quietly acquired another
step's access would be the surprising one.

---

## 4. `context:` — everything else the skill needs

A **top-level** block, a sibling of `agent:`, not nested inside it:

```yaml
agent:
  enabled: true
  source: { env: MY_REPO, required: true }
  tools:
    - name: jira_issue
      run: ["python", "tools/jira.py"]
      input_schema: { type: object, properties: { key: { type: string } } }

context:
  jira_token:  { env: JIRA_TOKEN, required: true }
  revision:    { env: BUILD_SHA, pin: true }
  conventions: { file: ./conventions.md }
  project:     hub-backend
```

Three forms, and **what the model sees differs from what your tools get**:

| form | in the agent's prompt | to `tools:` on stdin | hashed into the run |
|---|---|---|---|
| `{ env: X }` | `<env:X>` — the name only | the real value | no |
| `{ env: X, pin: true }` | **the value** | the real value | **yes** |
| `{ file: ./p }` | `<file:./p>` | the file's contents | yes, by content |
| literal | the value itself | the value itself | yes |

- A **secret** goes in as plain `{ env: … }`. It reaches the tool that needs it and never enters a
  prompt or a transcript.
- **`pin:` is not "also hash it" — it un-redacts.** Use it for a commit SHA or a schema version:
  something that is not secret and *does* determine what the reviewer reads. **Never pin a token.**
- Something the **model should read** must be a literal or a `file:`. An `env:` one is redacted and
  the agent only ever sees the placeholder.
- `file:` paths are resolved relative to the skill folder and may not climb out of it.
- `required: true` on an `env:` makes an unset variable a refusal at the plan rather than a silent
  absence. Use it on anything a rule depends on.

`context:` works identically on `evaluate`, `improve` and `triage`.

---

## 5. Where `.env` goes

**One `.env`, beside `whetstone.toml` at the workspace root.** Not per skill.

```
my-skills/
  whetstone.toml
  .env                ← MY_REPO=/home/you/src/hub  ·  JIRA_TOKEN=…
  skills/<id>/…
```

Discovery walks *upward* from the working directory, so a `.env` inside a skill folder is never
found. Precedence: **CLI flag → real environment → `.env` → `whetstone.toml` → default**, so CI can
inject a token without editing a file. Override the file for one run with `--env-file <path>`.

The step files name the *variable*; `.env` holds the *value*. Several skills can share one.

---

## 6. `tools:` — programs the skill brings

```yaml
agent:
  tools:
    - name: owner_of
      description: >
        Return the team that owns a module. Use it so a finding names who should pick it up.
      run: ["python", "tools/owner_of.py"]
      input_schema:
        type: object
        properties: { module: { type: string } }
        required: [module]
```

The contract, in full:

```
stdin:  {"arguments": {"module": "ledger.py"}, "context": {"owners": "…"}}
stdout: whatever the model should see
```

- **`run:` resolves from the skill root.** `tools/owner_of.py` means `<skill>/tools/owner_of.py`,
  whichever step declared it. One copy serves every step.
- **A *step's* own `run:` is different.** `improve/step.yaml` with `run: ["python", "run.py"]` runs
  with `<skill>/improve` as its working directory. Same key, two bases — skill root for agent tools,
  step folder for step programs.
- **Exiting non-zero is not fatal.** stderr goes back to the model as an error result, so an agent
  told "no such module" tries something else instead of losing the case. Write useful errors.
- `run:` is a **list**, never a string. Nothing is re-split on spaces and no shell is involved, so a
  path containing a space works.
- The tool receives `context` with **real** values, secrets included. It is the out-of-band channel
  that keeps the token out of the prompt.
- `description` is read by the model and is the whole of what it knows. Say when to call it.

---

## 7. What the agent can and cannot reach

**Can:**

- `read_skill_file(path[, start, end])` — the skill's own **markdown** pages, by the exact path the
  instructions link to. The tool listing gives each page's length. A page too long for one reply
  comes back a window at a time, saying which lines it gave and how to ask for the next.
- `read_file`, `list_dir`, `grep` over the declared `source:` root, read-only and sandboxed.
- Whatever `tools:` declares.
- The redacted `context:` view, and its own instructions.

**Cannot:**

- **`meta.yaml`.** Only `.md` files under the skill folder are pages, so provenance is *not*
  reachable. If a prompt says "cite only rules traceable to a recorded ticket", the agent has no way
  to check unless you give it one — a `tools:` entry with `{ file: ./meta.yaml }` in `context:`.
  This trips people, because the instruction reads as satisfiable.
- **`eval_cases/`, `wiki/` and the step folders.** Pruned from the page walk.
- **Anything outside the source root**, including through a symlink.
- **A shell.** There is no Bash tool. Work that needs one goes in a `tools:` program.

**`grep` is a fixed substring, not a regex.** `@Transactional` works; `@Transactional\s+public`
matches nothing and returns "No matches", which reads like clean code. There is no glob/find tool
either, and `list_dir` is one level at a time — tell the agent to `list_dir("")` first to orient.

---

## 8. Prompt templates

`{{name}}` substitution, **strict**: an unknown placeholder is an error naming the available
variables, not an empty string. A prompt saying `{{failurs}}` fails loudly rather than quietly
improving a skill it was shown nothing about.

### `improve/prompt.md`

| variable | is |
|---|---|
| `{{skill_id}}` | the skill's id |
| `{{guidance}}` | the current `SKILL.md` body |
| `{{pages}}` | its companion pages, each under the path to return it as |
| `{{failures}}` | the rendered cluster representatives |
| `{{failure_count}}` / `{{shown_count}}` | how many there were / how many reached the prompt |
| `{{cases_total}}` / `{{cases_scored}}` | corpus size / what the run scored |
| `{{recall}}` / `{{fp_rate}}` | the run's scores, or `n/a` |
| `{{wiki}}` | repo context for the files the run's cases touch |
| `{{instruction}}` | whatever `--instruction` passed, or empty |

**Under `agent:`, `{{guidance}}` and `{{pages}}` render as pointers, not text** — the body is
already the system prompt and the pages are a tool call away. The `{{pages}}` appendix is not added
either. Write the template so it reads correctly both ways, or accept that the skill is agentic and
say so.

`{{instruction}}` and `{{pages}}` are appended if the template does not place them, so neither is
ever silently dropped.

**Tell the drafter it may rewrite a page.** The `submit_guidance` schema allows `pages`, but a
prompt that ends "return the complete new guidance body" implies otherwise — and then a rule living
in `references/x.md` gets restated in `SKILL.md` instead of fixed where it lives.

### `triage/prompt.md`

Variables are the mined evidence: `{{candidate_id}}`, `{{kind}}`, `{{path}}`, `{{ref}}`,
`{{human_signal}}`, `{{mr_title}}`, `{{comments}}`, `{{suggestion}}`, `{{diff}}`, `{{seeded}}`.

**A triage agent is deliberately not shown the guidance**, and is offered no `read_skill_file`. Its
instructions are the drafting brief instead of `SKILL.md`. An expectation written while looking at
the rules describes the rules; the reviewer then answers in the same words and the case passes
forever without testing anything. Source access and `tools:` are unaffected — reading the code
around a reviewer's comment is exactly how you find out what they objected to.

### `evaluate` has no prompt

The reviewer prompt is the harness's. A skill that needs a reviewer the harness cannot be sets
`run:` instead and gets the diff, the guidance and its context as JSON on stdin.

---

## 9. `{{wiki}}` and the `wiki/` folder

Optional pre-baked repo context, retrieved per run by **which source paths the run's cases touch**:

```
wiki/index.yaml      pages: [{ page: health, paths: ["src/health/**"] }]
wiki/pages/health.md
```

Every indexed page needs its file on disk — an index row naming a missing page is a hard load error.

Three reasons `{{wiki}}` can come back empty, and the message says which:

1. no `wiki/` folder;
2. a folder, but **no scored run** — retrieval is keyed to a run's cases, so score the skill first;
3. a folder and a run, but no `paths:` glob matched — fix the globs.

**The wiki is inside `skill_hash`**, so editing `index.yaml` or a page retracts your last run as
stale. That is deliberate: a gate passed against one set of context must not authorise publishing
another. Step files are *not* hashed — changing a sample size does not retract a gate.

**If the skill has a `source:` root, you probably do not want a wiki.** A pre-baked summary is what
you inject when the reviewer cannot open the repository. This one can.

---

## 10. Cost, and what the plan tells you

- An agent step costs up to **`max_steps + 1`** calls per invocation — the budget, then one forced
  answer. The plan prices the ceiling; agents normally stop well short.
- A gate scores **both sides**, so double it.
- `[runs] max_llm_calls_per_run` warns when the estimate exceeds it.

The cost preflight names the reviewer, what it can read, and which of run-or-paste you are about to
get. The skill page shows the same per step, before you click anything.

---

## 11. Checklist

- [ ] More than one guidance file, or a rule that depends on code outside the diff → `agent: enabled: true`
- [ ] Every `source:` has **`required: true`**
- [ ] Secrets are plain `{ env: … }`; only non-secret, run-determining values are `pin: true`
- [ ] Anything the model must *read* is a literal or a `file:`, never a bare `env:`
- [ ] `.env` at the workspace root, not in the skill folder
- [ ] `tools:` paths are relative to the **skill root**; step `run:` to the **step folder**
- [ ] No prompt asks the agent to check something it has no tool for (`meta.yaml`, a Jira id)
- [ ] No prompt tells the agent to `grep` a regex
- [ ] The improve prompt says it may return `pages`
- [ ] `{{wiki}}` and `inputs.wiki` are both present, or both absent
- [ ] `triage/prompt.md` does not ask for anything that needs the guidance

---

## Worked examples in this repo

| example | shows |
|---|---|
| [`examples/agent-skill`](../examples/agent-skill) | `agent:` on all three steps, `source:`, `tools:`, `context:`, the triage blindfold |
| [`examples/agentic-reviewer`](../examples/agentic-reviewer) | `run:` — your own reviewer program |
| [`examples/task-skill`](../examples/task-skill) | `task:` — scored on work produced, not findings reported |
| [`examples/qa-test-authoring`](../examples/qa-test-authoring) | an adopted 12-file skill: `task:` with a grader it ships, and why its improve step *must* be an agent |
| [`examples/console-demo`](../examples/console-demo) | the whole loop in the browser, one agentic skill and three plain ones |

Deeper reference: [docs/skill-pipeline.md](skill-pipeline.md). The decisions behind it:
[ADR-021](decisions.md), [ADR-023](decisions.md), [ADR-025](decisions.md).
