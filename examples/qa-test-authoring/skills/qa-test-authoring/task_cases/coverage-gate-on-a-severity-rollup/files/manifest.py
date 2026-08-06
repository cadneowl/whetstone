"""Rolling dependency-scan findings up into a build verdict."""

SEVERITY_ORDER = ("none", "low", "medium", "high", "critical")


def worst_severity(findings: list[dict]) -> str:
    """The highest severity among `findings`, or "none" when there are none."""
    worst = "none"
    for finding in findings:
        level = finding.get("severity", "none")
        if level not in SEVERITY_ORDER:
            raise ValueError(f"unknown severity {level!r}")
        if SEVERITY_ORDER.index(level) > SEVERITY_ORDER.index(worst):
            worst = level
    return worst


def is_blocking(findings: list[dict], *, gate: str = "high") -> bool:
    """Whether these findings should fail the build: anything at `gate` severity or above."""
    return SEVERITY_ORDER.index(worst_severity(findings)) >= SEVERITY_ORDER.index(gate)
