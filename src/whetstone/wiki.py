"""The repo knowledge a skill reviews with: generated elsewhere, indexed by source path, retrieved
per change.

A reviewer that sees only a diff and a list of rules judges every change as if the repository had no
shape and no history. The wiki is the missing half of that context — a summary of the source tree,
produced by whatever generator a team already runs, committed beside the skill and consulted at
review time.

Three constraints shape the design, and all three follow from the fact that a *gate* reads this:

**Retrieval is by source path, not by meaning.** The index maps globs to pages, a change names
files, and the pages for those files are what gets injected. No embeddings, no similarity search.
That is not a shortcut. A gate compares a base and a candidate over the same cases, so if retrieval
could return different context on the two sides, a score difference would stop being attributable to
the guidance change. Path retrieval is a pure function of the diff, which keeps the comparison fair.

**The caps are enforced here, not by the caller.** A monorepo wiki is far larger than any context
window, so something has to say no. Doing it at the retrieval boundary means every consumer —
reviewer, improve step, console — inherits the same bound without having to remember it, and a
badly written step cannot opt out of it.

**Nothing is dropped silently.** `Retrieved` reports what was left out and what was cut short. A
reviewer that quietly saw a third of the relevant context would produce a score nobody could
interpret, and "no silent caps" is the rule everywhere else in this codebase too.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, Field

INDEX_FILE = "index.yaml"
PAGES_DIR = "pages"
WIKI_DIR = "wiki"

# What a page is cut to when it alone exceeds the byte cap. Truncating the single most relevant page
# beats dropping it: half of the right page is context, and none of it is not.
TRUNCATION_NOTE = "\n\n[…truncated by Whetstone: this page exceeds the configured wiki byte cap]"


class WikiError(ValueError):
    """A wiki that cannot be loaded. Carries the offending file in the message, always."""


class WikiSource(BaseModel):
    """Where this wiki came from — recorded so a stale one is recognisable as stale.

    Whetstone does not generate the wiki; `update/` shells out to the generator a team already runs.
    That makes provenance the only way to answer "does this describe the code we are reviewing?",
    so the revision it was built from is the field that matters most here.
    """

    generator: str = ""
    repo: str = ""
    revision: str = ""
    generated_at: str = ""


class WikiPage(BaseModel):
    id: str
    title: str
    text: str

    def sized(self, budget: int) -> tuple[WikiPage, bool]:
        """This page cut to `budget` bytes. Returns the page and whether it was cut."""
        raw = self.text.encode("utf-8")
        if len(raw) <= budget:
            return self, False
        # Decode with `ignore` so a cut landing mid-codepoint yields short text rather than raising.
        kept = raw[:budget].decode("utf-8", errors="ignore")
        return self.model_copy(update={"text": kept + TRUNCATION_NOTE}), True


class WikiEntry(BaseModel):
    """One index row: the source paths a page describes."""

    page: str
    paths: list[str] = Field(default_factory=list)

    def matches(self, path: str) -> bool:
        candidate = PurePosixPath(path)
        # `full_match` (3.13+) gives real `**` semantics; `fnmatch` would let `src/auth/*` match a
        # file three directories down, which silently over-injects context.
        return any(candidate.full_match(glob) for glob in self.paths)


class SkillWiki(BaseModel):
    """A skill's repo context: an ordered index plus the pages it names."""

    entries: list[WikiEntry] = Field(default_factory=list)
    pages: dict[str, WikiPage] = Field(default_factory=dict)
    source: WikiSource = WikiSource()

    def is_empty(self) -> bool:
        return not self.pages


class WikiLimits(BaseModel):
    """How much repo context any one step may see.

    Defaults are deliberately small. Wiki text is paid for on *every* case of *every* trial on
    *both* sides of a gate, so a generous default would quietly multiply the cost of a run by the
    size of someone's monorepo summary.
    """

    max_pages: int = 4
    max_bytes: int = 24_000


class Retrieved(BaseModel):
    """What retrieval actually yielded, and what it had to leave behind."""

    pages: list[WikiPage] = Field(default_factory=list)
    # Pages whose globs matched but which the caps excluded. Named, not counted, so a run can say
    # which context the reviewer never saw.
    dropped: list[str] = Field(default_factory=list)
    truncated: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.pages

    @property
    def note(self) -> str:
        """A one-line summary of what the caps cost, or "" when they cost nothing."""
        parts = []
        if self.dropped:
            parts.append(f"{len(self.dropped)} page(s) omitted ({', '.join(self.dropped)})")
        if self.truncated:
            parts.append(f"{len(self.truncated)} page(s) truncated ({', '.join(self.truncated)})")
        return "; ".join(parts)

    def to_prompt(self) -> str:
        """The pages rendered for a model. Empty string when there is nothing to say."""
        if not self.pages:
            return ""
        blocks = [f"## {p.title}\n\n{p.text.strip()}" for p in self.pages]
        return "\n\n".join(blocks)


