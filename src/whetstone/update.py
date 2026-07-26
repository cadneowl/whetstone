"""Refreshing the repo context a skill reviews against, by running the generator a team already has.

Whetstone does not summarize repositories. Teams that want repo context already run something that
does — a LangChain openwiki, an internal doc pipeline — and reimplementing it here would produce a
second, worse summarizer that nobody asked for and everybody would have to keep in sync. So the
`update/` step invokes theirs and takes responsibility for the part that is actually Whetstone's
job: checking the output is usable, indexing it so retrieval can be deterministic, and making sure
the refresh retracts any gate that was passed against the old context.

The generator writes into a directory we hand it. What comes back must be indexable, which happens
one of two ways:

  1. It writes `index.yaml` itself. Preferred — the tool that knows which source files a page
     describes is the tool that wrote the page.
  2. It writes only `pages/*.md`, and the step declares the mapping under `index:`. Fine for a
     generator that cannot be taught about Whetstone, at the cost of a mapping someone maintains.

A generator that produces neither is an error naming both options, because the alternative is a
wiki that loads as empty and a reviewer that silently loses all its context.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml
from pydantic import BaseModel

from whetstone.steps import StepError, StepSpec, render_template
from whetstone.wiki import (
    INDEX_FILE,
    PAGES_DIR,
    WIKI_DIR,
    SkillWiki,
    load_wiki,
    wiki_digest,
)


class RefreshResult(BaseModel):
    """What a wiki refresh produced, and whether it actually changed anything."""

    skill_id: str
    pages: int
    files: dict[str, str]
    digest_before: str
    digest_after: str
    stdout_tail: str = ""

    @property
    def changed(self) -> bool:
        return self.digest_before != self.digest_after

    @property
    def note(self) -> str:
        if not self.changed:
            return f"{self.pages} page(s); identical to the committed wiki, nothing to write"
        return f"{self.pages} page(s); wiki content changed, so this skill needs a fresh gate"


def refresh_wiki(
    spec: StepSpec,
    *,
    repo: Path,
    current: SkillWiki | None = None,
    skills_root: str = "skills",
) -> RefreshResult:
    """Run the update step's generator and return the wiki files it produced.

    Returns the files rather than writing them: the caller decides whether they land in the working
    tree or on a branch, exactly as guidance edits and promoted cases already do.
    """
    if not spec.run:
        raise StepError(f"{spec.directory}: update step has no 'run' command")

    out_dir = Path(tempfile.mkdtemp(prefix="whetstone-wiki-"))
    try:
        _invoke(spec, repo=repo, out_dir=out_dir)
        # Before anything about indexing: a generator that produced nothing at all should be told
        # that, not lectured about a mapping for pages it never wrote.
        if not list((out_dir / PAGES_DIR).glob("*.md")):
            raise StepError(
                f"{spec.directory}: the generator wrote no pages to {out_dir}. It ran and exited "
                f"cleanly, so check that it was pointed at the right repository and that its "
                f"output-directory argument is {{{{out_dir}}}}"
            )
        _ensure_index(spec, out_dir)
        produced = load_wiki(out_dir)
        if produced.is_empty():
            raise StepError(
                f"{spec.directory}: the generator wrote pages to {out_dir}, but the index names "
                f"none of them, so retrieval would return nothing"
            )
        files = _collect(out_dir, spec.skill_id, skills_root)
        return RefreshResult(
            skill_id=spec.skill_id,
            pages=len(produced.pages),
            files=files,
            digest_before=wiki_digest(current or SkillWiki()),
            digest_after=wiki_digest(produced),
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _invoke(spec: StepSpec, *, repo: Path, out_dir: Path) -> None:
    values = {"repo": str(repo), "out_dir": str(out_dir), "skill_id": spec.skill_id}
    command = [
        render_template(arg, values, where=str(spec.directory / "step.yaml")) for arg in spec.run
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise StepError(
            f"{spec.directory}: cannot run {command[0]!r} — it is not on PATH. "
            f"This step invokes your wiki generator; Whetstone does not ship one"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise StepError(
            f"{spec.directory}: the generator did not finish within {spec.timeout_s}s "
            f"(raise 'timeout_s' if summarizing this repo genuinely takes longer)"
        ) from exc

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-1200:]
        raise StepError(
            f"{spec.directory}: generator exited {completed.returncode}"
            + (f"\n{tail}" if tail else "")
        )


def _ensure_index(spec: StepSpec, out_dir: Path) -> None:
    """Guarantee an `index.yaml` exists, writing one from the step when the generator wrote none."""
    index_path = out_dir / INDEX_FILE
    if index_path.is_file():
        return
    if not spec.index:
        raise StepError(
            f"{spec.directory}: the generator produced no {INDEX_FILE}, and this step declares "
            f"no 'index:' mapping — so there is no way to know which source files each page "
            f"describes, and retrieval would return nothing. Either have the generator write "
            f"{INDEX_FILE}, or add an 'index:' list of page/paths pairs to step.yaml"
        )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        yaml.safe_dump(
            {"pages": [e.model_dump() for e in spec.index]}, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )


def _collect(out_dir: Path, skill_id: str, skills_root: str) -> dict[str, str]:
    """The generated wiki as repo-relative path → contents, ready to commit."""
    base = f"{skills_root}/{skill_id}/{WIKI_DIR}"
    files = {f"{base}/{INDEX_FILE}": (out_dir / INDEX_FILE).read_text(encoding="utf-8")}
    pages_dir = out_dir / PAGES_DIR
    if pages_dir.is_dir():
        for page in sorted(pages_dir.glob("*.md")):
            files[f"{base}/{PAGES_DIR}/{page.name}"] = page.read_text(encoding="utf-8")
    return files
