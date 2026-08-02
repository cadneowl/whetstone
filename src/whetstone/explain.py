"""Why a run or a gate landed the way it did, in sentences.

Every number Whetstone reports is already recoverable from a record — recall is on the score, the
gate's reasons are on the result, the findings and judge verdicts are in the drill-down. What was
missing is the step a person actually performs after a run finishes: reading all of that at once and
working out *what happened*. Doing it by hand takes a drill-down, a transcript, a look at the case
YAML and, for a gate, the same again for the other side. It is several minutes of work per run, it
is the same work every time, and getting it wrong sends someone off to fix guidance that was never
the problem.

The distinction this module exists to keep is between a **reason** and a **caveat**:

- a *reason* is why the verdict is what it is. Remove it and the verdict changes.
- a *caveat* is something that weakens the measurement without changing its arithmetic — one trial
  per case, an agent that ran out of steps, two sides that investigated differently.

Keeping them apart is the whole value. Mixed together they read as a list of complaints and get
skipped; separated, the first list says what to fix and the second says how much to trust it. A
gate that fails on a case whose candidate answer was cut off at the step ceiling has a reason
("this case regressed") and a caveat that entirely changes what to do about it, and nothing in the
record put those two facts next to each other before.

Nothing here computes a verdict. `core.gate` decides pass or fail and this explains that decision;
a second opinion living in the presentation layer is how two parts of a system start disagreeing
about whether something shipped.
"""

from __future__ import annotations

from pydantic import BaseModel

from whetstone.core.gate import GateConfig
from whetstone.domain.run import CaseRun, RunRecord, TrialRecord
from whetstone.domain.score import CaseScore, SkillScore
from whetstone.gates import GateRecord

# Below this, a difference between two sides is not worth a sentence — they are float comparisons on
# ratios of small integers, and "recall moved by 0.0001" is noise dressed as news.
_EPSILON = 1e-9

# What "this case passed" means. Taken from the gate's defaults rather than written out again,
# because a run whose summary called a case passing while a gate over the same numbers called it
# regressed would be two parts of one tool disagreeing in front of the person deciding what to fix.
_BAR = GateConfig()


def _ok(case: CaseScore) -> bool:
    return case.passed(_BAR.case_recall_floor, _BAR.case_fp_ceiling)


class Explanation(BaseModel):
    """What happened, why, and how much to trust it."""

    # "passed" / "failed" — the arithmetic verdict, never re-derived here (see the docstring).
    verdict: str
    # One line that stands alone. This is what a notification shows and what someone reads first.
    headline: str
    # Why the verdict is what it is. Empty on a clean pass, which is itself the answer.
    reasons: list[str] = []
    # What weakens the measurement without changing it. Empty is a real and welcome answer.
    caveats: list[str] = []

    @property
    def passed(self) -> bool:
        return self.verdict == "passed"


def format_summary(summary: Explanation) -> str:
    """An `Explanation` as terminal text — one renderer, so the CLI and the report agree.

    A reason may carry continuation lines (a gate names what happened to each regressed case).
    Only the first gets the bullet; the rest are indented under it, because bulleting every line
    turns one reason with detail into several reasons that look unrelated.
    """
    lines = [summary.headline]
    for reason in summary.reasons:
        head, *rest = reason.split("\n")
        lines.append(f"  - {head}")
        lines += [f"    {part.strip()}" for part in rest]
    for caveat in summary.caveats:
        lines.append(f"  ! {caveat}")
    return "\n".join(lines)


# --- runs ------------------------------------------------------------------------


def explain_run(record: RunRecord) -> Explanation:
    """Why a score run came out where it did.

    A run has no pass/fail of its own — it is a measurement, not a comparison — so "passed" here
    means every case met its own bar. That is the question someone clicking *Score* is asking, and
    answering a different one would make the badge beside the run mean something other than what it
    appears to mean.
    """
    score = record.score
    failing = [c for c in score.cases if not _ok(c)]
    verdict = "failed" if failing else "passed"

    caught = [c for c in score.cases if c.kind == "should_catch"]
    quiet = [c for c in score.cases if c.kind == "should_not_flag"]
    parts = []
    if caught:
        parts.append(f"caught {sum(1 for c in caught if _ok(c))} of {len(caught)}")
    if quiet:
        parts.append(f"stayed quiet on {sum(1 for c in quiet if _ok(c))} of {len(quiet)}")

    # A run where nothing could be scored must not lead with a metric. An empty confusion reads as
    # `recall 1.0, fp_rate 0.0` — right for "there was nothing to catch here", catastrophic for "we
    # never found out" — and the headline is the one line that travels alone, into a notification or
    # a badge, without the caveat that says the figures are over zero cases. A reviewer pointed at a
    # backend it cannot authenticate to fails every case, and this used to announce it as
    # "caught 0 of 2 — recall 1.000": a self-contradiction that reads as a bug in the summary rather
    # than a broken run.
    if not score.cases:
        headline = "scored no cases"
    elif score.scorable == 0:
        headline = (
            f"nothing was measured — all {len(score.cases)} case(s) failed to run, so this run "
            f"has no recall or false-positive figure worth reading"
        )
    else:
        measured = ", ".join(parts)
        headline = f"{measured} — recall {score.recall:.3f}, false positives {score.fp_rate:.3f}"

    runs_by_id = {c.case_id: c for c in record.cases}
    reasons = [_why_case_failed(c, runs_by_id.get(c.case_id)) for c in failing]
    return Explanation(
        verdict=verdict,
        headline=headline,
        reasons=reasons,
        caveats=_run_caveats(record),
    )


