"""An offline stand-in for a review model, speaking the OpenAI chat-completions API.

The console reaches a model through `build_llm_client`, and there is no way to hand it a Python
fake from outside the process. So rather than monkey-patching the factory — which would exercise a
code path no real deployment uses — the demo starts a tiny server that speaks the same protocol as
Ollama or LM Studio and points Whetstone at it with `WHETSTONE_LLM_BASE_URL`. Everything downstream
is the production path: the real client, its retries and JSON extraction, the real preflight banner,
and a backend name recorded on every run.

**It reads the guidance, and that is the point.** `PatternReviewer` (practice mode) matches fixed
regexes and ignores the skill entirely, so editing `SKILL.md` cannot move its score by 0.01 — a demo
built on it would draw a flat line and prove nothing. This stub fires a rule only when the guidance
in the system prompt asks for it, so tightening a rule in the console genuinely changes the next
run's score. What it keys on is deliberately simple and written down in the README, because a
playground whose reactions you cannot predict is not a playground.

It is not a model. It cannot generalise, it has no opinion about code it has no rule for, and
nothing it produces is evidence about a real skill.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL_NAME = "whetstone-demo-stub"


# --- the review rules -------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One thing the stub knows how to notice, and the guidance that has to ask for it.

    `asks_for` is what couples the stub to the skill: the rule stays silent unless one of these
    phrases appears in the guidance it was given. That is the whole mechanism by which an edit in
    the console changes a score.
    """

    rule_id: str
    trigger: re.Pattern[str]
    asks_for: tuple[str, ...]
    message: str
    severity: str = "warning"
    confidence: float = 0.85
    # Suppressed in test code when the guidance says test code is exempt. Only meaningful for rules
    # about constructs that are idiomatic in tests and dangerous in service code.
    test_exempt: bool = False
    # An added line matching this is not a finding after all — used for the "unless" half of a rule.
    unless: re.Pattern[str] | None = None
    # Must also match one of the next two added lines. What separates "caught the exception" from
    # "caught it and threw it away", which is the difference between a finding and a false positive.
    then: re.Pattern[str] | None = None


RULES: tuple[Rule, ...] = (
    # --- Rust ---
    Rule(
        rule_id="R1",
        trigger=re.compile(r"\.unwrap\("),
        asks_for=("unwrap",),
        message="`.unwrap()` panics when the value is absent, which is a normal error path here.",
        test_exempt=True,
    ),
    Rule(
        rule_id="R1",
        trigger=re.compile(r"\.expect\("),
        asks_for=("expect",),
        message="`.expect()` panics exactly as `.unwrap()` does when the value is absent.",
        test_exempt=True,
    ),
    Rule(
        rule_id="R2",
        trigger=re.compile(r"^\s*let\s+_\s*="),
        asks_for=("swallow", "discard", "ignored", "let _", "drop the result"),
        message="the Result of this call is discarded, so a failure leaves no trace anywhere.",
    ),
    # --- Python ---
    Rule(
        rule_id="P1",
        trigger=re.compile(r"^\s*except\b[^:]*:\s*$"),
        asks_for=("swallow", "bare except", "silently", "discard"),
        message="this handler catches and discards the exception, hiding the failure from callers.",
        then=re.compile(r"^\s*(pass|return None|return|continue)\s*$"),
    ),
    Rule(
        rule_id="P2",
        # `from` is too common a word to look for in prose, so the guidance has to name the
        # keyword in backticks for this rule to count as asked for.
        trigger=re.compile(r"^\s*raise\s+\w*(Error|Exception)\b(?!.*\bfrom\b)"),
        asks_for=("`from`", "chain", "traceback", "original exception"),
        message="re-raising without `from` drops the original traceback, so the cause is lost.",
    ),
    # --- SQL migrations ---
    Rule(
        rule_id="S1",
        trigger=re.compile(r"CREATE\s+(UNIQUE\s+)?INDEX\b", re.I),
        asks_for=("concurrently", "lock", "index"),
        message="building this index takes an exclusive lock; use CREATE INDEX CONCURRENTLY.",
        severity="error",
        unless=re.compile(r"CONCURRENTLY", re.I),
    ),
    Rule(
        rule_id="S2",
        trigger=re.compile(r"ADD\s+COLUMN\b.*\bNOT\s+NULL\b", re.I),
        asks_for=("not null", "default", "backfill"),
        message="adding a NOT NULL column with no DEFAULT fails against any existing row.",
        severity="error",
        unless=re.compile(r"\bDEFAULT\b", re.I),
    ),
    Rule(
        rule_id="S3",
        trigger=re.compile(r"DROP\s+COLUMN\b", re.I),
        asks_for=("drop column", "still reads", "previous release", "expand"),
        message="the running release still reads this column, so dropping it breaks the rollback.",
        severity="error",
    ),
    # --- Go ---
    # `asks_for` names the *construct*, not the topic, which is what makes the agent skill's story
    # work: its v1 guidance talks about deadlines in general and never names `context.Background()`,
    # so it misses — and the improve step's job is to add the specific rule.
    Rule(
        rule_id="G1",
        trigger=re.compile(r"context\.Background\(\)"),
        asks_for=("context.background",),
        message="this call is made with no deadline, so a hung gateway blocks the worker forever.",
        severity="error",
    ),
)

