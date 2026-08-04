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

import difflib
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal

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
from whetstone.sidecars.claims import with_claim
from whetstone.sidecars.collect import AGENTS_DIR, CONTEXT_FILE

CASE_FILE = "case.yaml"
DIFF_FILE = "change.diff"
META_FILE = "meta.yaml"

# The shape `service.rule_ids` can find in a SKILL.md body ("- **R1 — …**"). Enforced here so a
# provenance key can never name a rule the body regex could not produce: such a key would be
# reported as a declared rule forever and, having no findings to cite it, as a permanently untested
# one.
_RULE_ID = re.compile(r"^[A-Z][A-Z0-9]*[0-9]$")


Destination = Literal["rule", "context", "exception"]

# Where a destination's claim lands. A `context` claim is a fact about the subsystem that every
# role needs, so it goes in the role-agnostic file — the reason `context.md` was factored out at
# all. An `exception` narrows a rule belonging to *this* skill, so it goes in the role's own file:
# excepting R1 for the arch reviewer must not silence the QA reviewer, which reads a different one.
DESTINATION_FILE: dict[str, str] = {"context": CONTEXT_FILE, "exception": ""}


class SidecarDelivery(BaseModel):
    """A sidecar claim on its way to the source repo — as a patch, never as a write.

    Kept out of `PreparedCase.files` on purpose. `commit_promotion` writes every entry in `files`
    under `skills_repo`, and this file does not live there: it belongs to the reviewed code, in
    front of that folder's CODEOWNERS. Whetstone holds no write credentials on a source repo
    (ADR-028's *git stays the operator's*, surviving contact with a second repo), so what it
    produces is something a person applies.
    """

    # Repo-relative inside the **source** repo, not the skills repo.
    path: str
    role: str
    # The whole file after the claim is added — what review sees, and what `patch` applies to.
    content: str
    # A unified diff, `git apply`-able from the source repo root.
    patch: str
    branch: str
    title: str
    body: str
    # True when the folder had no sidecar for this role yet, which is what the PR body should say:
    # a first claim in a folder is a different review from a line added to an established file.
    creates_file: bool = False


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
    # Where the *judgment* goes. Triage has always had one destination while the decision being
    # made had three, and that is what made the sidecar tier unfillable: a "the reviewer flagged X
    # but X is correct here" signal had nowhere to go except softening the central rule, which
    # degrades it everywhere to fix one folder.
    #
    # `reject` is not here — it is a different endpoint, which records a decision and writes no
    # case at all. Every destination that *does* write, writes the eval case: it is the evidence
    # the reviewer missed something there, and what the ablation uses to show the claim is
    # load-bearing.
    destination: Destination = "rule"
    # The sidecar claim, for `context` and `exception`. Prose about this folder, in the words
    # someone reading the code would need.
    claim: str = ""
    # Provenance for the claim — a review comment, a ticket, an ADR. Required, because blind
    # verification needs something to check against beyond the claim's own plausibility, and the
    # dead-claim sweep needs to ask whether the originating constraint still holds.
    claim_source: str = ""
    # The rule an `exception` narrows. Named, so exceptions stay countable: three folders excepting
    # the same rule is the signal that the rule, not the folders, is what needs changing.
    excepts_rule_id: str = ""

    @property
    def must(self) -> str:
        """Derived, never asked for: a `should_catch` case whose expectation says `not_appear` is
        incoherent, so the UI has no way to express it."""
        return "appear" if self.kind == "should_catch" else "not_appear"

    @property
    def writes_sidecar(self) -> bool:
        return self.destination in DESTINATION_FILE


class PreparedCase(BaseModel):
    """A validated case, ready to write. `files` are repo-relative paths → contents."""

    skill_id: str
    case_id: str
    files: dict[str, str]
    case: EvalCase
    # The sidecar this promotion also produces, when the destination is `context` or `exception`.
    # Deliberately not in `files` — see `SidecarDelivery`.
    sidecar: SidecarDelivery | None = None


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


