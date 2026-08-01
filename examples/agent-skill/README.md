# A skill that runs as an agent

The runnable counterpart to the "Run the skill as an agent" section of
[`docs/skill-pipeline.md`](../../docs/skill-pipeline.md).

**This is not the same thing as [`../agentic-reviewer/`](../agentic-reviewer/)**, despite the name.
That example uses `run:` — *your* program does the reviewing and Whetstone feeds it a JSON payload.
Here Whetstone runs the skill itself: `SKILL.md` becomes the model's instructions, the folder's other
pages are fetched on demand, the source tree is searchable, and the skill's own tool is offered to
the model as a tool.

```
skills/panic-guard-agent/
  SKILL.md                    the instructions — short, and it links rather than inlines
  references/panics.md        read only when the instructions send the model here
  owners.json                 committed config, loaded by `context: { file: … }`
  tools/owner_of.py           a tool the skill brings; Whetstone runs it on request
  evaluate/step.yaml          `agent: enabled`, plus source and tools
  improve/step.yaml           the *same* runtime — the drafter reads the code too
  eval_cases/                 one should_catch, one should_not_flag
source/ledger.py              the "source tree" — the evidence that is *not* in the diff
```

**Both steps run the same way**, which is the point of the example as much as the reviewing is.
Score the skill and the reviewer greps `ledger.py` to decide whether a call can panic; improve it
and the *drafter* greps the same tree before writing a rule about it. Drop `agent:` from
`improve/step.yaml` and you have the old behaviour to compare against: one call, every companion
page pasted into the prompt, and a rewrite grounded in a failure list rather than in the code.

## Why an agent here

Whether `open_ledger()` is dangerous is a fact about `open_ledger`, and the diff only shows the line
that calls it. A single-prompt reviewer cannot know: you would have to paste the whole codebase into
every case, on the chance that a function is relevant. The agent greps for the definition, reads the
docstring, and decides — which is the same thing a person does, and it is why the `should_not_flag`
case is the interesting one. `safe_get` and `balance_of` *look* risky and are not, and only a
reviewer that actually went and looked can tell.

## Run it

The source root is machine-local, so it comes from the environment and is never committed. Point it
at the bundled tree:

```bash
# from the repository root
export PANIC_GUARD_AGENT_SOURCE="$PWD/examples/agent-skill/source"     # Windows: $env:PANIC_GUARD_AGENT_SOURCE = "$PWD\examples\agent-skill\source"

whetstone eval run --skill examples/agent-skill/skills/panic-guard-agent --llm ollama --model qwen3-coder

# ...then improve it from what that run got wrong — the drafter runs as an agent too
whetstone skills improve --skill examples/agent-skill/skills/panic-guard-agent --run <run-id>
```

Leave the variable unset and the run is refused **at the plan**, before it spends anything — an
agent whose source root is missing would answer having opened nothing, and that reads exactly like a
clean codebase.

**Your backend must support tool calling.** A model that cannot is refused loudly rather than
degraded to a plain completion, for the same reason: a review carried out with no access looks
identical to one that worked. For Ollama, use a tool-capable model such as `qwen3-coder`.

## What to look at afterwards

```
whetstone runs show <run-id>
```

The `read` line is the **trajectory** — every tool the agent actually called. An agent is a less
fixed instrument than a single call, so when a gate's two sides read different things the record
says so (`trace_diverged`) and the delta is not purely the guidance. Watch for
`forced answer (ran out of steps)` too: it means the agent hit `max_steps` and had to be made to
answer, which is the signal to raise the budget.

## The gotchas this example is shaped around

Each of these is silent — the run looks normal and the answer looks like an answer.

**`required: true` on `source:` is load-bearing.** Without it an unset variable is not an error: the
step runs with `read_skill_file` and nothing else, every instruction about checking the code becomes
unverifiable, and nothing says so. Both step files here write it.

**The absolute path never reaches the model.** It is told a checkout exists and that paths are
relative to its root — no more, because an `env:` value is as often a token as a path. If the agent
should know *which* repository it is looking at, say so as a literal in `context:`.

**`grep` is a fixed substring, not a regex.** `PANICS:` works; `PANICS:\s*\w+` matches nothing and
returns "No matches", which reads exactly like clean code. There is no glob tool either, and
`list_dir` is one level at a time.

**`meta.yaml` is not reachable.** Only markdown files under the skill folder are pages, so
`read_skill_file` cannot serve it — see the comment at the top of this skill's `meta.yaml`. A prompt
that says "cite only rules traceable to a recorded ticket" is asking for something the agent cannot
check, unless you hand the file over through `context:` and a tool.

**`tools:` paths resolve from the skill root; a step's own `run:` resolves from the step folder.**
`tools/owner_of.py` here is `<skill>/tools/owner_of.py` for all three steps. Had `improve/step.yaml`
used `run:` instead of `prompt:`, its program would have been `<skill>/improve/…`.

**Triage does not get the guidance.** `agent:` means "SKILL.md is the instructions" on evaluate and
improve, and deliberately not on triage — `triage/step.yaml` explains why. Source and tools are
unaffected.

**Drop `agent:` from `improve/step.yaml` and it is refused, not degraded.** This skill has a
companion page, and a single call's only way to show a folder is to paste it. Try it: the error
names the setting that fixes it.

Full reference, including the pieces this example does not use (`task:`, `wiki:`, `pin:`):
**[docs/authoring-skills.md](../../docs/authoring-skills.md)**.
