"""Is this skill actually getting sharper?

Whetstone's whole purpose is sharpening skills, and until now nothing in it could answer that
question. You could read one run and one gate. Neither is an answer: a run is a snapshot, and a gate
is a verdict about a single edit. "Has this skill improved over the last ten iterations" had no
instrument at all, which is a strange gap in a tool whose name is a sharpening stone.

The obvious instrument — plot recall over time and see if the line goes up — is a trap, and building
it without saying so would have been worse than building nothing. Three things move that line for
reasons that have nothing to do with the skill:

**The corpus changes underneath it.** The loop's healthy state is promoting new cases, which are by
selection the ones the skill got *wrong*. So a skill doing exactly what it is supposed to do shows
*falling* recall, and a skill whose corpus has been frozen for a month shows a flat, flattering
line. Recall across a changed corpus is not a trend; it is two different exams.

**The judge changes.** Every recall figure is the judge's opinion, so a doctrine edit or a swap to a
distilled tier-1 re-scores history. The console already draws this seam in the run list; a trend
that ignored it would draw straight through.

**The reviewer changes.** A different model, or an agent given a bigger step budget, moves the score
without a word of guidance changing.

So this reports two things and is explicit that they are not the same strength of evidence.

The **trend** is the weak one: the score over time, cut into comparable segments, with every seam
named. Read it for shape, never for a delta across a seam.

The **ledger** is the strong one, and it is the reason this module exists. A gate holds the case
set, the judge and the reviewer fixed across both sides — that is the entire point of a gate — so a
case it recorded as going from failing to passing genuinely improved, and no amount of corpus churn
explains it away. Counting those, and then checking whether each one *still* passes on the newest
run, is the closest thing to an honest answer: **N cases were proven fixed, M of them have stuck.**

The ledger is also why a skill can be sharpening while its recall falls, and the report says so when
that is what happened.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from whetstone.core.gate import GateConfig
from whetstone.gates import GateStore
from whetstone.runs import RunStore
from whetstone.taskruns import TaskGateStore, TaskRunStore

# How many runs back the trend reaches by default. Ten is the user's own unit — "is this skill
# getting better across ten iterations" — and long enough that a seam is usually visible inside it.
DEFAULT_WINDOW = 10


class TrendPoint(BaseModel):
    """One run, and every reason it may not be comparable with the one before it.

    The flags are the point of this model. A bare (date, recall) pair invites a subtraction that is
    very often meaningless, so each point carries what changed since its predecessor and the
    console renders a seam rather than a line.
    """

    run_id: str
    created_at: datetime
    skill_version: int
    recall: float
    fp_rate: float
    f2: float
    k: int = 1
    model: str = ""
    # How many cases this run scored, and how many it could not score at all. A recall computed over
    # 12 of 20 cases is a different measurement from one over all 20.
    cases: int = 0
    errors: int = 0
    # The overfitting readout, when the run partitioned. Holdout recall is the honest half: the
    # improve loop never sees these cases, so a train line climbing away from a flat holdout line is
    # memorization rather than sharpening — which looks identical to progress on the aggregate.
    holdout_recall: float | None = None
    divergence: float | None = None

    # --- seams: each is a reason not to subtract this point from the previous one ---
    judge_changed: bool = False
    reviewer_changed: bool = False
    corpus_changed: bool = False
    # Cases this run scored that its predecessor did not, and vice versa — what `corpus_changed`
    # means concretely, so the console can say "recall fell because you added 3 cases".
    cases_added: list[str] = Field(default_factory=list)
    cases_removed: list[str] = Field(default_factory=list)
    # The guidance itself moved. Not a seam — it is the thing under test — but the console marks it,
    # because two adjacent points with identical guidance are measuring run-to-run noise.
    guidance_changed: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def comparable(self) -> bool:
        """Whether a delta against the previous point means anything at all."""
        return not (self.judge_changed or self.reviewer_changed or self.corpus_changed)


class TaskTrendPoint(BaseModel):
    """One task run. Same idea, different numbers — work produced rather than findings reported."""

    run_id: str
    created_at: datetime
    skill_version: int
    pass_rate: float
    mean_score: float
    cases: int = 0
    errors: int = 0
    model: str = ""
    executor_changed: bool = False
    verifier_changed: bool = False
    corpus_changed: bool = False
    guidance_changed: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def comparable(self) -> bool:
        return not (self.executor_changed or self.verifier_changed or self.corpus_changed)


class ProvenFix(BaseModel):
    """A case a gate demonstrated went from failing to passing — the strong evidence.

    `still_holds` is what turns a fix into sharpening rather than a moment. A case fixed in March
    that has been failing since April was not a lasting improvement, and a ledger that counted it
    forever would be a monument to a regression nobody noticed.
    """

    case_id: str
    gate_id: str
    at: datetime
    # True/False from the newest run that scored this case; None when nothing has scored it since
    # the gate, so the fix is neither confirmed nor refuted — an honest third state, not a pass.
    still_holds: bool | None = None
    last_seen_at: datetime | None = None


class SharpeningReport(BaseModel):
    """What Whetstone can and cannot demonstrate about one skill's progress."""

    skill_id: str
    # Oldest → newest, so it reads and charts left to right.
    points: list[TrendPoint] = Field(default_factory=list)
    task_points: list[TaskTrendPoint] = Field(default_factory=list)

    # --- the ledger (strong evidence) ---
    proven_fixes: list[ProvenFix] = Field(default_factory=list)
    # Cases a gate recorded as regressed. Kept beside the fixes because a ledger that reported only
    # the wins would be a marketing document.
    regressions: list[str] = Field(default_factory=list)
    gates_run: int = 0
    gates_passed: int = 0
    # Passing gates that named no targeted case and fixed none — the ones that proved only that
    # nothing broke. Counted, because a history made entirely of these is the exact shape of a skill
    # that has been maintained rather than sharpened.
    gates_proving_nothing: int = 0

    # --- honesty about the trend ---
    caveats: list[str] = Field(default_factory=list)
    verdict: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fixes_that_stuck(self) -> int:
        return sum(1 for f in self.proven_fixes if f.still_holds)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recall_delta(self) -> float | None:
        """Change in recall across the longest unbroken comparable run of points, or None.

        Deliberately not first-to-last: that subtraction spans every seam in the history and is the
        number this module exists to stop people from quoting.
        """
        segment = _longest_comparable(self.points)
        if len(segment) < 2:
            return None
        return segment[-1].recall - segment[0].recall

    @computed_field  # type: ignore[prop-decorator]
    @property
    def comparable_runs(self) -> int:
        """How many runs the delta above is computed over — the size of its evidence."""
        return len(_longest_comparable(self.points))


