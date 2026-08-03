"""Turning a triaged candidate into an eval case.

`corpus/builder.py` sets a candidate's expectation text to the raw body of the first review comment.
In real repositories that is "nit: use ? here", "see above", or a paragraph about something else —
and it becomes the ground truth the LLM judge scores every finding against. So the human step this
module exists to support is **rewriting**, not rubber-stamping, and the CLI's `corpus promote` (a
verbatim file copy) never supported it.

Nothing is written until the edited case has been round-tripped through `load_skill`: the files are
rendered, loaded back through the exact production parser, and only then handed to the caller. A
case that cannot be read is never committed.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from whetstone.candidates import CandidateEntry
from whetstone.core.loader import PROMOTED_CASES_DIR, SkillLoadError, load_skill
from whetstone.corpus.model import CandidateCase
from whetstone.domain.enums import Severity
from whetstone.domain.eval_model import (
    SOURCE_MINED_MR,
    CaseTier,
    EvalCase,
    EvalKind,
    Provenance,
)
from whetstone.naming import describe_unsafe, is_safe_segment

CASE_FILE = "case.yaml"
DIFF_FILE = "change.diff"
META_FILE = "meta.yaml"

# The shape `service.rule_ids` can find in a SKILL.md body ("- **R1 — …**"). Enforced here so a
# provenance key can never name a rule the body regex could not produce: such a key would be
# reported as a declared rule forever and, having no findings to cite it, as a permanently untested
# one.
_RULE_ID = re.compile(r"^[A-Z][A-Z0-9]*[0-9]$")


class CaseEdits(BaseModel):
    """The human's corrections to a candidate, applied before it becomes an eval case."""

    case_id: str
    skill_id: str
    kind: EvalKind
    semantic: str = ""
    path: str
    line_range: tuple[int, int] | None = None
    severity_min: Severity | None = None
    expectation_id: str = "e1"
    # Optional: the rule this case is evidence for. Setting it files the source MR under that rule
    # in `meta.yaml`, which is the only record of *why* a piece of guidance exists. Left empty, the
    # case still lands — plenty of cases test a skill without justifying any one rule.
    rule_id: str = ""
    # Set when a triage step drafted the semantic and the operator kept it. Carried into the case's
    # provenance, so a corpus can be asked how its expectations were written.
    semantic_drafted_by: str = ""
    # "archive" promotes the case straight to the low-weight tier — the disposition for a
    # candidate that duplicates existing coverage but is still worth counting as regression
    # insurance. The evidence (provenance, ref) is preserved either way; only the draw weight
    # differs. See `curation.similar_cases` for how the console surfaces the duplicates.
    tier: CaseTier = "active"

    @property
    def must(self) -> str:
        """Derived, never asked for: a `should_catch` case whose expectation says `not_appear` is
        incoherent, so the UI has no way to express it."""
        return "appear" if self.kind == "should_catch" else "not_appear"


class PreparedCase(BaseModel):
    """A validated case, ready to write. `files` are repo-relative paths → contents."""

    skill_id: str
    case_id: str
    files: dict[str, str]
    case: EvalCase


def edits_from(entry: CandidateEntry, *, skill_id: str | None = None) -> CaseEdits:
    """Seed the edit form from a candidate — what the console shows before anyone touches it.

    A *mined* region the diff does not touch is seeded as the whole file. Both minting paths already
    make that call — `build_candidates` for a thread anchored to expanded context, and
    `candidate_from_finding` for a stray cited line — but only for candidates minted since those
    guards landed. A queue filled before them still holds anchors that `_check_region` will refuse,
    and re-mining the corpus is the only other way to reach them. So the fallback runs again on the
    way *out* of the store, where it also repairs what is already on disk.

    Only for mined regions. A `review_miss` region is one a person typed with the diff in front of
    them, and widening that would discard what they said; `_check_region` refuses it instead and
    names the lines the change does touch, which is the answer they can act on.
    """
    candidate = entry.candidate
    first = candidate.expect[0] if candidate.expect else None
    path = first.where.path if first else (
        candidate.change.files[0].path if candidate.change.files else ""
    )
    return CaseEdits(
        case_id=candidate.id,
        skill_id=skill_id or candidate.suggested_skill or "",
        kind=candidate.kind,
        semantic=first.semantic if first else "",
        path=path,
        line_range=_anchored_range(candidate, path, first.where.line_range) if first else None,
        severity_min=first.severity_min if first else None,
        expectation_id=first.id if first else "e1",
        rule_id=candidate.suggested_rule_id,
    )


def _anchored_range(
    candidate: CandidateCase, path: str, line_range: tuple[int, int] | None
) -> tuple[int, int] | None:
    """A mined `line_range` the change does not touch, reduced to None — meaning the whole file.

    `covers` treats a change carrying no hunk information as covering everything, so a synthesized
    diff keeps whatever range it was given.
    """
    if line_range is None or candidate.provenance.source != SOURCE_MINED_MR:
        return line_range
    file = candidate.change.file(path)
    return line_range if file is None or file.covers(line_range) else None


