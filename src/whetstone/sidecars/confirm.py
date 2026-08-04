"""Consumer confirmations: what a review noticed about the claims it was handed.

`docs/design/sidecars.md` §8. Every run already holds both the sidecar and the code, so asking it
for a verdict costs one more field in a reply it was making anyway. That is the whole economics of
this loop — verification effort then tracks how often code is *touched*, so hot code is checked
weekly and cold code is left to the maintainer's crawl.

Two rules do the work, and both are about refusing to accept the cheapest possible answer:

**A confirmation without a code citation is not a confirmation.** Assent is free, evidence is not.
An uncited `confirmed` is recorded as `unverifiable`, which is what it is.

**A verdict that cannot be matched back to a real claim is dropped.** A model asked about a folder's
claims will occasionally invent one, summarise two as one, or answer about the guidance instead.
Keying a ledger on the model's paraphrase would fill it with claims that are not in any file, and
nothing downstream could tell those from the real ones. So every verdict is matched to a parsed
bullet and carries *that* text, or it does not survive.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from whetstone.domain.run import ClaimStatus, ClaimVerdict
from whetstone.sidecars.claims import Claim, parse

LEDGER_FILE = "sidecar_claims.jsonl"

# Below this, "one string contains the other" stops meaning anything: a five-character claim is
# inside half the sentences in a file.
_MIN_CONTAINMENT = 24
# Jaccard over content words, for a paraphrase that neither contains nor is contained.
_MIN_OVERLAP = 0.6


def verdicts_from(reported: Sequence[Any], resolved: dict[str, Any]) -> list[ClaimVerdict]:
    """Model-reported verdicts, matched against what was actually loaded.

    `reported` is any sequence of objects carrying `path`, `claim`, `status` and `evidence` — the
    reviewer's response model, kept structural so this module does not import from the reviewer it
    is called by.
    """
    by_path: dict[str, list[Claim]] = {}
    for entry in resolved.get("files") or []:
        path = str(entry.get("path", ""))
        by_path[path] = parse(str(entry.get("text", "")), path=path).claims

    out: list[ClaimVerdict] = []
    seen: set[tuple[str, str]] = set()
    for item in reported:
        path = str(getattr(item, "path", "") or "")
        candidates = by_path.get(path)
        if candidates is None:
            # Not a file this case was given. Either the model named a folder it inferred, or it
            # answered about a sidecar from an earlier case in the same conversation — neither is
            # a fact about this run.
            continue
        matched = match_claim(str(getattr(item, "claim", "") or ""), candidates)
        if matched is None:
            continue
        evidence = str(getattr(item, "evidence", "") or "").strip()
        raw = str(getattr(item, "status", "") or "")
        # Anything unrecognised falls to `unverifiable`, which is also where an uncited
        # confirmation lands. Written out rather than looked up so the narrowing is a fact the type
        # checker can see, and so the two ways of reaching `unverifiable` stay visible.
        status: ClaimStatus = "unverifiable"
        if raw == "confirmed" and evidence:
            status = "confirmed"
        elif raw == "contradicted":
            status = "contradicted"
        key = (path, matched.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ClaimVerdict(path=path, claim=matched.text, status=status, evidence=evidence)
        )
    return out


def match_claim(reported: str, claims: Sequence[Claim]) -> Claim | None:
    """The claim a reported verdict is about, or None when it is about nothing on file.

    Conservative on purpose. The cost of a miss is one unrecorded confirmation on one run, against
    a claim that any later run touching the same folder will be asked about again. The cost of a
    false match is a `contradicted` stamp on the wrong claim, which sends a human to rewrite
    something that was correct.
    """
    needle = _normalise(reported)
    if not needle:
        return None
    normalised = [(claim, _normalise(claim.text)) for claim in claims]

    for claim, text in normalised:
        if text == needle:
            return claim
    if len(needle) >= _MIN_CONTAINMENT:
        for claim, text in normalised:
            if needle in text or (len(text) >= _MIN_CONTAINMENT and text in needle):
                return claim

    best: tuple[float, Claim | None] = (0.0, None)
    words = _words(needle)
    for claim, text in normalised:
        score = _overlap(words, _words(text))
        if score > best[0]:
            best = (score, claim)
    return best[1] if best[0] >= _MIN_OVERLAP else None


def _normalise(text: str) -> str:
    return " ".join(text.replace("`", "").replace("*", "").lower().split())


_STOP = frozenset(
    "a an the is are was were be been to of in on at for and or not it its this that with from by"
    " as into no do does did has have had must may can".split()
)


def _words(text: str) -> frozenset[str]:
    return frozenset(w.strip(".,;:()[]\"'") for w in text.split()) - _STOP


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class LedgerEntry(BaseModel):
    """One verdict, stamped with where it came from.

    Kept because a claim's *history* is the thing worth having: one `contradicted` from one model
    on one case is an opinion, and the same verdict from four unrelated runs over a month is a
    finding. The console answers "when was this last verified" from here; `git blame` on the
    sidecar answers "when did it last move". Those are different questions and this is why they
    have different homes (§8, *stamp on change, not on check*).
    """

    at: datetime
    run_id: str = ""
    skill_id: str = ""
    case_id: str = ""
    path: str
    claim: str
    status: str
    evidence: str = ""


class Ledger:
    """Append-only JSONL of claim verdicts. Never rewrites, never deletes.

    Append-only because the alternative — a current-status-per-claim table — throws away exactly
    the disagreement that makes a verdict worth acting on. A claim confirmed eleven times and
    contradicted once is a different situation from one contradicted eleven times, and a table
    keyed on the claim shows the same thing for both.
    """

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory) / LEDGER_FILE

    def record(
        self,
        verdicts: Iterable[ClaimVerdict],
        *,
        run_id: str = "",
        skill_id: str = "",
        case_id: str = "",
        at: datetime | None = None,
    ) -> int:
        """Append these verdicts. Returns how many were written.

        **Idempotent per run.** A record that is saved twice — a retry, a re-index, a console job
        and a CLI invocation over the same run — must not turn one confirmation into two. The
        ledger's only job is to say how much agreement a claim has accumulated, and evidence that
        inflates when nothing new happened is worse than no evidence.

        Only when `run_id` identifies the observation, though. The maintainer sweep records with no
        run id, and two sweeps a week apart are genuinely two observations of the same claim: that
        is the accumulation this exists to capture, not a duplicate to suppress.
        """
        rows = [
            LedgerEntry(
                at=at or datetime.now(UTC),
                run_id=run_id,
                skill_id=skill_id,
                case_id=case_id,
                path=v.path,
                claim=v.claim,
                status=v.status,
                evidence=v.evidence,
            )
            for v in verdicts
        ]
        # Within one call, a repeated (path, claim) is one observation stated twice, never two —
        # whoever produced the verdicts looked at each claim once. Across calls the same key means
        # the same thing only when `run_id` ties both to one observation, so only then is history
        # consulted; the maintainer sweep records without one, and two sweeps a week apart are
        # genuinely two observations.
        seen: set[tuple[str, str, str, str]] = set()
        if run_id:
            seen = {(e.run_id, e.case_id, e.path, e.claim) for e in self.entries()}
        deduped: list[LedgerEntry] = []
        for row in rows:
            key = (row.run_id, row.case_id, row.path, row.claim)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        rows = deduped
        if not rows:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(row.model_dump_json() + "\n")
        return len(rows)

    def entries(self) -> list[LedgerEntry]:
        if not self.path.is_file():
            return []
        out: list[LedgerEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(LedgerEntry.model_validate_json(line))
            except ValueError:
                # One unreadable row must not hide the rest of the history.
                continue
        return out

    def summary(self) -> list[ClaimHistory]:
        """One row per claim, newest activity first — what the console and CLI both show."""
        grouped: dict[tuple[str, str], list[LedgerEntry]] = {}
        for entry in self.entries():
            grouped.setdefault((entry.path, entry.claim), []).append(entry)
        histories = [
            ClaimHistory(
                path=path,
                claim=claim,
                confirmed=sum(1 for e in rows if e.status == "confirmed"),
                contradicted=sum(1 for e in rows if e.status == "contradicted"),
                unverifiable=sum(1 for e in rows if e.status == "unverifiable"),
                last_seen=max(e.at for e in rows),
                last_evidence=next(
                    (e.evidence for e in sorted(rows, key=lambda r: r.at, reverse=True)
                     if e.status == "contradicted" and e.evidence),
                    "",
                ),
            )
            for (path, claim), rows in grouped.items()
        ]
        # Disputed first, then most recent: the reason to open this list is to find what somebody
        # with the code in front of them said was wrong.
        histories.sort(key=lambda h: (h.contradicted, h.last_seen), reverse=True)
        return histories


class ClaimHistory(BaseModel):
    """Everything the ledger knows about one claim."""

    path: str
    claim: str
    confirmed: int = 0
    contradicted: int = 0
    unverifiable: int = 0
    last_seen: datetime
    # The most recent evidence *against* the claim, which is the only text a human needs to decide.
    last_evidence: str = ""

    @property
    def disputed(self) -> bool:
        """Worth a human's attention: something with the code in front of it said this is wrong."""
        return self.contradicted > 0