# Phrases that make the stub treat test code as exempt. Any test token plus any exemption token —
# both have to be present, so "no unwrap in test code" (a rule *about* tests) does not read as one.
_TEST_TOKENS = ("test code", "#[cfg(test)]", "#[test]", "_test.rs", "tests/", "test module")
_EXEMPT_TOKENS = (
    "does not apply",
    "do not apply",
    "not apply",
    "exempt",
    "idiomatic",
    "except in",
    "excluded",
    "outside test",
)


def exempts_tests(guidance: str) -> bool:
    lowered = guidance.lower()
    return any(t in lowered for t in _TEST_TOKENS) and any(
        t in lowered for t in _EXEMPT_TOKENS
    )


def asks_for(guidance: str, phrases: tuple[str, ...]) -> bool:
    lowered = guidance.lower()
    return any(phrase in lowered for phrase in phrases)


# --- reading the diff -------------------------------------------------------------


@dataclass
class AddedLine:
    path: str
    number: int  # in the new file, which is what a finding's `line` means
    text: str


@dataclass
class ParsedDiff:
    added: list[AddedLine] = field(default_factory=list)
    # Every line of each file's hunks, for deciding whether we are looking at test code.
    context: dict[str, list[str]] = field(default_factory=dict)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# The reviewer prompt puts each line's new-file line number in a left gutter (`llm_reviewer
# .number_diff`), because models cannot reliably add up hunk offsets while reading. The gutter is
# stripped here so the stub reads the diff underneath.
#
# Missing this was silent and total: with the gutter in place no line starts with `+`, so the stub
# found no added lines, produced no findings, and every skill in the demo scored 0.00 — while the
# README went on describing a 0.33 baseline rising to 1.00. Nothing failed; the demo just quietly
# stopped demonstrating anything. Tolerating both shapes is what keeps that from recurring the next
# time the prompt is reworked.
_GUTTER = re.compile(r"^\s*\d*\s\|\s")


def strip_gutter(line: str) -> str:
    return _GUTTER.sub("", line, count=1)


def parse_diff(text: str) -> ParsedDiff:
    """Added lines with their new-file line numbers. Enough for a stub, not a diff library."""
    out = ParsedDiff()
    path, line_no = "", 0
    for gutted in text.splitlines():
        raw = strip_gutter(gutted)
        if raw.startswith("+++ "):
            path = raw[4:].strip().removeprefix("b/")
            out.context.setdefault(path, [])
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if not path:
            continue
        out.context[path].append(raw)
        if raw.startswith("+"):
            out.added.append(AddedLine(path=path, number=line_no, text=raw[1:]))
            line_no += 1
        elif not raw.startswith("-"):
            line_no += 1  # context line: present in both files
    return out


def _is_test_path(path: str) -> bool:
    name = Path(path).name.lower()
    lowered = path.lower()
    return (
        name.startswith("test_")
        or name.endswith("_test.rs")
        or name.endswith("_test.py")
        or "/tests/" in lowered
        or lowered.startswith("tests/")
    )