def retrieve(wiki: SkillWiki, paths: list[str], limits: WikiLimits | None = None) -> Retrieved:
    """The wiki pages describing `paths`, ranked and capped.

    Ranked by how many of the changed paths a page covers, ties broken by index order. Both keys are
    properties of the input, never of iteration order or of a clock, so the same change retrieves
    the same context every time — which is what lets a gate attribute a score change to guidance.
    """
    limits = limits or WikiLimits()
    if wiki.is_empty() or not paths:
        return Retrieved()

    ranked: list[tuple[int, int, WikiPage]] = []
    for position, entry in enumerate(wiki.entries):
        page = wiki.pages.get(entry.page)
        if page is None:
            continue  # an index row naming a page that is not on disk; `load_wiki` already warned
        hits = sum(1 for path in paths if entry.matches(path))
        if hits:
            ranked.append((-hits, position, page))
    ranked.sort(key=lambda row: (row[0], row[1]))

    kept: list[WikiPage] = []
    dropped: list[str] = []
    truncated: list[str] = []
    remaining = limits.max_bytes
    for _, _, page in ranked:
        if len(kept) >= limits.max_pages or remaining <= 0:
            dropped.append(page.id)
            continue
        sized, was_cut = page.sized(remaining)
        kept.append(sized)
        if was_cut:
            truncated.append(page.id)
        remaining -= len(sized.text.encode("utf-8"))
    return Retrieved(pages=kept, dropped=dropped, truncated=truncated)


def paths_of(change: object) -> list[str]:
    """The new-side paths a change touches, in file order.

    Takes the change structurally rather than importing `CodeChange`, so this module stays free of
    the domain layer and can be used from a step that only has parsed JSON.
    """
    files = getattr(change, "files", None) or []
    out: list[str] = []
    for f in files:
        path = getattr(f, "path", None) or (f.get("path") if isinstance(f, dict) else None)
        if path:
            out.append(str(path))
    return out


def load_wiki(directory: str | Path) -> SkillWiki:
    """Load `<skill>/wiki/`. A missing folder is an empty wiki — most skills have none.

    An index row naming a page that is not on disk *is* an error. The alternative is a reviewer that
    silently loses context because someone deleted a markdown file, and a gate that scores lower for
    reasons nobody can see.
    """
    root = Path(directory)
    index_path = root / INDEX_FILE
    if not index_path.is_file():
        return SkillWiki()

    raw = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise WikiError(f"{index_path}: expected a mapping, got {type(raw).__name__}")

    source = WikiSource(**(raw.get("source") or {}))
    entries: list[WikiEntry] = []
    for row in raw.get("pages") or []:
        if not isinstance(row, dict) or not row.get("page"):
            raise WikiError(f"{index_path}: every entry under 'pages:' needs a 'page:' key")
        globs = row.get("paths") or []
        if not isinstance(globs, list):
            raise WikiError(f"{index_path}: 'paths' for page {row['page']!r} must be a list")
        entries.append(WikiEntry(page=str(row["page"]), paths=[str(g) for g in globs]))

    pages: dict[str, WikiPage] = {}
    for entry in entries:
        if entry.page in pages:
            continue  # two index rows may point at one page; different globs, same content
        path = _page_path(root, entry.page, index_path)
        if not path.is_file():
            raise WikiError(
                f"{index_path}: page {entry.page!r} is indexed but "
                f"{path} does not exist — regenerate the wiki or remove the entry"
            )
        text = path.read_text(encoding="utf-8")
        pages[entry.page] = WikiPage(id=entry.page, title=_title(text, entry.page), text=text)

    return SkillWiki(entries=entries, pages=pages, source=source)


def wiki_digest(wiki: SkillWiki) -> str:
    """Content identity for a wiki: the index and every page body.

    Folded into `skill_hash` so that regenerating the wiki retracts a passing gate. Without that,
    an `update/` run could change what the reviewer sees while a stale gate still says the skill is
    safe to publish — which is exactly the hole C6 exists to close.
    """
    h = hashlib.sha256()
    for entry in wiki.entries:
        h.update(b"\0entry\0")
        h.update(entry.page.encode("utf-8"))
        for glob in entry.paths:
            h.update(b"\0")
            h.update(glob.encode("utf-8"))
    for page_id in sorted(wiki.pages):
        h.update(b"\0page\0")
        h.update(page_id.encode("utf-8"))
        h.update(b"\0")
        h.update(wiki.pages[page_id].text.encode("utf-8"))
    return h.hexdigest()


def _page_path(root: Path, page: str, index_path: Path) -> Path:
    """Where a page id lives under `pages/`, refusing an id that would resolve outside it.

    A slash is legitimate: `architecture/overview` is `pages/architecture/overview.md`, which is how
    a generator that groups its output by subject is read. `..` and a leading slash are not — they
    name a file outside the wiki, and this path is opened and its contents go into a reviewer's
    prompt and into `skill_hash`. A backslash or a colon is refused for a duller reason: each is a
    separator or reserved on one platform and an ordinary filename character on the other, so an id
    containing one names two different files depending on where the run happened.

    Checked twice, because the two checks catch different things. The rules above are a property of
    the id and produce a message naming the rule it broke; resolving the result then catches the
    case no rule about the id can see, which is a symlink under `pages/` pointing out of the wiki.
    """
    parts = page.split("/")
    if page.startswith("/") or ":" in page or "\\" in page or ".." in parts:
        raise WikiError(
            f"{index_path}: page id {page!r} must be a path inside {PAGES_DIR}/ — a name, or names "
            f"joined by '/', with no '..', no leading '/', and no backslash or colon"
        )
    path = root.joinpath(PAGES_DIR, *parts[:-1], f"{parts[-1]}.md")
    if not path.resolve().is_relative_to((root / PAGES_DIR).resolve()):
        raise WikiError(
            f"{index_path}: page {page!r} resolves to {path.resolve()}, which is outside "
            f"{PAGES_DIR}/ — a page reached through a symlink is not this skill's context"
        )
    return path


def _title(text: str, fallback: str) -> str:
    """The page's first markdown heading, or its id.

    Read from the body rather than from frontmatter so a page generated by any tool works as-is;
    there is nothing for an author to add and therefore nothing to get wrong.
    """
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback
