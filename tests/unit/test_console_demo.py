"""The demo stub has to keep speaking the prompt Whetstone actually sends.

The demo is the only place most people ever see the loop work, and it rotted without a sound. The
reviewer prompt gained a line-number gutter (`llm_reviewer.number_diff`) because models cannot add
up hunk offsets reliably; the stub still looked for lines starting with `+`. With the gutter in
place none did, so it produced no findings, every skill in the demo scored 0.00, and the README went
on describing a 0.33 baseline that rose to 1.00 after an improve. Nothing errored. The demo simply
stopped demonstrating anything, and there was no test to notice.

So these tests couple the stub to the real prompt builders rather than to a remembered format.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from whetstone.core.loader import load_skill
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.refs import RepoRef
from whetstone.reviewer.llm_reviewer import number_diff
from whetstone.sampling import partition_of
from whetstone.steps import SamplePolicy

DEMO = Path(__file__).resolve().parents[2] / "examples" / "console-demo"
sys.path.insert(0, str(DEMO))

seed = pytest.importorskip("seed")
stub_model = pytest.importorskip("stub_model")

GUIDANCE = """# Rust error handling review

- **R1 — no `.unwrap()` in service code.** Replace it with `?` and a mapped error.
"""

DIFF = """diff --git a/src/handlers/charge.rs b/src/handlers/charge.rs
--- a/src/handlers/charge.rs
+++ b/src/handlers/charge.rs
@@ -40,5 +40,6 @@ impl ChargeHandler {
     pub fn charge(&self, id: u64) -> Response {
-        let row = self.db.get(id);
+        let row = self.db.get(id).unwrap();
         Response::ok(row)
     }
 }
"""


def test_the_stub_reads_the_diff_the_reviewer_actually_sends() -> None:
    """The regression that made the whole demo score zero.

    `number_diff` is imported from the real reviewer, so this fails the day the prompt shape changes
    again rather than the day someone opens the demo and wonders why nothing is flagged.
    """
    findings = stub_model.review(GUIDANCE, number_diff(DIFF))
    assert [f["rule_id"] for f in findings] == ["R1"]
    assert findings[0]["line"] == 41  # the gutter's number, not one counted from the hunk header


def test_a_bare_diff_still_works() -> None:
    """Both shapes, so the stub is not merely coupled to today's prompt in the other direction."""
    assert [f["rule_id"] for f in stub_model.review(GUIDANCE, DIFF)] == ["R1"]


def test_the_stub_stays_silent_when_the_guidance_does_not_ask() -> None:
    """The mechanism the whole demo rests on: an edit to the guidance moves the next score."""
    assert stub_model.review("# Rust review\n\n- **R9 — prefer iterators.**\n", DIFF) == []


# What each demo skill catches at v1, before anyone improves anything — the demo's whole starting
# story in one table. The regression that made the stub blind flipped every one of these to empty
# at once while the README went on quoting the old numbers, so this is the assertion that would
# have caught it. Changing the demo means changing this deliberately.
STARTING_BEHAVIOUR: dict[str, tuple[list[str], list[str]]] = {
    #                          caught                            falsely flagged
    "rust-error-handling": (["unwrap-in-handler"], ["unwrap-in-test"]),
    "python-service-errors": (["swallow-in-charge-worker"], []),
    "sql-migration-safety": (["index-without-concurrently"], []),
    # Catches nothing by design: its guidance states the principle ("every outbound call needs a
    # deadline") and never names `context.Background()`. That gap is what the walkthrough's improve
    # step closes, and `test_the_agent_skill_catches_its_case_once_improved` proves it closes.
    "go-timeout-guard": ([], []),
}


def _demo_skill(tmp_path: Path, skill_id: str):  # type: ignore[no-untyped-def]
    """One demo skill written out and loaded, straight from the committed seed definition.

    Built from `seed.SKILLS` rather than from `workspace/`, which is generated and gitignored — so
    this runs everywhere instead of skipping on any machine that has not launched the demo, which
    is every CI machine and the reason the rot went unnoticed in the first place.
    """
    root = tmp_path / skill_id
    for relative, content in seed.SKILLS[skill_id].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return load_skill(root)


