from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from whetstone.core.harness import run_skill
from whetstone.core.loader import load_skill
from whetstone.domain.change import CodeChange, FileChange, parse_hunk_added_lines
from whetstone.domain.enums import Severity
from whetstone.domain.refs import RepoRef
from whetstone.domain.skill import Skill
from whetstone.judge import DeterministicJudge
from whetstone.llm import FakeLLMClient
from whetstone.reviewer.llm_reviewer import (
    LLMFinding,
    LLMFindingList,
    LLMReviewer,
    number_diff,
)
from whetstone.wiki import SkillWiki, WikiEntry, WikiPage

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "code-review-rust-error-handling"
REPO = RepoRef.parse("gitlab:acme/payments")
DIFF = "@@ -40,5 +40,6 @@\n     x\n+        let row = self.db.get(id).unwrap();\n"


def _skill() -> Skill:
    return load_skill(SKILL_DIR)


def _change() -> CodeChange:
    return CodeChange(
        repo=REPO,
        files=[
            FileChange(
                path="src/handlers/charge.rs",
                added=parse_hunk_added_lines(DIFF),
                raw_diff=DIFF,
            )
        ],
    )


def test_reviewer_converts_llm_findings_to_domain_findings() -> None:
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        return LLMFindingList(
            findings=[
                LLMFinding(
                    path="src/handlers/charge.rs",
                    line=41,
                    severity="error",
                    message="avoid unwrap() in service code",
                    rule_id="R1",
                    confidence=0.9,
                )
            ]
        )

    findings = LLMReviewer(FakeLLMClient(handler)).review(_skill(), _change())

    assert len(findings) == 1
    f = findings[0]
    assert f.skill_id == "code-review-rust-error-handling"
    assert f.path == "src/handlers/charge.rs"
    assert f.line == 41
    assert f.severity is Severity.error
    assert f.rule_id == "R1"
    assert f.confidence == 0.9


def test_reviewer_prompt_carries_skill_body_and_diff() -> None:
    captured: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        captured["system"] = system
        captured["user"] = user
        return LLMFindingList(findings=[])

    client = FakeLLMClient(handler)
    LLMReviewer(client).review(_skill(), _change())

    assert "unwrap" in captured["system"]  # skill guidance body
    assert ".unwrap()" in captured["user"]  # the diff under review
    assert client.calls[0].effort == "high"


def _capture() -> tuple[dict[str, str], object]:
    captured: dict[str, str] = {}

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        captured["system"] = system
        captured["user"] = user
        return LLMFindingList(findings=[])

    return captured, handler


def test_wiki_pages_for_the_touched_paths_reach_the_system_prompt() -> None:
    skill = _skill().model_copy(
        update={
            "wiki": SkillWiki(
                entries=[
                    WikiEntry(page="handlers", paths=["src/handlers/**"]),
                    WikiEntry(page="billing", paths=["src/billing/**"]),
                ],
                pages={
                    "handlers": WikiPage(id="handlers", title="Handlers", text="Charge flow notes"),
                    "billing": WikiPage(id="billing", title="Billing", text="Ledger notes"),
                },
            )
        }
    )
    captured, handler = _capture()
    LLMReviewer(FakeLLMClient(handler)).review(skill, _change())

    assert "Charge flow notes" in captured["system"]
    assert "Ledger notes" not in captured["system"]  # its glob does not cover the changed file


def test_guidance_precedes_wiki_context_in_the_prompt() -> None:
    """The stable text stays in front, so the cacheable prefix is as long as possible."""
    skill = _skill().model_copy(
        update={
            "wiki": SkillWiki(
                entries=[WikiEntry(page="h", paths=["src/**"])],
                pages={"h": WikiPage(id="h", title="Handlers", text="Charge flow notes")},
            )
        }
    )
    captured, handler = _capture()
    LLMReviewer(FakeLLMClient(handler)).review(skill, _change())

    assert captured["system"].index("unwrap") < captured["system"].index("Charge flow notes")


def test_wiki_is_labelled_as_context_not_as_rules() -> None:
    """Background that reads as guidance would invent findings the skill never authorised."""
    skill = _skill().model_copy(
        update={
            "wiki": SkillWiki(
                entries=[WikiEntry(page="h", paths=["src/**"])],
                pages={"h": WikiPage(id="h", title="Handlers", text="Charge flow notes")},
            )
        }
    )
    captured, handler = _capture()
    LLMReviewer(FakeLLMClient(handler)).review(skill, _change())

    assert "NOT review guidance" in captured["system"]


def test_skill_without_a_wiki_produces_the_prompt_it_always_did() -> None:
    captured, handler = _capture()
    LLMReviewer(FakeLLMClient(handler)).review(_skill(), _change())
    assert "Background on this codebase" not in captured["system"]


def test_the_diff_reaches_the_model_with_new_file_line_numbers() -> None:
    """The number a finding is scored against is stated, not left to the model's arithmetic."""
    captured, handler = _capture()
    LLMReviewer(FakeLLMClient(handler)).review(_skill(), _change())

    assert "41 | +        let row = self.db.get(id).unwrap();" in captured["user"]
    assert "do not count lines yourself" in captured["user"]


def test_numbering_skips_deleted_lines_and_diff_headers() -> None:
    numbered = number_diff(
        "diff --git a/a.rs b/a.rs\n"
        "--- a/a.rs\n"
        "+++ b/a.rs\n"
        "@@ -18,4 +18,4 @@\n"
        " fn settle() {\n"
        "-    old();\n"
        "+    new();\n"
        " }\n"
    )
    gutters = [line.split(" | ", 1)[0].strip() for line in numbered.splitlines()]

    # Headers and the removal carry no new-file line; the removal does not advance the count.
    assert gutters == ["", "", "", "", "18", "", "19", "20"]


def test_numbering_is_width_aligned_across_hunks() -> None:
    numbered = number_diff("@@ -1,2 +1,2 @@\n x\n+y\n@@ -98,2 +98,3 @@\n a\n+b\n c\n")
    gutters = [line.split(" | ", 1)[0] for line in numbered.splitlines()]

    assert gutters == ["   ", "  1", "  2", "   ", " 98", " 99", "100"]


def test_a_diff_with_no_hunk_header_is_left_alone() -> None:
    """Nothing is numbered on a guess: without a hunk header there is no new-file origin."""
    numbered = number_diff("diff --git a/a.rs b/a.rs\n+ orphan line\n")

    assert numbered.splitlines() == ["  | diff --git a/a.rs b/a.rs", "  | + orphan line"]


def test_reviewer_plugs_into_run_skill() -> None:
    # Catch the two real defects, stay silent on the noflag cases: an unwrap finding on charge.rs,
    # a swallowed-error finding on refund.rs's `let _ =` case, and nothing on the clean files.
    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        if "refund.rs" in user:
            if "let _ =" in user:
                return LLMFindingList(
                    findings=[LLMFinding(path="src/handlers/refund.rs", line=32, message="swallow")]
                )
            return LLMFindingList(findings=[])
        if "charge_test.rs" in user:
            return LLMFindingList(findings=[])
        return LLMFindingList(
            findings=[LLMFinding(path="src/handlers/charge.rs", line=41, message="unwrap")]
        )

    score = run_skill(_skill(), LLMReviewer(FakeLLMClient(handler)), DeterministicJudge())
    assert score.recall == 1.0
    assert score.fp_rate == 0.0