def sharpening_report(
    skill_id: str,
    runs: RunStore,
    gates: GateStore,
    *,
    task_runs: TaskRunStore | None = None,
    task_gates: TaskGateStore | None = None,
    window: int = DEFAULT_WINDOW,
    cfg: GateConfig | None = None,
) -> SharpeningReport:
    """Assemble the trend and the ledger for one skill.

    `task_runs`/`task_gates` are optional so the plain review call needs no task stores; when given,
    a task skill gets exactly the same report shape. The two never both populate in practice — a
    skill is scored one way or the other — but the report carries both rather than guessing, so a
    skill mid-conversion shows what it has rather than whichever half was looked for first.
    """
    cfg = cfg or GateConfig()
    report = SharpeningReport(skill_id=skill_id)
    report.points = _review_points(skill_id, runs, window)
    if task_runs is not None:
        report.task_points = _task_points(skill_id, task_runs, window)

    ledger = list(gates.list(skill_id=skill_id))
    task_ledger = list(task_gates.list(skill_id=skill_id)) if task_gates is not None else []
    report.gates_run = len(ledger) + len(task_ledger)
    report.gates_passed = sum(1 for g in ledger + task_ledger if g.passed)
    report.gates_proving_nothing = sum(
        1 for g in ledger + task_ledger if g.passed and not g.fixed and not g.targeted
    )
    report.regressions = sorted(
        {c for g in ledger for c in g.result.regressed_cases}
        | {c for g in task_ledger for c in g.result.regressed_cases}
    )

    fixes: list[ProvenFix] = []
    for gate_record in sorted(ledger + task_ledger, key=lambda g: g.created_at):
        if not gate_record.evidential:
            # A failing or practice gate demonstrates nothing. Reading fixes off one would let a
            # comparison that was never trusted for publishing become evidence of progress.
            continue
        for case_id in gate_record.fixed:
            fixes.append(
                ProvenFix(case_id=case_id, gate_id=gate_record.id, at=gate_record.created_at)
            )
    # Newest gate per case wins: a case fixed, broken and fixed again is one live fix, and its
    # status should be read from the most recent time it was demonstrated.
    by_case: dict[str, ProvenFix] = {}
    for fix in fixes:
        by_case[fix.case_id] = fix
    report.proven_fixes = [
        _check_still_holds(fix, skill_id, runs, task_runs, cfg)
        for fix in sorted(by_case.values(), key=lambda f: f.case_id)
    ]

    report.caveats = _caveats(report)
    report.verdict = _verdict(report)
    return report


def _review_points(skill_id: str, runs: RunStore, window: int) -> list[TrendPoint]:
    """The trend, oldest first, with each point's seams computed against its predecessor.

    Baseline probes are excluded by `RunStore.list`'s default — a run with the guidance deliberately
    stripped would read as a catastrophic regression. Practice runs are dropped here for the same
    reason in reverse: they score a regex, so they belong to no series with real ones.
    """
    summaries = [
        s for s in runs.list(skill_id=skill_id, limit=window * 2) if not s.practice_mode
    ][:window]
    summaries.reverse()

    points: list[TrendPoint] = []
    previous_cases: set[str] | None = None
    for i, s in enumerate(summaries):
        before = summaries[i - 1] if i else None
        # One record read per point, for the case ids and the holdout report. Both live on the full
        # record rather than the index, and both are the difference between a trend and a line.
        scored: set[str] = set()
        holdout = None
        try:
            record = runs.load(s.id)
            scored = {c.case_id for c in record.cases}
            holdout = record.holdout
        except (FileNotFoundError, ValueError):
            # An unreadable record degrades the point rather than the whole report: the index row
            # still carries the score, and a missing corpus comparison is reported as "unknown"
            # by leaving `corpus_changed` false rather than asserting a change that was not seen.
            pass
        points.append(
            TrendPoint(
                run_id=s.id,
                created_at=s.created_at,
                skill_version=s.skill_version,
                recall=s.recall,
                fp_rate=s.fp_rate,
                f2=s.f2,
                k=s.k,
                model=s.model,
                cases=len(scored),
                errors=0,
                holdout_recall=holdout.holdout_recall if holdout else None,
                divergence=holdout.divergence if holdout else None,
                judge_changed=before is not None and s.judge_hash != before.judge_hash,
                reviewer_changed=before is not None and s.model != before.model,
                corpus_changed=previous_cases is not None
                and bool(scored)
                and scored != previous_cases,
                cases_added=sorted(scored - previous_cases) if previous_cases else [],
                cases_removed=sorted(previous_cases - scored)
                if previous_cases and scored
                else [],
                guidance_changed=before is not None and s.guidance_hash != before.guidance_hash,
            )
        )
        if scored:
            previous_cases = scored
    return points


def _task_points(skill_id: str, runs: TaskRunStore, window: int) -> list[TaskTrendPoint]:
    records = [r for r in runs.list(skill_id=skill_id, limit=window * 2) if not r.practice_mode]
    records = records[:window]
    records.reverse()

    points: list[TaskTrendPoint] = []
    previous_cases: set[str] | None = None
    for i, r in enumerate(records):
        before = records[i - 1] if i else None
        scored = {c.case_id for c in r.score.cases}
        points.append(
            TaskTrendPoint(
                run_id=r.id,
                created_at=r.created_at,
                skill_version=r.skill_version,
                pass_rate=r.score.pass_rate,
                mean_score=r.score.mean_score,
                cases=len(scored),
                errors=r.score.errors,
                model=r.model,
                executor_changed=before is not None and r.executor != before.executor,
                # The grader moving is the task equivalent of the judge moving, and just as fatal to
                # a comparison: the same work graded by a different `verify:` is a different score.
                verifier_changed=before is not None and r.verifier != before.verifier,
                corpus_changed=previous_cases is not None and scored != previous_cases,
                guidance_changed=before is not None and r.guidance_hash != before.guidance_hash,
            )
        )
        previous_cases = scored
    return points


def _check_still_holds(
    fix: ProvenFix,
    skill_id: str,
    runs: RunStore,
    task_runs: TaskRunStore | None,
    cfg: GateConfig,
) -> ProvenFix:
    """Does the newest run that scored this case still pass it?

    None when nothing has scored it since the gate. That is not a pass: it means the fix has not
    been re-measured, and reporting it as holding would let a ledger drift further from the truth
    the longer nobody looked.
    """
    outcomes = runs.case_history(skill_id, fix.case_id, limit=1)
    if outcomes:
        latest = outcomes[0]
        if latest.created_at < fix.at:
            return fix
        passing = (
            latest.recall >= cfg.case_recall_floor
            if latest.kind == "should_catch"
            else latest.fp_rate <= cfg.case_fp_ceiling
        )
        return fix.model_copy(update={"still_holds": passing, "last_seen_at": latest.created_at})

    if task_runs is not None:
        record = task_runs.latest(skill_id)
        run = (
            next((c for c in record.score.cases if c.case_id == fix.case_id), None)
            if record
            else None
        )
        if record is not None and run is not None and record.created_at >= fix.at:
            return fix.model_copy(
                update={"still_holds": run.outcome.passed, "last_seen_at": record.created_at}
            )
    return fix