def _why_case_failed(case: CaseScore, run: CaseRun | None) -> str:
    """One sentence for one failing case, as specific as the record allows.

    The four failures below look identical in a score and call for four different responses: fix the
    instrument, raise the budget, write a rule, or argue with the judge. Reporting them as one
    number is what sends people to rewrite guidance that was already right.
    """
    label = f"{case.case_id}"
    if case.error:
        return f"{label}: could not be scored at all — {case.error[:160]}"

    trial = run.representative_trial if run is not None else None
    note = f" ({trial.note})" if trial is not None and trial.note else ""

    if case.kind == "should_not_flag":
        flagged = _first_false_positive(trial)
        where = f" — it flagged {flagged}" if flagged else ""
        return f"{label}: the reviewer spoke when it should have stayed quiet{where}{note}"

    if trial is None:
        return f"{label}: missed, with no recorded detail{note}"
    if not trial.findings:
        return f"{label}: the reviewer reported nothing at all{note}"

    judged = sum(len(o.verdicts) for o in trial.outcomes)
    if judged == 0:
        return (
            f"{label}: the reviewer reported {len(trial.findings)} finding(s), but none of them "
            f"were in the part of the change this case is about, so the judge never saw "
            f"them{note}"
        )
    reason = next(
        (v.reason for o in trial.outcomes for v in o.verdicts if not v.matched and v.reason), ""
    )
    tail = f" — {reason[:200]}" if reason else ""
    return (
        f"{label}: the reviewer reported {len(trial.findings)} finding(s) and the judge ruled that "
        f"none of them are the issue this case is about{tail}{note}"
    )


def _first_false_positive(trial: TrialRecord | None) -> str:
    """Where a `should_not_flag` case was flagged, for the one sentence that reports it."""
    if trial is None:
        return ""
    for outcome in trial.outcomes:
        for verdict in outcome.verdicts:
            if verdict.matched and verdict.finding_index < len(trial.findings):
                finding = trial.findings[verdict.finding_index]
                line = f":{finding.line}" if finding.line else ""
                return f"{finding.path}{line}"
    return ""


def _run_caveats(record: RunRecord) -> list[str]:
    """What would make someone read this run's numbers with more suspicion."""
    out: list[str] = []
    out += _budget_caveat(_forced_notes(record.cases), "")
    flaky = [c.case_id for c in record.cases if c.flaky]
    if flaky:
        out.append(
            f"{len(flaky)} case(s) disagreed between trials — {', '.join(sorted(flaky)[:5])}. "
            f"Their scores are averages of runs that did not agree with each other."
        )
    out += _sampling_caveat(record.score, record.k)
    errored = [c.case_id for c in record.score.cases if c.error]
    if errored:
        out.append(
            f"{len(errored)} case(s) could not be scored, so the figures above are over "
            f"{record.score.scorable} case(s), not {len(record.score.cases)}"
        )
    return out


# --- gates -----------------------------------------------------------------------


def explain_gate(record: GateRecord) -> Explanation:
    """Why a gate passed or failed, and what would make the verdict less believable.

    The gate's own `reasons` are the authority on *why* and are carried through unchanged. What this
    adds is the part a reader had to reconstruct by hand: which cases moved in which direction, and
    whether the case that decided the verdict was measured properly on both sides.
    """
    result = record.result
    delta = _delta(record.base_score, record.candidate_score)
    if result.passed:
        headline = f"PASSED — {delta}"
    else:
        count = len(result.reasons)
        headline = f"FAILED — {delta}, {count} reason(s) below"

    reasons = [_expand(reason, record) for reason in result.reasons]
    return Explanation(
        verdict="passed" if result.passed else "failed",
        headline=headline,
        reasons=reasons,
        caveats=_gate_caveats(record),
    )


