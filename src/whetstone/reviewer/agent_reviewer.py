"""A reviewer that *runs* the skill instead of pasting it into a prompt.

The built-in `LLMReviewer` sends the whole skill folder as one system prompt and takes one answer.
This gives the model the skill's `SKILL.md` as instructions, a tool to read the folder's other pages
when its instructions point at them, tools to read the source when the skill declares a root, and a
budget of steps to investigate before answering.

It satisfies the same one-method `Reviewer` protocol as the other two, so the harness, judge,
scoring and gate are untouched — an agent-scored run is still a run. The findings it produces are
judged against expectations exactly as any other reviewer's are.

**On the output shape.** Findings are the review-shaped answer, and this deliberately keeps to them
for now: the loop itself (`agent/loop.py`) is output-agnostic, and the only review-specific thing
here is the terminal tool's schema. When a generic output/verification model lands, that schema is
what gets replaced — not the engine.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from whetstone.agent.builtins import COLLECT, BuiltinTools
from whetstone.agent.loop import AgentTrace, run_agent
from whetstone.agent.runner import SkillAgent
from whetstone.agent.skilltools import SkillTools
from whetstone.domain.change import CodeChange
from whetstone.domain.enums import Severity
from whetstone.domain.finding import Finding
from whetstone.domain.skill import Skill
from whetstone.llm.tools import ToolCall, ToolClient, ToolResult, ToolSpec
from whetstone.reviewer.base import ReviewerProvenance
from whetstone.reviewer.llm_reviewer import number_diff
from whetstone.sidecars.collect import AGENTS_DIR

SUBMIT = "submit_findings"

# How many times a review of a sidecar-bearing skill may be turned down for ending without reading
# the notes. See `AgentReviewer.review`'s `admit`: enough that a model which merely forgot is put
# right, few enough that one which will not comply does not spend a whole budget being asked.
_NUDGES = 2

_SUBMIT_TOOL = ToolSpec(
    name=SUBMIT,
    description=(
        "Report your conclusions and finish. Call this exactly once, with every issue you are "
        "reporting. Call it with an empty list if the change is fine — that is a real answer. "
        "A finding is a problem you are reporting: never list something to say it is correct, or "
        "to explain why it is allowed here. Leave it out instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "file path from the diff"},
                        "line": {"type": "integer", "description": "line in the NEW file"},
                        "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                        "message": {"type": "string", "description": "what is wrong, and why"},
                        "rule_id": {"type": "string", "description": "the rule, if one applies"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["path", "line", "message"],
                },
            }
        },
        "required": ["findings"],
    },
)

_TASK = """\
Review the following change and report what your instructions say should be reported.

Only report issues in lines this change adds or modifies. Give the line number in the NEW file, \
taken from the gutter on the left of each line below.