def is_test_context(path: str, lines: list[str]) -> bool:
    return _is_test_path(path) or any(
        marker in line for line in lines for marker in ("#[test]", "#[cfg(test)]", "def test_")
    )


def review(guidance: str, diff: str) -> list[dict[str, Any]]:
    """The findings this guidance would produce on this diff."""
    parsed = parse_diff(diff)
    findings: list[dict[str, Any]] = []
    for index, added in enumerate(parsed.added):
        in_tests = is_test_context(added.path, parsed.context.get(added.path, []))
        following = [
            line.text for line in parsed.added[index + 1 : index + 3] if line.path == added.path
        ]
        for rule in RULES:
            if not rule.trigger.search(added.text):
                continue
            if rule.unless is not None and rule.unless.search(added.text):
                continue
            if rule.then is not None and not any(rule.then.search(t) for t in following):
                continue
            if not asks_for(guidance, rule.asks_for):
                continue
            if rule.test_exempt and in_tests and exempts_tests(guidance):
                continue
            findings.append(
                {
                    "path": added.path,
                    "line": added.number,
                    "severity": rule.severity,
                    "rule_id": rule.rule_id,
                    "message": rule.message,
                    "confidence": rule.confidence,
                }
            )
            break  # one finding per line, like a reviewer who has made their point
    return findings


# --- judging ----------------------------------------------------------------------

# Two texts describe the same issue when they share a topic. Crude on purpose: a real judge is a
# model, and anything cleverer here would be a second implementation to keep honest.
TOPICS: dict[str, tuple[str, ...]] = {
    "unwrap": ("unwrap",),
    "expect": ("expect(", "expect(\"", ".expect"),
    "panic": ("panic",),
    "discarded": ("discard", "swallow", "ignored", "let _", "no trace", "leaves no trace"),
    "except": ("except", "exception"),
    "traceback": ("traceback", "from", "chain"),
    "lock": ("lock", "concurrently"),
    "notnull": ("not null", "default", "backfill"),
    "dropcol": ("drop column", "dropping it", "still reads"),
}


def topics_of(text: str) -> set[str]:
    lowered = text.lower()
    return {name for name, words in TOPICS.items() if any(w in lowered for w in words)}


def judge(expected: str, finding: str) -> dict[str, Any]:
    shared = topics_of(expected) & topics_of(finding)
    if shared:
        return {
            "matched": True,
            "confidence": 0.9,
            "reason": f"both describe the same issue ({', '.join(sorted(shared))})",
        }
    return {
        "matched": False,
        "confidence": 0.8,
        "reason": "the finding is about something else at this location",
    }


# --- drafting a guidance change ---------------------------------------------------


@dataclass(frozen=True)
class Patch:
    """A repair the stub knows how to make, and the failure that calls for it."""

    when: re.Pattern[str]
    # Skipped when the guidance already says this, so a second improve run is a no-op rather than
    # a pile of duplicate rules.
    already: str
    text: str