def render_case_yaml(entry: CandidateEntry, edits: CaseEdits) -> str:
    """The `case.yaml` an edited candidate becomes."""
    candidate = entry.candidate
    where: dict[str, Any] = {"path": edits.path}
    if edits.line_range is not None:
        where["line_range"] = list(edits.line_range)

    expectation: dict[str, Any] = {
        "id": edits.expectation_id,
        "must": edits.must,
        "where": where,
    }
    if edits.semantic:
        expectation["semantic"] = edits.semantic
    if edits.severity_min is not None:
        expectation["severity_min"] = edits.severity_min.name

    provenance: dict[str, Any] = {"source": candidate.provenance.source}
    if candidate.provenance.ref:
        provenance["ref"] = candidate.provenance.ref
    if candidate.provenance.human_signal:
        provenance["human_signal"] = candidate.provenance.human_signal
    if edits.semantic_drafted_by:
        provenance["semantic_drafted_by"] = edits.semantic_drafted_by

    payload: dict[str, Any] = {
        "id": edits.case_id,
        "kind": edits.kind,
        "repo": candidate.change.repo.slug,
        "base_ref": candidate.change.base_ref,
        "head_ref": candidate.change.head_ref,
        "change": DIFF_FILE,
        "provenance": provenance,
        "expect": [expectation],
    }
    if edits.tier != "active":
        # Written only when it says something — absent means active, and every case file written
        # before tiers existed reads that way.
        payload["tier"] = edits.tier
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def render_meta_yaml(existing: str | None, rule_id: str, provenance: Provenance) -> str:
    """`meta.yaml` with one more signal filed under `rule_id`.

    Round-tripped through YAML, so comments in the original are lost. That is the price of making
    this a structured edit rather than a text patch, and `meta.yaml` is documented as machine
    metadata — a mangled provenance block would be the worse trade.
    """
    data: dict[str, Any] = {}
    if existing and existing.strip():
        loaded = yaml.safe_load(existing)
        if loaded is not None and not isinstance(loaded, dict):
            raise SkillLoadError(f"{META_FILE} must be a mapping")
        data = loaded or {}

    block = data.get("provenance") or {}
    if not isinstance(block, dict):
        raise SkillLoadError(f"{META_FILE}: 'provenance' must map a rule id to a list of signals")

    signal: dict[str, Any] = {"source": provenance.source}
    if provenance.ref:
        signal["ref"] = provenance.ref
    if provenance.human_signal:
        signal["human_signal"] = provenance.human_signal

    signals = block.get(rule_id) or []
    if not isinstance(signals, list):
        raise SkillLoadError(f"{META_FILE}: provenance for {rule_id!r} must be a list")
    # Re-promoting two cases out of the same MR must not cite it twice.
    block[rule_id] = signals if signal in signals else [*signals, signal]

    data["provenance"] = block
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _check_semantic(entry: CandidateEntry, edits: CaseEdits) -> None:
    """Refuse an expectation nothing could be judged against.

    Two ways to write one, both of which produce a case that looks fine and measures nothing.

    **Empty.** `semantic` is what the LLM judge compares every finding to. Omitted, the judge is
    asked whether a finding matches "" and its verdicts are noise — so the case's outcome is noise,
    and it still counts towards recall like any other.

    **The skill's own words.** A confirmed ruling on the skill's own finding seeds `semantic` from
    that finding's message, because there is nothing else to seed it from. Promoted unedited, the
    case asks whether the reviewer says what the reviewer already said: it passes on the guidance
    that produced it and on any guidance that phrases the same complaint, so it can never fail and
    never constrains a change. That is the one case worth blocking rather than warning about,
    because unlike a badly-worded expectation it will never be discovered by failing.
    """
    semantic = edits.semantic.strip()
    if not semantic:
        raise SkillLoadError(
            "expectation needs a semantic: it is the ground truth every finding is judged "
            "against, and an empty one makes the case's result meaningless"
        )

    candidate = entry.candidate
    # Compare against the reviewer's own message (`seed_semantic`) when the candidate recorded it,
    # not against the current expectation. A confirmed finding promoted *with* a note has the note
    # as its expectation and the reviewer's message as the seed — the note is a standalone
    # description and must promote, while a still-unedited expectation (seed == expectation) must
    # not. Falling back to `expect[0].semantic` keeps the check working for candidates written
    # before `seed_semantic` existed.
    seeded = (candidate.seed_semantic or "").strip()
    if not seeded and candidate.expect:
        seeded = (candidate.expect[0].semantic or "").strip()
    if (
        candidate.provenance.source == "skill_review"
        and candidate.kind == "should_catch"
        and semantic == seeded
    ):
        raise SkillLoadError(
            "this expectation is still the skill's own finding, word for word. A case asserting "
            "that the reviewer says what it already said can never fail, so it would constrain "
            "nothing. Rewrite it as a standalone description of the problem."
        )