class SidecarTarget(BaseModel):
    """What `prepare` needs to render a claim: the skill's role, and what is already on disk.

    Resolved by the caller rather than here, because finding it means reading the skill's
    `evaluate` step and the source tree — neither of which this module knows about, and both of
    which have to be resolved identically for the console and the CLI.
    """

    role: str
    # The target file's current contents in the source repo, or None when the folder has none yet.
    existing: str | None = None
    # Rules the skill actually declares, so an `Excepts R9` on a skill with no R9 is caught here
    # rather than becoming an exception that narrows nothing and is never noticed.
    rule_ids: list[str] = []


def prepare(
    entry: CandidateEntry,
    edits: CaseEdits,
    *,
    skills_root: Path | str,
    meta_yaml: str | None = None,
    sidecar: SidecarTarget | None = None,
) -> PreparedCase:
    """Render and validate an edited candidate. Raises `SkillLoadError` if it wouldn't load.

    `skills_root` is only used to build repo-relative paths; nothing under it is written here.
    `meta_yaml` is the skill's current metadata, needed only when `edits.rule_id` is set — the
    updated copy is returned in `files` so the rule's evidence lands in the same commit as the case
    that demonstrates it, rather than in a follow-up nobody makes.

    `sidecar` is required when the destination is `context` or `exception`, and unused otherwise.
    What comes back for those is a *patch*, in `PreparedCase.sidecar` and never in `files` — the
    file belongs to the reviewed repository, and Whetstone does not write there.
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
    _check_destination(edits, sidecar)

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
        sidecar=(
            _delivery(entry, edits, sidecar)
            if edits.writes_sidecar and sidecar is not None
            else None
        ),
    )


def _check_destination(edits: CaseEdits, target: SidecarTarget | None) -> None:
    """Refuse a destination that cannot produce what it promises.

    Configured-but-ignored is the failure shape being avoided in both directions: a claim typed
    against a `rule` destination is silently discarded, and an `exception` on a skill with no role
    would write a file nothing ever reads.
    """
    if not edits.writes_sidecar:
        extra = [
            name
            for name, value in (("claim", edits.claim), ("excepts_rule_id", edits.excepts_rule_id))
            if value.strip()
        ]
        if extra:
            raise SkillLoadError(
                f"destination is 'rule', so {' and '.join(extra)} would be discarded — choose "
                f"'context' or 'exception' to file a claim beside the code, or clear the field"
            )
        return

    if target is None or not target.role:
        raise SkillLoadError(
            f"destination {edits.destination!r} files a claim in `.agents/`, but "
            f"{edits.skill_id!r} declares no `sidecar: role:` in SKILL.md — add one, or send this "
            f"to 'rule'"
        )
    if not edits.claim.strip():
        raise SkillLoadError(
            "a context or exception destination needs a claim: it is what the reviewer will read "
            "in that folder, and an empty one files nothing"
        )
    if not edits.claim_source.strip():
        raise SkillLoadError(
            "every claim carries its source and is rejected without one — a review comment, a "
            "ticket, an ADR. Verification needs something to check against beyond the claim's own "
            "plausibility, and the dead-claim sweep needs to know what the claim was for"
        )
    if edits.destination == "exception":
        rule = edits.excepts_rule_id.strip()
        if not rule:
            raise SkillLoadError(
                "an exception must name the rule it excepts: unnamed, it is a rule quietly "
                "repealed in one folder, and nothing can count how often that has happened"
            )
        if not _RULE_ID.match(rule):
            raise SkillLoadError(
                f"excepted rule id {rule!r} should look like R1 or SEC2, matching how rules are "
                f"tagged in SKILL.md"
            )
        if target.rule_ids and rule not in target.rule_ids:
            declared = ", ".join(target.rule_ids)
            raise SkillLoadError(
                f"{edits.skill_id!r} declares no rule {rule!r}, so this exception would narrow "
                f"nothing. It declares: {declared}"
            )


def _delivery(
    entry: CandidateEntry, edits: CaseEdits, target: SidecarTarget
) -> SidecarDelivery:
    """The claim, its file, and the patch that adds it. `_check_destination` has already run."""
    changed = PurePosixPath(edits.path)
    folder = changed.parent
    # `exception` is folder-level and `context` is filed under the file it came from. An exception
    # that holds for one file and not its neighbours is a rule that needs changing, not a folder
    # that is a different kind of place — which is the only thing an exception is for.
    section = "" if edits.destination == "exception" else changed.name
    name = DESTINATION_FILE[edits.destination] or f"{target.role}.md"
    path = str(folder / AGENTS_DIR / name) if str(folder) != "." else f"{AGENTS_DIR}/{name}"

    content = with_claim(
        target.existing,
        edits.claim,
        edits.claim_source,
        role="" if name == CONTEXT_FILE else target.role,
        section=section,
        excepts=edits.excepts_rule_id.strip(),
    )
    creates = not (target.existing or "").strip()
    return SidecarDelivery(
        path=path,
        role=target.role,
        content=content,
        patch=_patch(path, target.existing or "", content),
        branch=f"whetstone/sidecar/{edits.case_id}",
        title=_title(edits, str(folder)),
        creates_file=creates,
        body=_pr_body(entry, edits, path=path, folder=str(folder), creates=creates),
    )


def _title(edits: CaseEdits, folder: str) -> str:
    where = folder if folder != "." else "the repository root"
    if edits.destination == "exception":
        return f"{AGENTS_DIR}: {edits.excepts_rule_id} does not apply in {where}"
    return f"{AGENTS_DIR}: record local context for {where}"


def _pr_body(
    entry: CandidateEntry, edits: CaseEdits, *, path: str, folder: str, creates: bool
) -> str:
    """The pull request this claim goes out as.

    Addressed to the folder's owners, because they are the only people who can say whether the
    claim is true. The ticket is in the body for the same reason the claim carries its source: a
    reviewer asked to accept an assertion about their own code needs to see where it came from.
    """
    provenance = entry.candidate.provenance
    origin = provenance.ref or provenance.source or "triage"
    lead = (
        f"Adds `{path}` — the first `{AGENTS_DIR}/` note in `{folder}`."
        if creates
        else f"Adds one claim to `{path}`."
    )
    kind = (
        f"It excepts **{edits.excepts_rule_id}** for this folder only. The rule stays strict "
        f"everywhere else."
        if edits.destination == "exception"
        else "It records a fact about this folder that is not recoverable from the code."
    )
    return (
        f"{lead}\n\n{kind}\n\n"
        f"> {edits.claim.strip()}\n\n"
        f"**Source:** {edits.claim_source.strip()}\n"
        f"**Origin:** {origin}\n\n"
        f"This note is read by automated review of changes under `{folder}`; it adds no rules of "
        f"its own. It came with an eval case (`{edits.case_id}`) that fails without it, which is "
        f"what keeps it from being decoration.\n\n"
        f"Please reject it if it is wrong — it will be read as fact by every future review of this "
        f"folder."
    )


def _patch(path: str, before: str, after: str) -> str:
    """A `git apply`-able unified diff. New files diff against /dev/null, as git writes them."""
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    from_file = f"a/{path}" if before else "/dev/null"
    body = "".join(
        difflib.unified_diff(old, new, fromfile=from_file, tofile=f"b/{path}", n=3)
    )
    header = f"diff --git a/{path} b/{path}\n"
    if not before:
        header += "new file mode 100644\n"
    # A final line with no newline would make `git apply` reject the whole patch.
    if body and not body.endswith("\n"):
        body += "\n\\ No newline at end of file\n"
    return header + body


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