def _longest_comparable(points: list[TrendPoint]) -> list[TrendPoint]:
    """The longest unbroken stretch of points with no seam between them.

    A stretch, not the whole history: the honest window for a delta ends at the last thing that
    changed the measurement. Ties go to the most recent stretch, which is the one someone reading
    "is it improving" is actually asking about.
    """
    best: list[TrendPoint] = []
    current: list[TrendPoint] = []
    for point in points:
        if current and not point.comparable:
            current = [point]
        else:
            current.append(point)
        if len(current) >= len(best):
            best = list(current)
    return best


def _caveats(report: SharpeningReport) -> list[str]:
    """Everything true of this history that a reader would otherwise take the trend to mean."""
    notes: list[str] = []
    points = report.points
    if len(points) >= 2:
        seams = sum(1 for p in points[1:] if not p.comparable)
        if seams:
            steps = len(points) - 1
            notes.append(
                f"{seams} of the {steps} step{'' if steps == 1 else 's'} in this trend "
                f"cross{'es' if seams == 1 else ''} a change in what was being measured — the "
                f"judge, the model, or the case set — so the line is several short series, not "
                f"one. The delta above spans only the longest unbroken stretch "
                f"({report.comparable_runs} run(s))."
            )
        grown = sum(len(p.cases_added) for p in points)
        if grown:
            # Plain prose: this string is rendered as text in the console and printed by the CLI,
            # so markdown emphasis would show up as literal asterisks in both.
            notes.append(
                f"{grown} case(s) entered the corpus across this window. Promoted cases are by "
                f"selection ones the skill got wrong, so recall falling here is what a working "
                f"loop looks like — read the ledger, not the line."
            )
    latest = points[-1] if points else None
    if latest and latest.divergence is not None and latest.divergence > 0.2:
        notes.append(
            f"train recall leads holdout by {latest.divergence:.2f} on the latest run. The improve "
            f"loop never sees holdout cases, so a gap this wide is the guidance learning its own "
            f"exam rather than the pattern behind it."
        )
    if latest and latest.errors:
        notes.append(
            f"{latest.errors} case(s) could not be scored on the latest run, so its recall is "
            f"computed over fewer cases than the run before it."
        )
    if report.gates_proving_nothing:
        notes.append(
            f"{report.gates_proving_nothing} passing gate(s) named no case they were meant to fix "
            f"and fixed none — they demonstrate that nothing broke, which is worth having and is "
            f"not sharpening."
        )
    return notes


def _verdict(report: SharpeningReport) -> str:
    """One sentence answering the question the whole module is for.

    Written to be quotable and to be *true* — which mostly means declining to claim sharpening on
    evidence that does not support it, however much the trend line might invite it.
    """
    points = report.points or report.task_points
    if not points:
        return (
            "never scored, so there is no trend to read. Run an eval — a skill with no history "
            "cannot be shown to be sharpening or rotting."
        )

    # The ledger is checked *before* the trend's own preconditions, because a gate is self-contained
    # evidence: it scored both sides itself, over one case set, with one judge. Two proven fixes and
    # a single run is a real and common state — you gate the first change you make — and answering
    # "one point is not a trend" there suppressed the strongest thing this report knows in favour of
    # a complaint about the weakest.
    proven = len(report.proven_fixes)
    if proven:
        remeasured = sum(1 for f in report.proven_fixes if f.still_holds is not None)
        stuck = report.fixes_that_stuck
        if not remeasured:
            held = "none has been re-scored since, so whether they stuck is not yet known"
        elif stuck == proven:
            held = "all of them still pass on the latest run"
        else:
            held = f"{stuck} of the {proven} still pass on the latest run"
        tail = (
            f" {len(report.regressions)} case(s) have regressed at some point."
            if report.regressions
            else ""
        )
        return (
            f"sharpening, demonstrably: {proven} case(s) went from failing to passing under a gate "
            f"that held the corpus and the judge fixed, and {held}.{tail}"
        )

    if len(points) == 1:
        return (
            "scored once, and never gated. One point is a measurement, not a trend — and only a "
            "gate can show a change was an improvement rather than the corpus getting easier."
        )

    if report.gates_run:
        return (
            f"not demonstrably. {report.gates_run} gate(s) have run and none recorded a case going "
            f"from failing to passing, so what is proven is that this skill has not rotted. To "
            f"prove it is improving, name the cases a change is meant to fix when you gate it."
        )
    return (
        "unproven: this skill has been scored but never gated, and a score alone cannot separate a "
        "better rule from an easier corpus. Gate a guidance change, naming the cases it should fix."
    )
