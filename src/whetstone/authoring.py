"""Editing a skill's guidance — the step the corpus loop stopped one short of.

`corpus pull` turns review history into eval cases and `promote.py` files them under a skill. That
grows a skill's *test suite*. Nothing grew the guidance itself: the rules an agent actually follows
could only be changed by opening `SKILL.md` in an editor, outside the tool, with nothing connecting
the edit to the cases that motivated it.

This module is the missing half. It renders an edited `SKILL.md`, validates it through the real
loader, and reports the content identity of the result so `gates.py` can answer the only question
that matters before publishing: *has this exact guidance been gated?*

Two choices worth stating:

**The frontmatter is edited surgically, not re-serialized.** `promote.py` round-trips `meta.yaml`
through YAML and accepts losing comments, because that file is machine metadata. `SKILL.md` is
prose a human wrote — reflowing their `triggers` block and deleting their comments on every save
would make the console something people edit *around*. So only the keys being changed are
rewritten, in place, and the result is verified by loading it back and checking those keys actually
hold the intended values. A substitution that hit the wrong line fails loudly instead of silently
mangling the file.

**Version bumps once per proposal, not once per save.** The bump is computed against the version on
the *base* branch, so a session of five edits on one branch lands as v2, not v6.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, computed_field

from whetstone.core.loader import SkillLoadError, load_skill
from whetstone.domain.run import skill_hash
from whetstone.domain.skill import GuidancePage, Skill
from whetstone.naming import describe_unsafe, is_safe_segment

SKILL_FILE = "SKILL.md"
META_FILE = "meta.yaml"


class SkillEdit(BaseModel):
    """A human's rewrite of a skill's guidance.

    Only the fields a person edits as prose. Everything else in the frontmatter — `id`, `triggers`,
    anything an operator hand-added — is carried through untouched, which is why the editor can be a
    plain markdown box rather than a form that has to know every key.
    """

    body: str
    # Rewritten companion pages, keyed by their path within the skill folder. A skill is a folder
    # and `SKILL.md` is its entry point, so for many skills the rules being edited are not in `body`
    # at all. Omitted paths are untouched; only pages the skill already has may be written.
    pages: dict[str, str] = Field(default_factory=dict)
    name: str | None = None
    description: str | None = None


class PreparedSkill(BaseModel):
    """A validated guidance edit, ready to commit. `files` are repo-relative paths → contents."""

    skill_id: str
    files: dict[str, str]
    # The skill as it would load from `files` — guidance from the edit, eval cases from the branch
    # this will land on. What a gate would actually score.
    skill: Skill
    skill_hash: str
    previous_hash: str
    version: int

    # A `computed_field`, not a plain property, so it reaches the console: the editor's whole job
    # is to say whether what is staged still needs a gate.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def guidance_changed(self) -> bool:
        """Whether this edit invalidates an existing gate result.

        `skill_hash` covers the guidance body and the eval cases — the things that determine a
        score. Renaming a skill or fixing a typo in its description leaves it unchanged, and
        correctly so: neither can alter what the reviewer does, so neither should force a re-gate.
        """
        return self.skill_hash != self.previous_hash


def prepare_guidance(
    base: Skill,
    current: str,
    edit: SkillEdit,
    *,
    skills_root: str,
    base_version: int | None = None,
) -> PreparedSkill:
    """Render and validate an edited `SKILL.md`. Raises `SkillLoadError` if it wouldn't load.

    `base` is the skill as it stands where the commit will land — its eval cases become the staged
    skill's, since a guidance edit does not touch them but the resulting hash depends on them.
    `current` is the existing file text, whose frontmatter is preserved. `base_version` is the
    version on the branch being proposed *against*; omit it and the bump is relative to `base`.
    """
    if not is_safe_segment(base.id):
        raise SkillLoadError(describe_unsafe(base.id, "skill id"))
    if not edit.body.strip():
        raise SkillLoadError("guidance body is empty; a skill with no rules cannot review anything")

    against = base_version if base_version is not None else base.version
    version = _next_version(base.version, against)
    text = render_skill_md(
        current,
        skill_id=base.id,
        body=edit.body,
        version=version,
        name=edit.name,
        description=edit.description,
    )
    loaded = _validate(text, base.id, version, edit)

    # Guidance from the edit, eval cases and metadata from the branch. `load_skill` above ran
    # against a folder holding only `SKILL.md`, so its `owner`/`provenance` are empty by
    # construction rather than by absence — take those from `base`, and let frontmatter `owner`
    # win when it is set, which is the precedence `core/loader.py` applies.
    staged = base.model_copy(
        update={
            "name": loaded.name,
            "description": loaded.description,
            "version": loaded.version,
            "body": loaded.body,
            "triggers": loaded.triggers,
            "owner": loaded.owner or base.owner,
        }
    )
    pages = _edited_pages(base, edit)
    if pages:
        staged = staged.model_copy(
            update={
                "pages": [
                    GuidancePage(path=p.path, text=pages.get(p.path, p.text)) for p in base.pages
                ]
            }
        )
    files = {f"{skills_root}/{base.id}/{SKILL_FILE}": text}
    files.update({f"{skills_root}/{base.id}/{path}": text for path, text in pages.items()})
    return PreparedSkill(
        skill_id=base.id,
        files=files,
        skill=staged,
        skill_hash=skill_hash(staged),
        previous_hash=skill_hash(base),
        version=version,
    )


def _edited_pages(base: Skill, edit: SkillEdit) -> dict[str, str]:
    """Companion pages this edit rewrites, validated against the skill it edits.

    Only paths the skill already carries, and only where the text actually differs. Restricting to
    known pages is what makes this safe without a second path validator: every accepted path came
    from the loader walking this skill's own folder, so none of them can escape it, name a reserved
    directory, or create a file nothing references. Writing a *new* page is a real thing to want and
    a larger change — it needs somewhere in `SKILL.md` to reference it, or the reviewer never reads
    it — so it is deliberately not smuggled in here.
    """
    known = {page.path: page.text for page in base.pages}
    unknown = sorted(path for path in edit.pages if path not in known)
    if unknown:
        raise SkillLoadError(
            f"{base.id} has no guidance page(s) {', '.join(unknown)}. An edit may rewrite the "
            f"pages a skill already has ({', '.join(sorted(known)) or 'none'}), not add one."
        )
    edited = {
        path: text for path, text in edit.pages.items() if text.strip() != known[path].strip()
    }
    empty = sorted(path for path, text in edited.items() if not text.strip())
    if empty:
        raise SkillLoadError(
            f"guidance page(s) {', '.join(empty)} would be emptied. A page the reviewer is sent "
            f"must say something — delete the file in a merge request of its own instead."
        )
    return edited


def prepare_meta(base: Skill, meta_yaml: str, *, skills_root: str) -> PreparedSkill:
    """Validate a replacement `meta.yaml`.

    Edited as text rather than through a form. `meta.yaml` holds owner, references and the
    rule → signal provenance block, and the provenance block is written structurally by triage
    (`promote.render_meta_yaml`); a form covering all of it would either duplicate that or restrict
    what an operator can express. Validation is the part that was actually missing.

    Never changes `skill_hash`: metadata does not reach the reviewer, so editing it cannot
    invalidate a gate result.
    """
    if not is_safe_segment(base.id):
        raise SkillLoadError(describe_unsafe(base.id, "skill id"))

    parsed = yaml.safe_load(meta_yaml) if meta_yaml.strip() else {}
    if parsed is not None and not isinstance(parsed, dict):
        raise SkillLoadError(f"{META_FILE} must be a mapping, got {type(parsed).__name__}")

    text = meta_yaml if meta_yaml.endswith("\n") else meta_yaml + "\n"
    with tempfile.TemporaryDirectory(prefix="whetstone-meta-") as tmp:
        directory = Path(tmp) / base.id
        directory.mkdir(parents=True)
        (directory / SKILL_FILE).write_text(_render(base.id, base.version, base.body), "utf-8")
        (directory / META_FILE).write_text(text, encoding="utf-8")
        loaded = load_skill(directory)

    staged = base.model_copy(
        update={"owner": loaded.owner or base.owner, "provenance": loaded.provenance,
                "references": loaded.references}
    )
    return PreparedSkill(
        skill_id=base.id,
        files={f"{skills_root}/{base.id}/{META_FILE}": text},
        skill=staged,
        skill_hash=skill_hash(staged),
        previous_hash=skill_hash(base),
        version=base.version,
    )


def render_skill_md(
    current: str,
    *,
    skill_id: str,
    body: str,
    version: int,
    name: str | None = None,
    description: str | None = None,
) -> str:
    """The `SKILL.md` an edit becomes: the existing frontmatter, amended, above the new body."""
    front = _split_frontmatter(current)
    if front is None:
        # A skill whose id came from its folder name. Write it down now rather than leave the file
        # dependent on where it happens to live.
        front = f"id: {skill_id}"

    front = _set_key(front, "version", version)
    if name is not None:
        front = _set_key(front, "name", name)
    if description is not None:
        front = _set_key(front, "description", description)

    return f"---\n{front}\n---\n\n{body.strip()}\n"


def frontmatter_version(text: str) -> int | None:
    """The version a `SKILL.md` declares, or None if it declares none.

    Reads the file alone. The version on the *base* branch is what a bump is measured against, and
    fetching it should not cost exporting and loading a whole skill folder.
    """
    front = _split_frontmatter(text)
    if front is None:
        return None
    data = yaml.safe_load(front)
    if not isinstance(data, dict) or "version" not in data:
        return None
    try:
        return int(data["version"])
    except (TypeError, ValueError):
        return None


def _next_version(current: int, base_version: int) -> int:
    """One bump per proposal.

    A branch already carrying a bump keeps it, so five saves in one session produce v2 rather than
    v6 and a reviewer sees the version the change actually proposes.
    """
    return current if current > base_version else base_version + 1


def _split_frontmatter(text: str) -> str | None:
    """The raw frontmatter block, or None if the file has none.

    Split exactly the way `core/loader.py` does, so the block this returns is the same one the
    loader will parse. A divergence here would let the console edit a region the loader ignores.
    """
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillLoadError(f"{SKILL_FILE} frontmatter is not closed with '---'")
    return parts[1].strip("\n")


# A top-level key and everything that belongs to it: the `key:` line plus any indented continuation
# lines (a block scalar, a nested mapping). Anchored at column 0, so a `version:` nested inside
# `triggers:` is not mistaken for the document's own.
def _key_block(key: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(key)}:[^\n]*(?:\n[ \t]+[^\n]*)*", re.MULTILINE)


def _set_key(front: str, key: str, value: Any) -> str:
    """Replace one frontmatter key in place, or append it if it is not there yet."""
    # `width` disables YAML's line wrapping. Wrapped plain scalars reload as space-joined text,
    # which would quietly rewrite a long description on save.
    line = yaml.safe_dump(
        {key: value}, sort_keys=False, allow_unicode=True, width=10**6
    ).rstrip("\n")
    pattern = _key_block(key)
    if pattern.search(front):
        # A lambda, not a string: `\1` or a backslash in a description would otherwise be read as a
        # replacement escape.
        return pattern.sub(lambda _: line, front, count=1)
    return f"{front}\n{line}" if front else line


def _validate(text: str, skill_id: str, version: int, edit: SkillEdit) -> Skill:
    """Load the rendered file through the real parser, in a throwaway skill folder.

    Doubles as the check on the surgical frontmatter edit: if a substitution landed on the wrong
    line, the value it was supposed to set will not be the value that loads back.
    """
    with tempfile.TemporaryDirectory(prefix="whetstone-authoring-") as tmp:
        directory = Path(tmp) / skill_id
        directory.mkdir(parents=True)
        (directory / SKILL_FILE).write_text(text, encoding="utf-8")
        skill = load_skill(directory)

    if skill.id != skill_id:
        raise SkillLoadError(
            f"frontmatter declares id {skill.id!r} but this skill is {skill_id!r}; "
            "renaming a skill means moving its folder, which the console does not do"
        )
    _check(skill.version, version, "version")
    _check(skill.body, edit.body.strip(), "body")
    if edit.name is not None:
        _check(skill.name, edit.name, "name")
    if edit.description is not None:
        _check(skill.description, edit.description, "description")
    return skill


def _check(actual: object, expected: object, field: str) -> None:
    if actual != expected:
        raise SkillLoadError(
            f"{SKILL_FILE} did not round-trip: {field} was written as {expected!r} but loads as "
            f"{actual!r}. This is a bug in the frontmatter edit, not in what you typed."
        )


def _render(skill_id: str, version: int, body: str) -> str:
    return f"---\nid: {skill_id}\nversion: {version}\n---\n\n{body.strip()}\n"