def prepare(
    entry: CandidateEntry,
    edits: CaseEdits,
    *,
    skills_root: Path | str,
    meta_yaml: str | None = None,
) -> PreparedCase:
    """Render and validate an edited candidate. Raises `SkillLoadError` if it wouldn't load.

    `skills_root` is only used to build repo-relative paths; nothing under it is written here.
    `meta_yaml` is the skill's current metadata, needed only when `edits.rule_id` is set — the
    updated copy is returned in `files` so the rule's evidence lands in the same commit as the case
    that demonstrates it, rather than in a follow-up nobody makes.
    """
    if not edits.skill_id:
        raise SkillLoadError("no target skill chosen for this candidate")
    if not edits.case_id:
        raise SkillLoadError("case id is required")
    if not edits.path:
        raise SkillLoadError("expectation needs a file path")
    _check_semantic(entry, edits)

    # Both become path segments in a commit, so neither may traverse.
    for value, what in ((edits.skill_id, "target skill"), (edits.case_id, "case id")):
        if not is_safe_segment(value):
            raise SkillLoadError(describe_unsafe(value, what))

    if edits.rule_id and not _RULE_ID.match(edits.rule_id):
        raise SkillLoadError(
            f"rule id {edits.rule_id!r} should look like R1 or SEC2 — uppercase, ending in a "
            "digit, matching how rules are tagged in SKILL.md"
        )

    if edits.line_range is not None:
        lo, hi = edits.line_range
        if lo > hi:
            raise SkillLoadError(
                f"line range {lo}–{hi} is inverted; the first line must not be after the last"
            )
        if lo < 1:
            raise SkillLoadError(f"line range starts at {lo}; line numbers begin at 1")

    diff = entry.diff or entry.candidate.change.to_unified_diff()
    if not diff.strip():
        raise SkillLoadError("candidate has no diff to review")

    case_yaml = render_case_yaml(entry, edits)
    case = _validate(case_yaml, diff, edits)

    # Promotion writes to `promoted_cases/`, not the eval corpus: a promoted case is a candidate for
    # the corpus, graduated into `eval_cases/` only once a person is satisfied it earns its place.
    skill_dir = Path(skills_root) / edits.skill_id
    base = skill_dir / PROMOTED_CASES_DIR / edits.case_id
    files = {
        (base / CASE_FILE).as_posix(): case_yaml,
        (base / DIFF_FILE).as_posix(): diff,
    }
    if edits.rule_id:
        files[(skill_dir / META_FILE).as_posix()] = render_meta_yaml(
            meta_yaml, edits.rule_id, entry.candidate.provenance
        )

    return PreparedCase(
        skill_id=edits.skill_id,
        case_id=edits.case_id,
        files=files,
        case=case,
    )


def _validate(case_yaml: str, diff: str, edits: CaseEdits) -> EvalCase:
    """Load the rendered case through the real parser, in a throwaway skill folder.

    Re-implementing the checks would let the two drift; this way a case that the console accepts is
    by construction one the harness can run.
    """
    with tempfile.TemporaryDirectory(prefix="whetstone-validate-") as tmp:
        skill_dir = Path(tmp) / edits.skill_id
        case_dir = skill_dir / "eval_cases" / edits.case_id
        case_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nid: {edits.skill_id}\n---\n\nvalidation stub\n", encoding="utf-8"
        )
        (case_dir / CASE_FILE).write_text(case_yaml, encoding="utf-8")
        (case_dir / DIFF_FILE).write_text(diff, encoding="utf-8")

        skill = load_skill(skill_dir)

    case = next((c for c in skill.eval_cases if c.id == edits.case_id), None)
    if case is None:
        raise SkillLoadError(f"case {edits.case_id!r} did not load back")
    if not case.expect:
        raise SkillLoadError("case has no expectations")
    _check_region(case, diff)
    return case


def _check_region(case: EvalCase, diff: str) -> None:
    """The expectation must point somewhere the diff actually changes.

    A path typo mints a case that can never pass, and catching it here is the difference between
    "the reviewer regressed" and "the case was wrong".

    The line range is a weaker claim than it used to be. Matching widens an anchor to the footprint
    of the change (`core.matching.effective_region`), so a range that misses every hunk no longer
    makes a case unmatchable — it makes it *unanchored*, which is a different and quieter problem:
    the operator selected lines the diff does not contain, and nothing downstream would ever say so.
    Refusing it keeps the anchor meaning what it says.
    """
    changed = {f.path for f in case.change.files}
    for expectation in case.expect:
        path = expectation.where.path
        if path not in changed:
            listed = ", ".join(sorted(changed)) or "(none)"
            raise SkillLoadError(
                f"expectation points at {path!r}, which this diff does not change; "
                f"the diff touches: {listed}"
            )
        file = case.change.file(path)
        rng = expectation.where.line_range
        if file is None or rng is None:
            continue
        if not file.covers(rng):
            spans = ", ".join(f"{lo}–{hi}" for lo, hi in file.new_line_spans())
            raise SkillLoadError(
                f"expectation covers lines {rng[0]}–{rng[1]} of {path}, which this diff does not "
                f"touch; it changes lines {spans}. Point the range at a line the change contains, "
                f"or clear it to mean the whole file"
            )