{diff}"""


class AgentReviewer(SkillAgent):
    """Runs a skill as an agent over a change, and returns what it reported."""

    def __init__(
        self,
        client: ToolClient,
        *,
        source_root: Path | str | None = None,
        max_steps: int | None = None,
        context: dict[str, object] | None = None,
        redacted: dict[str, object] | None = None,
        context_digest: str = "",
        skill_tools: SkillTools | None = None,
    ) -> None:
        super().__init__(
            client,
            source_root=source_root,
            max_steps=max_steps,
            context=context,
            skill_tools=skill_tools,
        )
        self._redacted = dict(redacted or {})
        self._digest = context_digest
        self.last_trace: AgentTrace | None = None
        # The `.agents/` files the last review was seen to open, in the shape `harness._sidecars_of`
        # reads. Same contract as `last_trace`: written by `review`, read immediately after it.
        self.last_sidecars: dict[str, Any] | None = None

    @property
    def provenance(self) -> ReviewerProvenance:
        return ReviewerProvenance(
            identity=self.identity, context=dict(self._redacted), context_digest=self._digest
        )

    def review(self, skill: Skill, change: CodeChange) -> list[Finding]:
        builtins = BuiltinTools(skill=skill, root=self._root)
        skill_tools = self._skill_tools
        tools = [
            *builtins.specs(),
            *(skill_tools.specs() if skill_tools else []),
            _SUBMIT_TOOL,
        ]
        def dispatch(call: ToolCall) -> ToolResult:
            if builtins.handles(call.name):
                return builtins.dispatch(call)
            if skill_tools is not None and skill_tools.handles(call.name):
                return skill_tools.dispatch(call)
            return ToolResult(call.id, f"No tool named {call.name!r}.", is_error=True)

        told = 0

        def admit(_: dict[str, Any]) -> str | None:
            """Refuse to end a review of a sidecar-bearing skill that never read the sidecars.

            The gap this closes was the whole of a reported failure. Whetstone deliberately does
            not inject local notes into an agent review — the agent reads what it chooses, and
            recording injected text as the reviewer's context would be false provenance and would
            corrupt the hash the gate rests on (`factory._self_collected`). So the only thing
            asking the reviewer to collect was a sentence in the skill's own prompt, and a model
            that ignores it produces a review with no local context, scored as though it had some.

            Improve, meanwhile, resolves the same notes unconditionally. The two sides of the loop
            therefore disagreed about what was knowable: the scorer failed a case the drafter could
            see was already answered beside the code, and no screen reconciled them.

            A refusal rather than an injection keeps the provenance honest — the reviewer still
            does its own reading, it is simply not allowed to skip it.

            Bounded at `_NUDGES`, and the bound is not timidity. Refusing until it complies costs
            a model that will never comply its entire step budget on every case, and ends in the
            forced ending anyway — the same review, several calls later, on every case in the
            corpus. Most models comply on the first refusal; one that has been told twice is a
            configuration problem, and the useful response is to let the run finish and leave the
            evidence. `AgentTrace.refused` counts it, and `last_sidecars` still reports an empty
            read, which is what `improve` turns into a sentence naming the reviewer.
            """
            nonlocal told
            if builtins.collected or not builtins.collects:
                return None
            told += 1
            if told > _NUDGES:
                return None
            return (
                f"Not yet. This skill reads notes kept beside the code, and they are part of the "
                f"review — a folder can say that something which looks wrong there is deliberate, "
                f"or that a concern is handled elsewhere. Call `{COLLECT}` with the paths this "
                f"change touches, read what comes back, then submit. If those folders keep no "
                f"notes it will say so, and you can submit straight after."
            )

        answer, trace = run_agent(
            self._client,
            system=self._system(skill, SUBMIT),
            task=_TASK.format(diff=number_diff(change.to_unified_diff())),
            tools=tools,
            dispatch=dispatch,
            terminal_tool=SUBMIT,
            max_steps=self._max_steps,
            cancel=self._cancel,
            admit=admit,
        )
        self.note_trace(trace)
        self.last_trace = trace
        # Only for a skill that declares a role. Without that guard every agent-reviewed skill
        # grows a sidecars block reading "opened nothing", which is not the same fact and would
        # collapse `CaseRun.sidecars`'s distinction between "read nothing" and "never asked to".
        #
        # A fresh `BuiltinTools` per review means `reads` is this case's, not the run's — the same
        # reason `last_trace` is safe to read here.
        self.last_sidecars = (
            None
            if skill.sidecar.is_empty()
            else {
                "resolved_by": "reviewer",
                "files": [{"path": p} for p in builtins.reads if _is_sidecar(p)],
            }
        )
        return _findings(skill.id, answer)

    def _source_note(self) -> str:
        return (
            "\n# The source tree\n\nThe code under review lives in a checkout you can read "
            "with `read_file`, `list_dir` and `grep`. Paths are relative to its root. Use it "
            "when the change alone does not tell you whether something is a problem."
        )


def _is_sidecar(path: str) -> bool:
    """Whether a delivered source file sits inside an `.agents/` directory.

    Not filtered by role. The reviewer chooses its own reads, so an `arch` skill that opens
    `qa.md` has done something worth seeing in the record rather than something to quietly drop.
    """
    return AGENTS_DIR in PurePosixPath(path.replace("\\", "/")).parts


def _findings(skill_id: str, answer: dict[str, object]) -> list[Finding]:
    """Turn the terminal tool's arguments into findings, discarding entries that are not usable.

    Tolerant on purpose: a model that returns a string line number or omits severity has still
    reported a real issue, and losing it to a validation error would score the skill for the
    model's formatting rather than its judgement.
    """
    raw = answer.get("findings")
    if not isinstance(raw, list):
        return []
    out: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        out.append(
            Finding(
                skill_id=skill_id,
                rule_id=_str_or_none(item.get("rule_id")),
                path=path,
                line=_int_or_none(item.get("line")),
                severity=Severity.parse(str(item.get("severity") or "warning")),
                message=str(item.get("message") or ""),
                confidence=_float_or_none(item.get("confidence")),
            )
        )
    return out


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
