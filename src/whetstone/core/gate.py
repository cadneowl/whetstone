from __future__ import annotations

from pydantic import BaseModel

from whetstone.domain.score import SkillScore


class GateConfig(BaseModel):
    """Tolerances for promoting a skill change. Defaults are strict: no recall loss, no new false
    positives, no case that used to pass may start failing.
    """

    recall_tol: float = 0.0
    fp_tol: float = 0.0
    max_case_regressions: int = 0
    case_recall_floor: float = 0.999
    case_fp_ceiling: float = 0.001
    # Cases this change claims to fix. Without them the gate only ever blocks *regressions*, so a
    # guidance edit that does nothing at all passes — it is a rot guard, not a sharpening one.
    # Naming the cases turns "I didn't break anything" into "I fixed what I said I would".
    targeted_cases: list[str] = []


class GateResult(BaseModel):
    passed: bool
    reasons: list[str]
    regressed_cases: list[str]
    recall_old: float
    recall_new: float
    fp_rate_old: float
    fp_rate_new: float
    # Targeted cases the candidate still fails, and those it actually fixed — reported separately so
    # a PASS says which claims were made good on, not merely that nothing broke.
    unfixed_cases: list[str] = []
    fixed_cases: list[str] = []


def gate(old: SkillScore, new: SkillScore, cfg: GateConfig | None = None) -> GateResult:
    """Compare a candidate skill score against the baseline. PASS requires all guards to hold.

    Both scores are expected to cover the *same* set of cases — see `service.gate_skills`, which
    scores each side's guidance over the union. Comparing scores taken over different case sets
    conflates "the guidance got worse" with "the case set got harder".
    """
    cfg = cfg or GateConfig()
    reasons: list[str] = []

    if new.recall < old.recall - cfg.recall_tol:
        reasons.append(
            f"recall regressed {old.recall:.3f} -> {new.recall:.3f} (tol {cfg.recall_tol})"
        )
    if new.fp_rate > old.fp_rate + cfg.fp_tol:
        reasons.append(
            f"false-positive rate rose {old.fp_rate:.3f} -> {new.fp_rate:.3f} (tol {cfg.fp_tol})"
        )

    old_by_id = {c.case_id: c for c in old.cases}
    regressed: list[str] = []
    for nc in new.cases:
        oc = old_by_id.get(nc.case_id)
        if oc is None:
            continue
        was_ok = oc.passed(cfg.case_recall_floor, cfg.case_fp_ceiling)
        now_ok = nc.passed(cfg.case_recall_floor, cfg.case_fp_ceiling)
        if was_ok and not now_ok:
            regressed.append(nc.case_id)
    if len(regressed) > cfg.max_case_regressions:
        reasons.append(
            f"{len(regressed)} case(s) regressed (max {cfg.max_case_regressions}): "
            + ", ".join(regressed)
        )

    fixed, unfixed = _targeted(old, new, cfg, reasons)

    return GateResult(
        passed=not reasons,
        reasons=reasons,
        regressed_cases=regressed,
        recall_old=old.recall,
        recall_new=new.recall,
        fp_rate_old=old.fp_rate,
        fp_rate_new=new.fp_rate,
        unfixed_cases=unfixed,
        fixed_cases=fixed,
    )


def _targeted(
    old: SkillScore, new: SkillScore, cfg: GateConfig, reasons: list[str]
) -> tuple[list[str], list[str]]:
    """Check the cases the change claims to fix, appending a reason for each one it didn't.

    "Improve" is read as *ends up passing*. Requiring a strictly better number instead would be
    unsatisfiable for a case already at the ceiling, and the honest question about a targeted case
    is binary anyway: the change was proposed to make it pass, so did it.
    """
    if not cfg.targeted_cases:
        return [], []

    old_by_id = {c.case_id: c for c in old.cases}
    new_by_id = {c.case_id: c for c in new.cases}
    fixed: list[str] = []
    unfixed: list[str] = []

    for case_id in cfg.targeted_cases:
        nc = new_by_id.get(case_id)
        if nc is None:
            # Naming a case that isn't scored is a typo or a stale reference, not a pass. Silently
            # ignoring it would make `--targeted` look enforced while enforcing nothing.
            unfixed.append(case_id)
            reasons.append(f"targeted case {case_id!r} is not in the candidate's eval set")
            continue
        if not nc.passed(cfg.case_recall_floor, cfg.case_fp_ceiling):
            unfixed.append(case_id)
            metric = (
                f"recall {nc.recall:.3f}"
                if nc.kind == "should_catch"
                else f"fp_rate {nc.fp_rate:.3f}"
            )
            reasons.append(f"targeted case {case_id!r} still fails ({metric})")
            continue
        oc = old_by_id.get(case_id)
        if oc is None or not oc.passed(cfg.case_recall_floor, cfg.case_fp_ceiling):
            fixed.append(case_id)

    return fixed, unfixed
