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


class GateResult(BaseModel):
    passed: bool
    reasons: list[str]
    regressed_cases: list[str]
    recall_old: float
    recall_new: float
    fp_rate_old: float
    fp_rate_new: float


def gate(old: SkillScore, new: SkillScore, cfg: GateConfig | None = None) -> GateResult:
    """Compare a candidate skill score against the baseline. PASS requires all guards to hold."""
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

    return GateResult(
        passed=not reasons,
        reasons=reasons,
        regressed_cases=regressed,
        recall_old=old.recall,
        recall_new=new.recall,
        fp_rate_old=old.fp_rate,
        fp_rate_new=new.fp_rate,
    )
