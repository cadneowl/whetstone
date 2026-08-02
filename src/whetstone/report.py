"""Render a run record as a self-contained HTML report, or as plain text.

The HTML is one file with inline CSS and no scripts — it opens from disk, attaches to a CI job, and
pastes into a merge request. Expansion uses `<details>`, so the drill-down works with no JavaScript.

This is the report the console's run-detail screen later reproduces interactively: score at the top,
then per-case, per-trial, down to each finding and the judge's reason for accepting or rejecting it.
"""

from __future__ import annotations

from html import escape

from whetstone.domain.run import CaseRun, ExpectationOutcome, RunRecord, TrialRecord
from whetstone.domain.score import SkillScore

_OUTCOME_LABEL = {"tp": "TP", "fn": "FN", "fp": "FP", "tn": "TN"}
_OUTCOME_TITLE = {
    "tp": "caught, as expected",
    "fn": "missed — the reviewer should have flagged this",
    "fp": "falsely flagged — the reviewer should have stayed quiet",
    "tn": "correctly silent",
}

_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e2e2e2; --panel: #fafafa;
  --good: #1a7f37; --bad: #cf222e; --warn: #9a6700; --accent: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --line: #30363d; --panel: #161b22;
    --good: #3fb950; --bad: #f85149; --warn: #d29922; --accent: #58a6ff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem; background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 62rem; margin: 0 auto; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 2rem 0 .75rem; text-transform: uppercase;
     letter-spacing: .06em; color: var(--muted); }