def _flagged(skill, guidance: str, kind: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        c.id
        for c in skill.eval_cases
        if c.kind == kind and stub_model.review(guidance, number_diff(c.change.to_unified_diff()))
    ]


def _reachable(skill) -> str:  # type: ignore[no-untyped-def]
    """The guidance a reviewer can actually see, whichever way the skill is run.

    The built-in reviewer concatenates the folder into one prompt; an agent fetches the pages with
    `read_skill_file`. Either way the pages are reachable, so they count as guidance here.
    """
    return skill.body + "\n" + "\n".join(p.text for p in skill.pages)


@pytest.mark.parametrize("skill_id", sorted(STARTING_BEHAVIOUR))
def test_each_demo_skill_starts_where_the_readme_says(tmp_path: Path, skill_id: str) -> None:
    skill = _demo_skill(tmp_path, skill_id)
    caught, noisy = STARTING_BEHAVIOUR[skill_id]
    guidance = _reachable(skill)
    assert _flagged(skill, guidance, "should_catch") == caught
    assert _flagged(skill, guidance, "should_not_flag") == noisy


def test_the_agent_skill_catches_its_case_once_improved(tmp_path: Path) -> None:
    """The payoff of the demo's flagship walkthrough, guarded.

    `go-timeout-guard` misses because its guidance names the principle and not the construct. The
    improve step's job is to add the construct — and if the two ever drift apart the tour ends on
    "nothing in these failures calls for a change", which reads like the drafter's judgement rather
    than a broken demo.
    """
    skill = _demo_skill(tmp_path, "go-timeout-guard")
    assert _flagged(skill, _reachable(skill), "should_catch") == []

    patch = next(p for p in stub_model.PATCHES if p.already == "context.background")
    improved = _reachable(skill) + "\n" + patch.text
    assert _flagged(skill, improved, "should_catch") == ["background-context-in-gateway"]
    # …and it still stays quiet where it should. A rule that catches everything is not an
    # improvement, and the gate would refuse it.
    assert _flagged(skill, improved, "should_not_flag") == []


def test_the_walkthrough_case_is_one_the_loop_can_learn_from() -> None:
    """The README promotes this case and gates a change against it — which only works from the
    train partition.

    `sampling.partition_of` hashes the case id, so the demo's flagship walkthrough silently depends
    on how one string hashes: a holdout case is withheld from the drafter and refused as a gate
    target, both correctly, and the tour would dead-end on its most important step. Renaming the
    signal is fine; renaming it without checking this is not, so the dependency is asserted rather
    than left as a comment.
    """
    fraction = SamplePolicy().holdout_fraction
    walkthrough = next(s for s in seed.SIGNALS if s[0].startswith("mr-1918"))
    assert partition_of(walkthrough[0], fraction) == "train"


# --- the agent path: the stub must hold a real tool-calling conversation ------------


def _tools(*names: str, pages: str = "") -> list[dict]:
    """The OpenAI `tools` array, as `OpenAICompatibleClient.converse` builds it."""
    described = {
        "read_skill_file": (
            f"Read one of this skill's own reference pages. Available pages: {pages}"
        ),
    }
    return [
        {
            "type": "function",
            "function": {"name": n, "description": described.get(n, ""), "parameters": {}},
        }
        for n in names
    ]


AGENT_SYSTEM = f"""You are running as an agent.

# Your instructions

{GUIDANCE}

# This skill's other files

- references/timeouts.md
"""