PATCHES: tuple[Patch, ...] = (
    Patch(
        when=re.compile(r"MISSED.*?\.expect\(", re.S),
        already="expect",
        text=(
            "- **R1b — `.expect()` is `.unwrap()` with a message.** It panics on exactly the same "
            "input. Everything R1 says about `.unwrap()` applies to `.expect()` unchanged."
        ),
    ),
    Patch(
        when=re.compile(r"MISSED.*?let\s+_\s*=", re.S),
        already="discard",
        text=(
            "- **R2 — no discarded `Result`s.** `let _ = f()` throws away a failure silently. "
            "Propagate it with `?`, or handle it and log what happened."
        ),
    ),
    Patch(
        when=re.compile(r"FALSELY FLAGGED.*?(#\[test\]|#\[cfg\(test\)\]|_test\.rs|tests/)", re.S),
        already="does not apply",
        text=(
            "These rules do not apply inside test code (`#[cfg(test)]` modules, `*_test.rs`, "
            "`tests/`), where `.unwrap()` is idiomatic and a panic is how a test reports failure."
        ),
    ),
    Patch(
        when=re.compile(r"MISSED.*?except\b[^:]*:", re.S),
        already="silently",
        text=(
            "- **P1 — never silently swallow an exception.** A handler whose body is `pass` or a "
            "bare `return` hides the failure from every caller. Log it and re-raise, or handle it "
            "and say in a comment why swallowing is correct here."
        ),
    ),
    Patch(
        when=re.compile(r"MISSED.*?CREATE\s+(UNIQUE\s+)?INDEX", re.S | re.I),
        already="concurrently",
        text=(
            "- **S1 — build indexes CONCURRENTLY.** A plain `CREATE INDEX` holds an exclusive "
            "lock for the whole build, which on a large table is an outage."
        ),
    ),
    Patch(
        when=re.compile(r"MISSED.*?ADD\s+COLUMN.*?NOT\s+NULL", re.S | re.I),
        already="not null",
        text=(
            "- **S2 — a NOT NULL column needs a DEFAULT.** Without one the statement fails against "
            "every existing row, so the migration cannot run on a populated table."
        ),
    ),
    Patch(
        when=re.compile(r"MISSED.*?context\.Background\(\)", re.S),
        already="context.background",
        text=(
            "- **G1 — name the construct, not the principle.** An outbound call built on "
            "`context.Background()` has no deadline at all. Use `context.WithTimeout` and pass the "
            "derived context, so a hung dependency fails in seconds rather than holding the worker."
        ),
    ),
)

_GUIDANCE_SECTION = re.compile(r"^## Current guidance\s*\n(.*?)(?=^## )", re.S | re.M)
_CASE_ID = re.compile(r"case `([^`]+)`")


def draft(prompt: str) -> dict[str, Any]:
    """A guidance change for the failures in an improve prompt.

    Only ever appends. A stub that rewrote the whole body would be the more impressive demo and the
    less honest one — a real model's rewrites are exactly where guidance quietly loses rules, which
    is what the console's diff pane exists to catch.
    """
    section = _GUIDANCE_SECTION.search(prompt)
    if section is None:
        return {
            "body": "",
            "rationale": (
                "the demo stub could not find a '## Current guidance' section in this prompt, so "
                "it has nothing to change. Check the skill's improve/prompt.md."
            ),
            "targeted_cases": [],
        }
    guidance = section.group(1).strip()
    applied = [
        patch.text
        for patch in PATCHES
        if patch.when.search(prompt) and patch.already not in guidance.lower()
    ]
    if not applied:
        return {
            "body": guidance,
            "rationale": "nothing in these failures calls for a change the stub knows how to make.",
            "targeted_cases": [],
        }
    return {
        "body": guidance + "\n\n" + "\n\n".join(applied) + "\n",
        "rationale": (
            f"Adds {len(applied)} thing(s) the failures show are missing, and keeps every existing "
            f"rule — the sample of failures says nothing about the rules that are working."
        ),
        "targeted_cases": sorted(set(_CASE_ID.findall(prompt))),
    }


# --- the server -------------------------------------------------------------------


# What a drafted expectation looks like for each construct the stub recognises. Keyed off the
# diff, not off any rule — the drafter is never shown the guidance, and neither is this.
EXPECTATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^\+.*\.unwrap\(", re.M),
        "unwrap on this lookup panics when the value is absent, which is a normal error path",
    ),
    (
        re.compile(r"^\+.*\.expect\(", re.M),
        "expect panics when the value is absent, exactly as unwrap would, and takes the process "
        "down on a routine failure",
    ),
    (
        re.compile(r"^\+\s*let\s+_\s*=", re.M),
        "the Result of this call is discarded, so a failure leaves no trace and the caller sees "
        "success",
    ),
    (
        re.compile(r"^\+\s*raise\s+\w*Error\b(?!.*\bfrom\b)", re.M),
        "the re-raise drops the original exception, so the traceback stops at the wrapper and the "
        "underlying failure is unrecoverable from the logs",
    ),
    (
        re.compile(r"^\+.*context\.Background\(\)", re.M),
        "the request is sent with no deadline, so a hung gateway blocks the worker indefinitely",
    ),
    (
        re.compile(r"^\+.*CREATE\s+INDEX(?!.*CONCURRENTLY)", re.M | re.I),
        "building this index without CONCURRENTLY holds an exclusive lock on the table for the "
        "whole build",
    ),
)


def draft_expectation(prompt: str) -> dict[str, Any]:
    """A standalone description of the problem, from the evidence in a triage prompt."""
    for pattern, sentence in EXPECTATIONS:
        if pattern.search(prompt):
            return {
                "semantic": sentence,
                "rationale": "describes the construct and why it fails here, without naming a rule",
            }
    return {
        "semantic": "",
        "rationale": "the demo stub recognises no construct in this diff; write it yourself",
    }


def respond_to(system: str, user: str) -> dict[str, Any]:
    """Dispatch on the schema Whetstone embedded in the system prompt."""
    if "SemanticDraft" in system:
        return draft_expectation(user)
    if "GuidanceProposal" in system:
        return draft(user)
    if "JudgeVerdict" in system:
        expected = _between(user, "Expected issue:", "\n")
        finding = _between(user, "Reviewer finding:", "\n")
        return judge(expected, finding)
    if "LLMFindingList" in system:
        # The guidance is everything before the output instructions Whetstone appends. Passing the
        # whole system prompt would let the schema's own words ("findings", "severity") read as
        # rules the skill asked for.
        guidance = system.split("Report every issue")[0]
        return {"findings": review(guidance, user)}
    raise ValueError("the stub was asked for a schema it does not know")


# --- running a skill as an agent ---------------------------------------------------
#
# The single-shot path above is not how a skill runs in real code. A skill is a *folder*, and
# `agent: enabled` runs it the way an agent runtime would: `SKILL.md` as the instruction set, its
# companion pages fetched on demand with `read_skill_file`, source files through `read_file`/`grep`,
# the skill's own scripts as tools, and a terminal tool it must call to answer.
#
# Without tool support here the demo could only ever exercise the single-shot reviewer — so the one
# thing a user most needs to satisfy themselves about, that the loop improves skills *as they will
# actually run*, was the one thing the demo could not show. The stub therefore holds a real
# tool-calling conversation: it investigates first (reading a page, exactly as the harness intends)
# and then calls the terminal tool.

# The terminal tool of each kind of step, in the order they are checked. Whichever one is on offer
# tells the stub what it is being asked to produce.
TERMINALS = ("submit_findings", "submit_guidance", "submit_expectation", "submit_work")

# `read_skill_file`'s description ends "Available pages: a.md, b.md" (see `agent.builtins`), which
# is how the stub learns what there is to read without being told the skill's layout.
_PAGES = re.compile(r"Available pages:\s*(.+)")


def available_pages(description: str) -> list[str]:
    found = _PAGES.search(description)
    return [p.strip() for p in found.group(1).split(",") if p.strip()] if found else []


def _offered(tools: list[dict[str, Any]]) -> dict[str, str]:
    """Tool name → description, from the OpenAI `tools` array."""
    out: dict[str, str] = {}
    for tool in tools:
        function = tool.get("function") or {}
        name = function.get("name")
        if name:
            out[str(name)] = str(function.get("description") or "")
    return out


def _already_called(messages: list[dict[str, Any]]) -> set[str]:
    return {
        str((call.get("function") or {}).get("name") or "")
        for message in messages
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    }


def _first_user(messages: list[dict[str, Any]]) -> str:
    return next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "")


def guidance_of(system: str) -> str:
    """The skill's own instructions out of an agent system prompt.

    `SkillAgent._system` puts the body under `# Your instructions` and appends its own sections
    after it. Taking the whole prompt instead would let the runtime preamble's vocabulary — it
    mentions tools, findings and files — read as rules the skill asked for, and the stub would fire
    rules no guidance requested.
    """
    if "# Your instructions" not in system:
        return system
    body = system.split("# Your instructions", 1)[1]
    for heading in ("\n# This skill's other files", "\n# Context you were given", "\n# The source"):
        body = body.split(heading, 1)[0]
    return body


def pages_read(messages: list[dict[str, Any]]) -> str:
    """Everything the agent fetched with `read_skill_file`, concatenated.

    Folded into the guidance the stub reviews with, and that is the whole point of the harness
    rather than a flourish: a skill is a folder, `SKILL.md` links to its reference pages, and a rule
    that lives on a page only reaches the review because the agent went and read it. If the stub
    reviewed with the body alone, reading a page would change nothing and the demo would show an
    agent going through the motions.
    """
    return "\n".join(
        str(m.get("content") or "") for m in messages if m.get("role") == "tool"
    )


def _answer(terminal: str, system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """What to hand the terminal tool — the same functions the single-shot path uses."""
    task = _first_user(messages)
    if terminal == "submit_findings":
        guidance = guidance_of(system) + "\n" + pages_read(messages)
        return {"findings": review(guidance, task)}
    if terminal == "submit_guidance":
        return draft(task)
    if terminal == "submit_expectation":
        return draft_expectation(task)
    # `submit_work`: the stub cannot write a program. Saying so is the honest answer — a made-up
    # summary would be graded by a real test run and fail anyway, but confusingly.
    return {
        "summary": (
            "the demo stub cannot do task work — it is a handful of regexes. Point WHETSTONE_LLM "
            "at a real backend to run task skills."
        ),
        "files_written": [],
    }


def converse(
    system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], forced: str
) -> dict[str, Any]:
    """One turn of a tool-calling conversation, as an assistant chat-completions message.

    Deliberately investigates before answering. A stub that went straight to the terminal tool
    would still make the loop *run*, but every trajectory would read as "answered immediately" —
    and the trajectory is load-bearing: a gate compares what each side looked at, and the console
    shows it. A demo whose agents never read anything would make that machinery look inert.
    """
    offered = _offered(tools)
    terminal = next((name for name in TERMINALS if name in offered), "")
    called = _already_called(messages)

    if forced:
        # `tool_choice` — the harness has run out of steps and is making the agent answer.
        return _tool_message(forced, _answer(forced if forced in TERMINALS else terminal,
                                             system, messages))

    if "read_skill_file" in offered and "read_skill_file" not in called:
        # The description carries the available pages; read the first, which is what a skill whose
        # SKILL.md links to a reference page would do.
        pages = available_pages(offered["read_skill_file"])
        if pages:
            return _tool_message("read_skill_file", {"path": pages[0]})

    if terminal:
        return _tool_message(terminal, _answer(terminal, system, messages))
    # No terminal tool on offer at all: say so as text rather than calling something at random.
    return {"role": "assistant", "content": "the demo stub was offered no tool it recognises"}


def _tool_message(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def _between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    rest = text.split(start, 1)[1]
    return rest.split(end, 1)[0].strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": f"no route {self.path}"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            messages = body.get("messages", [])
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            tools = body.get("tools") or []
            if tools:
                # A tool-calling request: the skill is being *run*, not prompted. Decided by the
                # request rather than by the system prompt, because that is what actually differs.
                choice = body.get("tool_choice")
                forced = ""
                if isinstance(choice, dict):
                    forced = str((choice.get("function") or {}).get("name") or "")
                message = converse(system, messages, tools, forced)
                finish = "tool_calls" if message.get("tool_calls") else "stop"
            else:
                user = next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
                )
                message = {"role": "assistant", "content": json.dumps(respond_to(system, user))}
                finish = "stop"
        except Exception as exc:  # noqa: BLE001 - a stub must answer, not take the console down
            self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}"}})
            return
        self._send(
            200,
            {
                "id": "chatcmpl-demo",
                "object": "chat.completion",
                "model": MODEL_NAME,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        """`/v1/models`, so `whetstone llm check` can see the endpoint is alive."""
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})
            return
        self._send(404, {"error": {"message": f"no route {self.path}"}})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence. The console's own output is what the operator is reading."""


def serve(port: int) -> ThreadingHTTPServer:
    """Start the stub on a background thread and return the server, for the caller to shut down."""
    import threading

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, name="demo-stub-model", daemon=True).start()
    return server


if __name__ == "__main__":
    import sys

    chosen = int(sys.argv[1]) if len(sys.argv) > 1 else 8789
    print(f"demo stub model on http://127.0.0.1:{chosen}/v1  (ctrl-c to stop)")
    ThreadingHTTPServer(("127.0.0.1", chosen), Handler).serve_forever()