code, .mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .9em;
}
.sub { color: var(--muted); margin: 0 0 1.5rem; }
.metrics {
  display: flex; flex-wrap: wrap; gap: .75rem; margin: 0 0 1rem; padding: 0; list-style: none;
}
.metrics li {
  border: 1px solid var(--line); border-radius: .5rem; padding: .5rem .85rem;
  background: var(--panel); min-width: 7.5rem;
}
.metrics .k {
  display: block; color: var(--muted); font-size: .75rem; text-transform: uppercase;
  letter-spacing: .05em;
}
.metrics .v { font-size: 1.25rem; font-variant-numeric: tabular-nums; }
.facts {
  display: flex; flex-wrap: wrap; gap: .35rem 1.25rem; color: var(--muted); font-size: .85rem;
  margin: 0 0 1.5rem; padding: 0; list-style: none;
}
details {
  border: 1px solid var(--line); border-radius: .5rem; margin-bottom: .5rem;
  background: var(--panel);
}
details > summary {
  cursor: pointer; padding: .6rem .85rem; display: flex; gap: .6rem;
  align-items: center; flex-wrap: wrap;
}
details > summary::marker { color: var(--muted); }
details .body { padding: .25rem .85rem .85rem 1.75rem; }
details details { background: var(--bg); }
.badge {
  border-radius: 1rem; padding: .05rem .5rem; font-size: .75rem; border: 1px solid var(--line);
}
.badge.catch { color: var(--accent); }
.badge.noflag { color: var(--muted); }
.badge.flaky { color: var(--warn); border-color: var(--warn); }
.badge.practice { color: var(--warn); border-color: var(--warn); }
.chip {
  font-size: .75rem; padding: .05rem .4rem; border-radius: .25rem;
  border: 1px solid var(--line); font-family: ui-monospace, monospace;
}
.chip.tp, .chip.tn { color: var(--good); border-color: var(--good); }
.chip.fn, .chip.fp { color: var(--bad); border-color: var(--bad); }
.grow { margin-left: auto; color: var(--muted); font-variant-numeric: tabular-nums; }
.expect { border-left: 2px solid var(--line); padding-left: .85rem; margin: .75rem 0; }
.expect .semantic { margin: 0 0 .25rem; }
.where { color: var(--muted); font-size: .85rem; }
.finding { margin: .5rem 0; }
.finding .head { display: flex; gap: .5rem; align-items: baseline; flex-wrap: wrap; }
.verdict { color: var(--muted); font-size: .88rem; margin: .15rem 0 0 1rem; }
.verdict.yes { color: var(--good); }
.verdict.no { color: var(--bad); }
.none { color: var(--muted); font-style: italic; }
footer {
  color: var(--muted); font-size: .8rem; margin-top: 2.5rem;
  border-top: 1px solid var(--line); padding-top: .75rem;
}
"""


def render_run_html(record: RunRecord) -> str:
    """A complete, standalone HTML document for one run."""
    score = record.score
    title = f"Whetstone run — {record.skill_id} v{record.skill_version}"
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{escape(record.skill_id)} <span class='mono'>v{record.skill_version}</span>"
        f"{_practice_badge(record)}</h1>",
        f"<p class='sub'>Run <code>{escape(record.id)}</code> · "
        f"{escape(record.created_at.strftime('%Y-%m-%d %H:%M:%S %Z').strip())}</p>",
        _metrics_html(score),
        _facts_html(record),
        "<h2>Cases</h2>",
    ]
    parts.extend(_case_html(case) for case in record.cases)
    if not record.cases:
        parts.append("<p class='none'>This skill has no eval cases.</p>")
    parts.append(
        "<footer>Generated by <code>whetstone report</code>. "
        "Findings and judge verdicts are captured verbatim from the run.</footer>"
    )
    parts.append("</main></body></html>")
    return "\n".join(parts)


def render_run_text(record: RunRecord) -> str:
    """A terminal summary of a run — what `whetstone runs show` prints."""
    score = record.score
    lines = [
        f"Run {record.id}",
        f"  skill    {record.skill_id} v{record.skill_version}  ({record.skill_hash[:12]})",
        f"  when     {record.created_at.isoformat()}",
        f"  backend  {record.backend or '-'}  model {record.model or '-'}"
        + ("  [practice]" if record.practice_mode else ""),
        f"  trials   k={record.k}   llm calls {record.llm_calls}   {record.duration_s:.1f}s",
        # The population and the instrument, beside the number. Two runs of one skill over
        # different case sets, or judged differently, are different measurements — and without
        # these a case passing in one and failing in the other reads as scoring that contradicts
        # itself rather than two experiments that were never comparable.
        f"  measured {len(record.cases)} case(s)"
        + (f"   judge {record.judge_hash[:12]}" if record.judge_hash else ""),
        f"  score    recall {score.recall:.3f}   fp_rate {score.fp_rate:.3f}   "
        f"precision {score.precision:.3f}   F2 {score.f_beta():.3f}",
    ]
    if record.reviewer:
        lines.append(f"  reviewer {record.reviewer}")
    # What the agent actually opened. Recorded since the agent runtime landed but shown nowhere,
    # which made it evidence only someone reading raw JSON could reach — and the whole argument for
    # keeping it is that a reader can see why a score moved.
    if record.reviewer_trace:
        lines.append("  read     " + "; ".join(record.reviewer_trace))
    if score.errors:
        # "over the rest" is only honest while there *is* a rest. With none, the metrics above are
        # an artifact of empty confusions — recall 1.000 over nothing — and saying so is the whole
        # job of this line.
        rest = (
            f"recall is over the remaining {score.scorable}"
            if score.scorable
            else "NOTHING was measured, so the score above is meaningless — fix the reviewer"
        )
        lines.append(f"  errors   {score.errors} case(s) could not be scored — {rest}")
    lines.append("  cases:")
    for case in record.cases:
        tag = "catch " if case.kind == "should_catch" else "noflag"
        metric = (
            f"recall {case.confusion.recall:.2f}"
            if case.kind == "should_catch"
            else f"fp_rate {case.confusion.fp_rate:.2f}"
        )
        if case.error:
            lines.append(f"    [error ] {case.case_id:<32} {case.error[:60]}")
            continue
        flag = "  ⚠ flaky" if case.flaky else ""
        lines.append(f"    [{tag}] {case.case_id:<32} {metric}{flag}")
    return "\n".join(lines)


# --- html fragments -----------------------------------------------------------


def _practice_badge(record: RunRecord) -> str:
    if not record.practice_mode:
        return ""
    return " <span class='badge practice'>practice mode — no model was called</span>"


def _metrics_html(score: SkillScore) -> str:
    items = [
        ("recall", f"{score.recall:.3f}"),
        ("fp rate", f"{score.fp_rate:.3f}"),
        ("precision", f"{score.precision:.3f}"),
        ("F2", f"{score.f_beta():.3f}"),
    ]
    if score.k > 1:
        items.append(("recall stdev", f"{score.recall_stdev:.3f}"))
        items.append(("fp stdev", f"{score.fp_rate_stdev:.3f}"))
    cells = "".join(
        f"<li><span class='k'>{escape(k)}</span><span class='v'>{escape(v)}</span></li>"
        for k, v in items
    )
    return f"<ul class='metrics'>{cells}</ul>"


def _facts_html(record: RunRecord) -> str:
    """What this number is a measurement *of*.

    A standalone report is the artifact people compare side by side, and two of them headed
    `architect-skill v5` read as the same experiment run twice. They are not comparable unless the
    case set, the judge and the reviewer all match — a run over the graduated corpus and a run over
    that corpus plus the promoted batch are different populations, and a different judge is a
    different instrument. The console says all three; the report said none of them, so a case that
    passed in one and failed in the other looked like the scoring contradicting itself.
    """
    facts = [
        f"{len(record.cases)} case(s)",
        f"backend <code>{escape(record.backend or '-')}</code>",
        f"model <code>{escape(record.model or '-')}</code>",
        f"k={record.k}",
        f"effort {escape(record.reviewer_effort)}/{escape(record.judge_effort)}",
        f"{record.llm_calls} llm calls",
        f"{record.duration_s:.1f}s",
        f"skill hash <code>{escape(record.skill_hash[:12])}</code>",
    ]
    if record.judge_hash:
        facts.append(f"judge <code>{escape(record.judge_hash[:12])}</code>")
    if record.reviewer:
        facts.append(f"reviewer <code>{escape(record.reviewer)}</code>")
    if record.git_ref:
        facts.append(f"ref <code>{escape(record.git_ref[:12])}</code>")
    if record.principal:
        facts.append(f"by {escape(record.principal)}")
    return "<ul class='facts'>" + "".join(f"<li>{f}</li>" for f in facts) + "</ul>"


def _case_html(case: CaseRun) -> str:
    confusion = case.confusion
    metric = (
        f"recall {confusion.recall:.2f}"
        if case.kind == "should_catch"
        else f"fp_rate {confusion.fp_rate:.2f}"
    )
    kind_class = "catch" if case.kind == "should_catch" else "noflag"
    kind_label = "should catch" if case.kind == "should_catch" else "should not flag"
    flaky = "<span class='badge flaky'>flaky</span>" if case.flaky else ""
    chips = "".join(_outcome_chip(o) for t in case.trials for o in t.outcomes)
    trials = "".join(_trial_html(case, t) for t in case.trials)
    return (
        "<details><summary>"
        f"<span class='badge {kind_class}'>{kind_label}</span>"
        f"<strong class='mono'>{escape(case.case_id)}</strong>{flaky}"
        f"<span class='grow'>{chips} &nbsp; {escape(metric)}</span>"
        f"</summary><div class='body'>{trials}</div></details>"
    )


def _trial_html(case: CaseRun, trial: TrialRecord) -> str:
    chips = "".join(_outcome_chip(o) for o in trial.outcomes)
    expectations = "".join(_expectation_html(trial, o) for o in trial.outcomes)
    unmatched = _unmatched_html(trial)
    label = f"Trial {trial.index + 1} of {len(case.trials)}"
    return (
        f"<details><summary><strong>{escape(label)}</strong>"
        f"<span class='grow'>{chips}</span></summary>"
        f"<div class='body'>{expectations}{unmatched}</div></details>"
    )


_EXCLUSION_TEXT = {
    "other_file": "on a different file",
    "outside_region": "outside the expected line range",
    "below_severity": "below the required severity",
}


def _expectation_html(trial: TrialRecord, outcome: ExpectationOutcome) -> str:
    rows = [
        _verdict_html(trial, v.finding_index, v.matched, v.reason, v.confidence)
        for v in outcome.verdicts
    ]
    for index in outcome.unjudged_finding_indices:
        rows.append(_unjudged_html(trial, index))
    body = "".join(rows) or (
        "<p class='none'>No finding was eligible — nothing the reviewer reported reached the "
        "judge for this expectation.</p>"
    )
    return (
        "<div class='expect'>"
        f"<p class='semantic'><span class='chip {outcome.outcome}'>"
        f"{_OUTCOME_LABEL[outcome.outcome]}</span> "
        f"<strong>must {escape(outcome.must.replace('_', ' '))}</strong> "
        f"<span class='where'>({_OUTCOME_TITLE[outcome.outcome]})</span></p>"
        f"{_expected_html(outcome)}"
        f"{body}"
        f"{_excluded_html(trial, outcome)}"
        "</div>"
    )


def _expected_html(outcome: ExpectationOutcome) -> str:
    """What the expectation actually asserted — without it, a failure is undiagnosable."""
    parts: list[str] = []
    if outcome.semantic:
        parts.append(f"<q>{escape(outcome.semantic)}</q>")
    if outcome.where is not None:
        location = escape(outcome.where.path)
        if outcome.where.line_range:
            lo, hi = outcome.where.line_range
            location += f" lines {lo}–{hi}"
        parts.append(f"<code>{location}</code>")
        # The anchor is one line; matching ran against everything the change touches. Printing only
        # the anchor makes an accepted finding on a nearby line look like a scoring error.
        used = outcome.considered.line_range if outcome.considered else None
        if used is not None and used != outcome.where.line_range:
            parts.append(f"matched across lines {used[0]}–{used[1]} of the change")
    if outcome.severity_min is not None:
        parts.append(f"severity ≥ {escape(outcome.severity_min.name)}")
    if not parts:
        # Pre-enrichment record: say so rather than implying the expectation was empty.
        return (
            f"<p class='where'>expectation <code>{escape(outcome.expectation_id)}</code> "
            "(text not recorded by this run)</p>"
        )
    return f"<p class='where'>Expected: {' · '.join(parts)}</p>"


def _excluded_html(trial: TrialRecord, outcome: ExpectationOutcome) -> str:
    """Findings the prefilter dropped, and why.

    A reviewer that flagged the right line one severity too low reads as total silence otherwise —
    and those are opposite problems with opposite fixes.
    """
    excluded = [e for e in outcome.excluded_findings(trial.findings) if e.reason != "other_file"]
    if not excluded:
        return ""
    rows = "".join(
        f"<div class='finding'>{_finding_head(trial, e.finding_index)}"
        f"<div class='verdict'>not judged — {_EXCLUSION_TEXT[e.reason]}</div></div>"
        for e in excluded
    )
    return f"<p class='where'>Filtered out before judging:</p>{rows}"


def _verdict_html(
    trial: TrialRecord, index: int, matched: bool, reason: str, confidence: float
) -> str:
    verdict_class = "yes" if matched else "no"
    verdict_word = "MATCHED" if matched else "NOT MATCHED"
    return (
        f"<div class='finding'>{_finding_head(trial, index)}"
        f"<div class='verdict {verdict_class}'>judge: {verdict_word} "
        f"(confidence {confidence:.2f}) — {escape(reason) or '<em>no reason given</em>'}</div>"
        f"</div>"
    )


def _unjudged_html(trial: TrialRecord, index: int) -> str:
    return (
        f"<div class='finding'>{_finding_head(trial, index)}"
        "<div class='verdict'>not judged — an earlier finding already matched</div></div>"
    )


def _finding_head(trial: TrialRecord, index: int) -> str:
    if index >= len(trial.findings):
        return f"<div class='head'><span class='where'>finding #{index} (missing)</span></div>"
    f = trial.findings[index]
    location = f"{f.path}:{f.line}" if f.line is not None else f.path
    rule = f" <code>{escape(f.rule_id)}</code>" if f.rule_id else ""
    # `is not None`, not truthiness: a reported confidence of 0.00 is a real and interesting value.
    confidence = (
        f" <span class='where'>conf {f.confidence:.2f}</span>" if f.confidence is not None else ""
    )
    return (
        f"<div class='head'><code>{escape(location)}</code>"
        f"<span class='where'>{escape(f.severity.name)}</span>{rule}"
        f"<span>{escape(f.message)}</span>{confidence}</div>"
    )


def _unmatched_html(trial: TrialRecord) -> str:
    indices = trial.unmatched_finding_indices()
    if not indices:
        return ""
    rows = "".join(f"<div class='finding'>{_finding_head(trial, i)}</div>" for i in indices)
    return (
        "<div class='expect'><p class='semantic'>Findings matching no expectation "
        "<span class='where'>— candidates for a new eval case, either a missing "
        "<code>should_catch</code> or noise worth pinning with <code>should_not_flag</code>"
        "</span></p>"
        f"{rows}</div>"
    )


def _outcome_chip(outcome: ExpectationOutcome) -> str:
    label = _OUTCOME_LABEL[outcome.outcome]
    return (
        f"<span class='chip {outcome.outcome}' title='{escape(_OUTCOME_TITLE[outcome.outcome])}'>"
        f"{label}</span>"
    )
