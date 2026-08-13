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
    WikiError,
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
        if not _pages(out_dir):
            raise StepError(
                f"{spec.directory}: the generator wrote no pages to {out_dir}. It ran and exited "
                f"cleanly, so check that it was pointed at the right repository and that its "
                f"output-directory argument is {{{{out_dir}}}}"
            )
        _ensure_index(spec, out_dir)
        try:
            produced = load_wiki(out_dir)
        except WikiError as exc:
            # Everything wrong with a step is a `StepError` — that is what both callers catch, and
            # what turns a bad generator into a message instead of a traceback. An unusable index is
            # the generator's output being wrong, so it is reported the same way as the rest of it.
            raise StepError(
                f"{spec.directory}: the generator's wiki cannot be loaded: {exc}"
            ) from exc
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


def _pages(out_dir: Path) -> list[Path]:
    """Every page the generator wrote, sub-folders included.

    Recursive because the read path already is: `load_wiki` resolves a page id containing a slash,
    so `architecture/overview` reads `pages/architecture/overview.md`. A generator that groups its
    output into folders — openwiki does — is therefore writing something Whetstone can already read,
    and a non-recursive glob here dropped exactly those pages at commit time. The cost landed a
    whole run later, as a `WikiError` from a reviewer whose context had silently gone missing.

    The suffix is tested here rather than left to a glob pattern because pathlib case-folds a
    pattern through `os.path.normcase`: `glob("*.md")` matches `OVERVIEW.MD` on Windows and not on
    Linux, so the same generator output produced two different commits depending on who ran the
    refresh. Tested exactly, and case-sensitively on every platform, because `.md` is precisely the
    suffix `load_wiki` reconstructs from a page id — accepting `OVERVIEW.MD` here would commit a
    file that the loader then cannot find, which trades one host-dependent bug for another.
    """
    pages_dir = out_dir / PAGES_DIR
    if not pages_dir.is_dir():
        return []
    found = [p for p in pages_dir.rglob("*") if p.name.endswith(".md") and p.is_file()]
    # Sorted by the posix-relative name, not by `Path`, so the order does not depend on the
    # separator the host platform compares with.
    return sorted(found, key=lambda p: p.relative_to(pages_dir).as_posix())


def _collect(out_dir: Path, skill_id: str, skills_root: str) -> dict[str, str]:
    """The generated wiki as repo-relative path → contents, ready to commit."""
    base = f"{skills_root}/{skill_id}/{WIKI_DIR}"
    files = {f"{base}/{INDEX_FILE}": (out_dir / INDEX_FILE).read_text(encoding="utf-8")}
    pages_dir = out_dir / PAGES_DIR
    for page in _pages(out_dir):
        # `as_posix`, because these keys are repo-relative git paths on every platform.
        # Interpolating the `Path` instead commits `pages/architecture\overview.md` — one file with
        # a backslash in its name — when the refresh happens to be run on Windows.
        relative = page.relative_to(pages_dir).as_posix()
        files[f"{base}/{PAGES_DIR}/{relative}"] = page.read_text(encoding="utf-8")
    return files
