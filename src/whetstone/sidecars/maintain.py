"""The maintainer sweep: checking stored claims against the code, blind.

`docs/design/sidecars.md` §8. Consumer confirmations (`confirm.py`) cover whatever gets *touched*,
which is the right allocation and leaves cold code unchecked forever. This is the loop that reaches
it: post-merge on changed folders, and a budgeted crawl over the least-recently-confirmed.

**Verification is blind, and that is the single detail most easily lost.** The verifier is given the
folder's code and asked *what would a reader of this folder need to know?* — and is never shown the
stored claims while it answers. Only then is its independent account compared against them.

Show a model a claim and ask "still true?" and it anchors: the claim is a plausible sentence written
by someone who knew the codebase, the code is long, and agreeing is the locally sensible move. The
loop still runs, still costs money, still writes `confirmed` into a ledger, and verifies nothing.
Two calls is the entire price of it meaning something, and the split is the reason this module
exists rather than one prompt.

**Nothing here writes a sidecar.** The maintainer stamps and files; a human promotes the edit
(§8, *confirmation is automatic, correction is gated*). Contradictions land in the same ledger the
consuming runs write to, which is `docs/design/sidecars.md` open question 3 answered the cheap way:
a maintenance contradiction has no diff and no expectation, so it does not fit the eval-candidate
shape, and forcing it into one would have meant inventing a case nobody can run. `whetstone
sidecars claims --disputed` is the queue.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from whetstone.domain.run import ClaimStatus, ClaimVerdict
from whetstone.sidecars.claims import Claim, parse
from whetstone.sidecars.collect import AGENTS_DIR, CONTEXT_FILE

# How much of a folder's code one blind account is allowed to read. A folder that does not fit is
# summarised from what does, and says so — the alternative, silently truncating, produces an
# account whose silence about a claim is indistinguishable from the code disagreeing with it.
DEFAULT_CODE_BYTES = 40_000

# Suffixes worth reading. Not a whitelist of languages so much as an exclusion of the things that
# blow the budget without informing an account: lockfiles, minified bundles, fixtures.
SKIP_SUFFIXES = frozenset(
    {".lock", ".min.js", ".map", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".bin"}
)


class FolderAccount(BaseModel):
    """What a reader of this folder would need to know — written without seeing the claims."""

    facts: list[str] = []
    # Named so the comparison can tell "the account did not look at that file" apart from "the
    # account looked and found nothing to say".
    read: list[str] = []
    truncated: bool = False


class ClaimCheck(BaseModel):
    """One stored claim, judged against the independent account."""

    claim: str
    verdict: Literal["supported", "contradicted", "absent"]
    evidence: str = ""

    def as_ledger_verdict(self, path: str) -> ClaimVerdict:
        """The ledger's vocabulary, which is deliberately narrower than this one.

        `absent` becomes `unverifiable` rather than a third ledger status: the account simply had
        nothing to say, which is what the consuming path already calls a verdict with no evidence.
        Two words for one state would make the disputed list harder to read for no gain.
        """
        status: ClaimStatus = "unverifiable"
        if self.verdict == "contradicted":
            status = "contradicted"
        elif self.verdict == "supported" and self.evidence.strip():
            status = "confirmed"
        return ClaimVerdict(
            path=path, claim=self.claim, status=status, evidence=self.evidence.strip()
        )


class FolderReport(BaseModel):
    """Everything the sweep learned about one folder."""

    folder: str
    sidecar: str
    checks: list[ClaimCheck] = []
    # Facts the account produced that no stored claim covers. Not a defect and not filed anywhere
    # automatically — it is the raw material for the next claim, and only a human should decide
    # whether a folder needs one.
    uncovered: list[str] = []
    skipped: str = ""

    @property
    def contradicted(self) -> list[ClaimCheck]:
        return [c for c in self.checks if c.verdict == "contradicted"]


class SweepReport(BaseModel):
    folders: list[FolderReport] = []
    calls: int = 0

    @property
    def contradicted(self) -> int:
        return sum(len(f.contradicted) for f in self.folders)


def sidecar_folders(source_root: str | Path, role: str) -> list[tuple[str, str]]:
    """Every (folder, sidecar path) under the root carrying claims for this role.

    Both `context.md` and `<role>.md`, which is what retrieval reads — a sweep that checked only
    the role file would leave the role-agnostic claims, which are usually the load-bearing ones,
    unverified forever.
    """
    root = Path(source_root)
    out: list[tuple[str, str]] = []
    for directory in sorted(root.rglob(AGENTS_DIR)):
        if not directory.is_dir():
            continue
        folder = directory.parent.relative_to(root).as_posix() or "."
        for name in (CONTEXT_FILE, f"{role}.md"):
            candidate = directory / name
            if candidate.is_file():
                out.append((folder, candidate.relative_to(root).as_posix()))
    return out


def stale_first(
    folders: Sequence[tuple[str, str]], last_seen: dict[str, datetime]
) -> list[tuple[str, str]]:
    """Least-recently-verified first — the order a budgeted nightly crawl should spend in.

    A sidecar nothing has ever said anything about sorts before one checked yesterday, which is the
    whole point: the folders most likely to have rotted are the ones nobody is touching, and those
    are exactly the ones consumer confirmations never reach.
    """
    epoch = datetime.fromtimestamp(0, tz=UTC)
    return sorted(folders, key=lambda pair: (last_seen.get(pair[1], epoch), pair[1]))


def read_folder(
    source_root: str | Path, folder: str, *, max_bytes: int = DEFAULT_CODE_BYTES
) -> tuple[str, list[str], bool]:
    """The folder's own code as prompt text, the files read, and whether anything was left out.

    One level, never recursive: a claim in `payments/.agents/` is about `payments/`, and pulling in
    `payments/gateway/` would have the account describing a subtree that has its own sidecar and its
    own claims.
    """
    root = Path(source_root)
    directory = root / folder if folder != "." else root
    blocks: list[str] = []
    read: list[str] = []
    spent = 0
    truncated = False
    try:
        entries = sorted(p for p in directory.iterdir() if p.is_file())
    except OSError:
        return "", [], False
    for file in entries:
        if file.suffix in SKIP_SUFFIXES or file.name.endswith(".min.js"):
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        block = f"--- {file.name} ---\n{text}"
        size = len(block.encode("utf-8"))
        if spent + size > max_bytes:
            truncated = True
            continue
        blocks.append(block)
        read.append(file.name)
        spent += size
    return "\n\n".join(blocks), read, truncated


def blind_account(client: Any, folder: str, code: str, *, effort: str = "medium") -> FolderAccount:
    """Call one: what a reader of this folder needs to know. **Never sees the claims.**

    The prompt asks for what is not recoverable from reading the code once — which is the same
    standard `sidecars.md` §6 applies to claims themselves ("a sidecar regenerable from the file it
    describes should not exist"). An account of what the code plainly says would agree with every
    claim by restating it.
    """
    system = (
        "You are reading one directory of a codebase so that a future reviewer of changes here "
        "knows what to watch for. List the things a competent engineer would need to be TOLD — "
        "invariants, ownership, deliberate exceptions, constraints imposed from outside this "
        "folder — and not the things they would see for themselves by reading it once. Be "
        "specific and cite the file each fact comes from. If the code does not support a fact, "
        "leave it out: an empty list is a correct answer for a folder that holds no surprises."
    )
    body = code or "(no readable files)"
    user = f"Directory: {folder}\n\n{body}"
    result = client.structured(system, user, FolderAccount, effort=effort)
    return result if isinstance(result, FolderAccount) else FolderAccount()


class Comparison(BaseModel):
    checks: list[ClaimCheck] = []
    uncovered: list[str] = []


def compare(
    client: Any, account: FolderAccount, claims: Sequence[Claim], *, effort: str = "medium"
) -> Comparison:
    """Call two: the account against the stored claims.

    Seeing both here is fine and unavoidable — the anchoring risk lives entirely in *producing* the
    account, which call one did without them. This call is a comparison, and a comparison needs
    both sides.

    `absent` is a first-class answer and the commonest correct one. An account written from one
    directory will simply not touch most claims, and forcing every claim into supported-or-
    contradicted is how a verification loop turns into a coin flip with a ledger attached.
    """
    system = (
        "Compare an independent account of a directory against the claims currently recorded for "
        "it. For each recorded claim answer: `supported` if the account states something that "
        "entails it — quote the account as evidence; `contradicted` if the account states "
        "something incompatible with it — quote that; `absent` if the account simply does not "
        "address it. `absent` is expected for most claims and is the right answer whenever you "
        "are unsure: a wrong `contradicted` sends someone to rewrite something correct. Also list "
        "any fact in the account that no recorded claim covers."
    )
    facts = "\n".join(f"- {fact}" for fact in account.facts) or "(the account is empty)"
    recorded = "\n".join(f"- {claim.text}" for claim in claims) or "(no claims on file)"
    user = f"INDEPENDENT ACCOUNT\n{facts}\n\nRECORDED CLAIMS\n{recorded}"
    result = client.structured(system, user, Comparison, effort=effort)
    return result if isinstance(result, Comparison) else Comparison()


def sweep_folder(
    client: Any,
    source_root: str | Path,
    folder: str,
    sidecar_path: str,
    *,
    max_bytes: int = DEFAULT_CODE_BYTES,
    effort: str = "medium",
) -> FolderReport:
    """One folder, two calls. Returns a report; writes nothing anywhere."""
    root = Path(source_root)
    try:
        text = (root / sidecar_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return FolderReport(folder=folder, sidecar=sidecar_path, skipped=f"unreadable: {exc}")
    claims = parse(text, path=sidecar_path).claims
    if not claims:
        return FolderReport(folder=folder, sidecar=sidecar_path, skipped="no claims to check")

    code, read, truncated = read_folder(root, folder, max_bytes=max_bytes)
    account = blind_account(client, folder, code, effort=effort)
    account.read = read
    account.truncated = truncated
    result = compare(client, account, claims, effort=effort)
    # Only claims that were actually on file. A comparison that invents a claim to judge is
    # judging nothing, and the ledger must not learn about claims no sidecar contains.
    known = {claim.text for claim in claims}
    return FolderReport(
        folder=folder,
        sidecar=sidecar_path,
        checks=[c for c in result.checks if c.claim in known],
        uncovered=list(result.uncovered),
    )


def sweep(
    client: Any,
    source_root: str | Path,
    role: str,
    *,
    folders: Sequence[str] | None = None,
    limit: int | None = None,
    last_seen: dict[str, datetime] | None = None,
    max_bytes: int = DEFAULT_CODE_BYTES,
    effort: str = "medium",
) -> SweepReport:
    """Sweep some or all of a tree's sidecars.

    `folders` narrows to the post-merge case — the directories a merge touched. Without it the
    whole tree is eligible and `limit` makes it the budgeted nightly crawl, spent
    least-recently-verified first.
    """
    targets = sidecar_folders(source_root, role)
    if folders is not None:
        wanted = {f.rstrip("/") or "." for f in folders}
        targets = [t for t in targets if t[0] in wanted]
    if last_seen is not None:
        targets = stale_first(targets, last_seen)
    if limit is not None:
        targets = targets[:limit]

    report = SweepReport()
    for folder, sidecar_path in targets:
        result = sweep_folder(
            client, source_root, folder, sidecar_path, max_bytes=max_bytes, effort=effort
        )
        report.folders.append(result)
        if not result.skipped:
            report.calls += 2
    return report