def test_the_agent_investigates_before_it_answers() -> None:
    """A stub that went straight to the terminal tool would still make the loop run — and every
    trajectory would read "answered immediately", making the gate's divergence check look inert."""
    message = stub_model.converse(
        AGENT_SYSTEM,
        [{"role": "user", "content": number_diff(DIFF)}],
        _tools("read_skill_file", "submit_findings", pages="references/timeouts.md"),
        "",
    )
    [call] = message["tool_calls"]
    assert call["function"]["name"] == "read_skill_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "references/timeouts.md"}


def test_the_agent_answers_once_it_has_read() -> None:
    conversation = [
        {"role": "user", "content": number_diff(DIFF)},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_skill_file"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "a page about deadlines"},
    ]
    message = stub_model.converse(
        AGENT_SYSTEM,
        conversation,
        _tools("read_skill_file", "submit_findings", pages="references/timeouts.md"),
        "",
    )
    [call] = message["tool_calls"]
    assert call["function"]["name"] == "submit_findings"
    findings = json.loads(call["function"]["arguments"])["findings"]
    assert [f["rule_id"] for f in findings] == ["R1"]


def test_a_rule_that_lives_on_a_page_only_fires_once_the_agent_reads_it() -> None:
    """The harness earning its keep, rather than going through the motions.

    A skill is a folder: `SKILL.md` links to reference pages, and a rule that lives on a page
    reaches the review only because the agent went and fetched it. If the stub reviewed with the
    body alone, reading a page would change nothing and the demo would prove nothing.
    """
    vague = "You are running as an agent.\n\n# Your instructions\n\nReview outbound calls.\n"
    tools = _tools("read_skill_file", "submit_findings", pages="references/timeouts.md")
    go = """diff --git a/c.go b/c.go
--- a/c.go
+++ b/c.go
@@ -1,2 +1,3 @@
 func f() {
+    ctx := context.Background()
 }
"""
    conversation = [{"role": "user", "content": number_diff(go)}]
    silent = stub_model.converse(vague, conversation, tools, "submit_findings")
    assert json.loads(silent["tool_calls"][0]["function"]["arguments"])["findings"] == []

    # …and again, after the agent has read a page that names the construct.
    read = [
        *conversation,
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_skill_file"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "never build on context.Background()"},
    ]
    answered = stub_model.converse(vague, read, tools, "submit_findings")
    findings = json.loads(answered["tool_calls"][0]["function"]["arguments"])["findings"]
    assert [f["rule_id"] for f in findings] == ["G1"]


def test_a_forced_tool_choice_is_honoured() -> None:
    """`tool_choice` is how the harness makes an agent that is out of steps produce its answer."""
    message = stub_model.converse(
        AGENT_SYSTEM,
        [{"role": "user", "content": number_diff(DIFF)}],
        _tools("read_skill_file", "submit_findings", pages="references/timeouts.md"),
        "submit_findings",
    )
    # Forced, so it answers instead of investigating first.
    assert message["tool_calls"][0]["function"]["name"] == "submit_findings"


def test_the_improve_terminal_returns_a_guidance_draft() -> None:
    prompt = (
        "### MISSED - case `x` (c.go)\n```diff\n+    ctx := context.Background()\n```\n"
        "## Current guidance\n\nReview outbound calls.\n\n## Next\n"
    )
    message = stub_model.converse(
        "system", [{"role": "user", "content": prompt}], _tools("submit_guidance"), ""
    )
    answer = json.loads(message["tool_calls"][0]["function"]["arguments"])
    assert "context.WithTimeout" in answer["body"]


def test_the_gutter_is_stripped_without_touching_the_content() -> None:
    numbered = number_diff(DIFF)
    assert " | " in numbered  # the gutter is really there
    restored = [stub_model.strip_gutter(line) for line in numbered.splitlines()]
    # Round-trips to the original diff, so the stub is reading exactly what a bare parser would.
    assert restored == DIFF.splitlines()
    # And the result still parses as a diff, which is the property that actually matters.
    change = parse_unified_diff("\n".join(restored), RepoRef.parse("local:x"))
    assert change.files[0].path == "src/handlers/charge.rs"
