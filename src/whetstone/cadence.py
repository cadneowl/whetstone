"""The operating cadence: the clocks behind the routine that removes what the machinery detects.

Every rot detector in this codebase fires on evidence — a failing case, an uncovered MR, a
saturated expectation. Entropy is the exception: improve cycles add rules weekly and nothing else
ever removes one, so the distill pass that compresses guidance back down has no failure to demand
it. The same goes for the quarterly anchor run (sampled scores are estimates; estimates need
ground-truthing) — nothing breaks when it is skipped, it just quietly stops being true that the
scores are checked against anything. A cadence that lives in a document is a cadence that dies in
the document, so these clocks live where the operator already looks: the health payload and the
inbox.

Three of the four clocks are *derived* from records that already exist — the saturation probe from
the run store's baseline records, the drift probe from the drift store, the anchor from the newest
run whose draw covered the whole active corpus. Deriving rather than storing keeps a second
bookkeeping path from disagreeing with the records; marking one of these "done" by hand would be a
statement the stores could contradict. Only the distill pass needs a stored mark, because a distill
is an ordinary improve run with a consolidating instruction — nothing in its record distinguishes
it, so the operator says when one happened and `CadenceStore` remembers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, computed_field

from whetstone.domain.skill import Skill
from whetstone.runs import CorruptRecord, RunStore

CadenceKind = Literal["distill", "saturation", "anchor", "drift"]

# Days between passes, per ANTI_ROT_PLAN.md §5: monthly for the two corpus-and-guidance
# housekeeping passes, quarterly for the two ground-truthing ones.
PERIOD_DAYS: dict[CadenceKind, int] = {
    "distill": 30,
    "saturation": 30,
    "anchor": 90,
    "drift": 90,
}

# What each clock is a clock *for*, in the words the inbox and the health panel use.
_NAMES: dict[CadenceKind, str] = {
    "distill": "guidance distill pass",
    "saturation": "saturation probe",
    "anchor": "full-corpus anchor run",
    "drift": "drift probe",
}

# The clocks an operator may mark by hand. Everything else is derived from a store that already
# records the event, and a hand-written mark could only ever disagree with it.
MARKABLE: tuple[CadenceKind, ...] = ("distill",)

# Run summaries inspected when deriving the anchor. The anchor is recency data — a full-corpus
# run older than ten runs ago is old enough that "overdue" is the right answer anyway.
ANCHOR_SCAN = 10

DEFAULT_CADENCE_DIR = Path(".whetstone/cadence")


class CadenceClock(BaseModel):
    """One routine pass: when it last happened, and whether it is due."""

    kind: CadenceKind
    period_days: int
    last_done: datetime | None = None
    due: bool = False
    # The due sentence, in inbox voice — "guidance distill pass due — last done 47 days ago".
    # Empty when the clock is not due; the panel renders `last_done` itself.
    label: str = ""


class CadenceSection(BaseModel):
    """The health payload's cadence section: every clock, and the due ones as sentences."""

    clocks: list[CadenceClock]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def due(self) -> list[str]:
        return [c.label for c in self.clocks if c.due]


def clocks(
    *,
    distill_at: datetime | None,
    saturation_at: datetime | None,
    anchor_at: datetime | None,
    drift_at: datetime | None,
    first_run_at: datetime | None,
    now: datetime | None = None,
) -> list[CadenceClock]:
    """All four clocks from their last-done facts.

    A clock that has never fired is due only once the skill has been operating longer than the
    clock's period — measured from its first real run. Without that gate every day-one skill
    would open with four overdue chores ahead of writing its first case, and an inbox that cries
    routine at a newborn teaches the operator to ignore it. A skill with no runs at all owes no
    cadence: "never measured" is the score action's job, and it already outranks this.
    """
    moment = now or datetime.now(UTC)
    last: dict[CadenceKind, datetime | None] = {
        "distill": distill_at,
        "saturation": saturation_at,
        "anchor": anchor_at,
        "drift": drift_at,
    }
    out: list[CadenceClock] = []
    for kind, period in PERIOD_DAYS.items():
        done = _aware(last[kind])
        if done is not None:
            age = (moment - done).days
            due = age >= period
            label = f"{_NAMES[kind]} due — last done {age} days ago" if due else ""
        else:
            started = _aware(first_run_at)
            due = started is not None and (moment - started).days >= period
            label = f"{_NAMES[kind]} due — never done" if due else ""
        out.append(
            CadenceClock(kind=kind, period_days=period, last_done=done, due=due, label=label)
        )
    return out


def last_anchor_at(store: RunStore, skill: Skill) -> datetime | None:
    """When the current active corpus was last scored whole — the newest real run whose draw
    covered every active case.

    Judged against the corpus as it is *now*, not as it was when the run happened: a run that was
    exhaustive before last week's promotions no longer grounds the current scores, and "re-anchor
    is due" is exactly the right reading of that. Baseline probes and practice runs never anchor
    anything — one scores blinded guidance, the other a regex.
    """
    active = {c.id for c in skill.eval_cases if c.tier == "active"}
    if not active:
        return None
    for summary in store.list(skill_id=skill.id, limit=ANCHOR_SCAN):
        if summary.practice_mode:
            continue
        try:
            record = store.load(summary.id)
        except (FileNotFoundError, CorruptRecord):
            continue
        if active <= {c.case_id for c in record.cases}:
            return record.created_at
    return None


def _aware(at: datetime | None) -> datetime | None:
    """Naive timestamps read as UTC — every writer in this codebase stamps UTC, and a clock that
    crashes comparing offsets is worse than one a timezone off."""
    if at is not None and at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    return at


class CadenceMarks(BaseModel):
    """The stored marks for one skill — only the hand-marked clocks ever appear here."""

    skill_id: str
    marks: dict[str, datetime] = {}


class CadenceStore:
    """A directory of per-skill mark files — plain JSON, same shape as gates and drift.

    An unreadable file reads as no marks: the clock then says overdue, and the worst that
    prompts is housekeeping done again — the safe direction for a store whose whole content is
    "when did someone last tidy up".
    """

    def __init__(self, root: str | Path = DEFAULT_CADENCE_DIR) -> None:
        self.root = Path(root)

    def marks(self, skill_id: str) -> CadenceMarks:
        path = self.root / f"{skill_id}.json"
        if not path.is_file():
            return CadenceMarks(skill_id=skill_id)
        try:
            return CadenceMarks.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return CadenceMarks(skill_id=skill_id)

    def mark(self, skill_id: str, kind: CadenceKind, at: datetime | None = None) -> datetime:
        """Record that a pass happened. Returns the timestamp written."""
        moment = at or datetime.now(UTC)
        current = self.marks(skill_id)
        current.marks[kind] = moment
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{skill_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(current.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return moment