def _delta(base: SkillScore, candidate: SkillScore) -> str:
    """The comparison in one clause, saying only what moved."""
    moved = []
    if abs(candidate.recall - base.recall) > _EPSILON:
        moved.append(f"recall {base.recall:.3f} → {candidate.recall:.3f}")
    if abs(candidate.fp_rate - base.fp_rate) > _EPSILON:
        moved.append(f"false positives {base.fp_rate:.3f} → {candidate.fp_rate:.3f}")
    if not moved:
        return f"nothing moved (recall {candidate.recall:.3f} on both sides)"
    return ", ".join(moved)


def _expand(reason: str, record: GateRecord) -> str:
    """A gate reason with the detail the record can add to it.

    Only the regression reason is expanded, because it is the only one that names cases whose story
    lives on the other side of the record. "Targeted case X still fails" is already complete.
    """
    # "case(s) regressed", not "regressed": the recall reason contains the word too, and matching on
    # it attached the same per-case detail to both, printing the whole explanation twice.
    if not record.result.regressed_cases or "case(s) regressed" not in reason:
        return reason
    lines = [reason]
    for case_id in record.result.regressed_cases:
        lines.append(f"  · {case_id}: {_regression_detail(case_id, record)}")
    return "\n".join(lines)


def _regression_detail(case_id: str, record: GateRecord) -> str:
    """What actually happened to one regressed case, on both sides.

    The candidate's note is the point of this. A case the candidate never got to finish looking at
    is not a case its guidance got worse at, and the difference decides whether the next hour is
    spent editing rules or raising `max_steps`.
    """
    cand_note = record.candidate_notes.get(case_id, "")
    base_note = record.base_notes.get(case_id, "")
    detail = "the baseline passed it and the candidate did not"
    if cand_note:
        return (
            f"{detail}, but {cand_note} — this may be the step budget rather than the guidance"
        )
    if base_note:
        return f"{detail} (on the baseline side, {base_note})"
    return detail


def _gate_caveats(record: GateRecord) -> list[str]:
    """Everything true of this comparison that makes its verdict weaker than it looks."""
    out: list[str] = []
    if record.practice_mode:
        out.append(
            "this gate ran in practice mode, which only ever runs against a stand-in backend — "
            "the verdict is about that stand-in, not about the reviewer your guidance will get"
        )
    if record.baseline_reused:
        # Said as a caveat rather than buried in the record, because it changes what the delta is:
        # the two sides were measured at different moments, and the trajectory comparison below is
        # against a trace from the earlier one.
        taken = record.baseline_taken_at.isoformat(timespec="minutes")
        out.append(
            f"the baseline was not measured by this gate — it was reused from "
            f"{record.base_from_gate} "
            f"(taken {taken}), which is sound because the commit, case set, judge, reviewer and "
            f"model are all identical, and is what kept this gate to one side's worth of spend"
        )
    out += _budget_caveat(record.candidate_notes, "the candidate side")
    out += _budget_caveat(record.base_notes, "the baseline side")
    if record.k == 1 and (record.base_notes or record.candidate_notes or record.reviewer):
        out.append(
            "every case was measured once on each side (k=1), and the gate fails on a single case "
            "moving from pass to fail — so a case the reviewer is not consistent about can decide "
            "the verdict. Raise `trials` in the skill's evaluate step to require agreement."
        )
    if record.trace_diverged:
        out.append(
            "the two sides did not investigate the same way, so part of the difference between "
            "them may be what the reviewer read rather than what the guidance said"
        )
    if not record.config.targeted_cases and record.result.passed:
        out.append(
            "no case was named as one this change should fix, so a pass proves only that nothing "
            "broke — not that anything improved"
        )
    return out


# --- shared ----------------------------------------------------------------------


def _forced_notes(cases: list[CaseRun]) -> dict[str, str]:
    """Per case, its first trial note — the run-side twin of `service.case_notes`."""
    out: dict[str, str] = {}
    for case in cases:
        note = next((t.note for t in case.trials if t.note), "")
        if note:
            out[case.case_id] = note
    return out


def _budget_caveat(notes: dict[str, str], side: str) -> list[str]:
    """The one caveat that most often explains a mysterious result, stated with the cases named."""
    if not notes:
        return []
    where = f" on {side}" if side else ""
    named = ", ".join(sorted(notes)[:5])
    more = f" and {len(notes) - 5} more" if len(notes) > 5 else ""
    return [
        f"{len(notes)} case(s){where} ran out of investigation budget and were made to answer "
        f"with what they had — {named}{more}. An answer given under that pressure is not the "
        f"same as a considered one; raise `max_steps` before reading these as judgements."
    ]


def _sampling_caveat(score: SkillScore, k: int) -> list[str]:
    """Say when one case is a large fraction of the score, because then nothing here is stable."""
    scorable = score.scorable
    if scorable == 0 or scorable > 12:
        return []
    share = 1.0 / scorable
    trials = "once" if k == 1 else f"{k} times"
    return [
        f"{scorable} case(s), each measured {trials} — one case is {share:.2f} of the score, so "
        f"small movements here are not evidence of anything"
    ]
