"""Grade written tests by trying to break the code they claim to protect.

This skill's whole thesis is that "a test exists to catch a bug before a customer does, not to make
a coverage number go up", and its third non-negotiable says so operationally:

    3. **Can fail.** Mentally (or actually) mutate the code under test — flip a condition,
       off-by-one a boundary — and confirm the test would go red.

A verifier that just ran `pytest -q` would score exactly the thing the skill exists to argue
against. `def test_split(): split_evenly(100, 3)` passes pytest, adds a line of coverage, and
catches nothing — and under a plain exit-code grader it is indistinguishable from a real test. So
this grader does what the guidance tells its reader to do, and grades on the answer:

    1. the tests must pass against the correct source        (they describe the code as it is)
    2. every hand-authored mutant must make them fail        (they would notice if it were wrong)

Score is the fraction of mutants killed, so a partial answer moves the number instead of waiting
for a whole case to flip — which is what lets a gate see a draft that got better without yet being
right (`whetstone.verify.base.VerifyOutcome`).

**Why 100% is the bar here and not in a real codebase.** `references/mutation-testing.md` is
explicit that chasing a 100% mutation score is a mistake, because equivalent mutants make it
unreachable and the pursuit produces implementation-mirroring tests. That is a statement about
*generated* mutants. Every mutant here is hand-authored and hand-checked to change observable
behaviour, so there are no equivalent ones to be defeated by, and a survivor is always a real gap.

Contract (`whetstone.verify.program.ProgramVerifier`): the case, the workspace and what the skill
produced arrive as JSON on stdin; a `VerifyOutcome` goes out on stdout. Exiting non-zero means the
*grader* broke, which stops the run rather than being blamed on the skill — so every verdict about
the work, however bad, still leaves here with exit 0.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Long enough for a cold interpreter start on a loaded machine, short enough that the worst case —
# a baseline plus five mutants, every one of them hanging — still lands inside the 300s the step
# gives this grader. Overrun that and `ProgramVerifier` raises, which ends the whole run: a hung
# test the skill wrote must cost its own case and nothing else.
PYTEST_TIMEOUT_S = 30
# pytest's own exit code for "the run was fine, there was simply nothing to run". Worth naming,
# because it is the single likeliest way for this grader to be handed nothing at all.
NO_TESTS_COLLECTED = 5
_TAIL = 1500


def main() -> int:
    payload = json.load(sys.stdin)
    case: dict[str, Any] = payload["case"]
    workspace = Path(payload["workspace"])
    mutants: list[dict[str, Any]] = list(case.get("verify", {}).get("mutants") or [])

    tampered = _tampered(case.get("files") or {}, workspace)
    if tampered:
        # Before running anything: the tests may well pass, and that is precisely the problem. A
        # skill that edits the code under test until the tests agree with it has inverted the job,
        # and no amount of mutation testing downstream would notice, because the mutants would be
        # applied to source the skill had already rewritten.
        return _emit(
            passed=False,
            score=0.0,
            metrics={"mutants": float(len(mutants)), "killed": 0.0, "baseline_passed": 0.0},
            detail=(
                "the code under test was modified: "
                + ", ".join(tampered)
                + ". The task is to write tests for this code, not to change it until they pass."
            ),
        )

    try:
        baseline = _pytest(workspace)
    except subprocess.TimeoutExpired:
        # The skill's own doing — a sleep, or a loop that never ends. Its case, not the run: raising
        # here would reach `ProgramVerifier` as a broken *grader* and stop every case after it.
        return _emit(
            passed=False,
            score=0.0,
            metrics={"mutants": float(len(mutants)), "killed": 0.0, "baseline_passed": 0.0},
            detail=(
                f"the tests did not finish within {PYTEST_TIMEOUT_S}s. Quality bar item 4: no "
                "sleeps, no unbounded waits — for an async result, poll with a timeout."
            ),
        )
    if baseline.returncode != 0:
        return _emit(
            passed=False,
            score=0.0,
            metrics={"mutants": float(len(mutants)), "killed": 0.0, "baseline_passed": 0.0},
            detail=_baseline_detail(baseline),
        )

    if not mutants:
        # A case with no mutants declared is graded on the baseline alone. Legitimate, and said
        # out loud rather than reported as a flawless mutation score over nothing.
        return _emit(
            passed=True,
            score=1.0,
            metrics={"mutants": 0.0, "killed": 0.0, "baseline_passed": 1.0},
            detail="tests pass; this case declares no mutants, so nothing probed whether they bite",
        )

    reading = _reads_its_own_source(case.get("files") or {}, workspace)
    if reading:
        return _emit(
            passed=False,
            score=0.0,
            metrics={"mutants": float(len(mutants)), "killed": 0.0, "baseline_passed": 1.0},
            detail=(
                f"{reading} reads the source under test as a file. A test asserting that "
                '"if attempt < 1:" appears in retry.py passes the baseline and fails every '
                "mutation, so it scores perfectly while claiming nothing about behaviour. Your "
                "own references/mutation-testing.md: never kill a mutant by writing a test that "
                "mirrors the mutated line — assert real outputs."
            ),
        )

    survivors = [m for m in mutants if not _kills(workspace, m)]
    killed = len(mutants) - len(survivors)
    return _emit(
        passed=not survivors,
        score=killed / len(mutants),
        metrics={
            "mutants": float(len(mutants)),
            "killed": float(killed),
            "baseline_passed": 1.0,
        },
        detail=_detail(killed, len(mutants), survivors),
    )


def _tampered(seed: dict[str, str], workspace: Path) -> list[str]:
    """Seeded files the skill deleted or rewrote, by path.

    Compared on text with line endings normalised, because a `write_file` round-trip through a
    model can change `\\r\\n` to `\\n` without changing a single character anyone cares about, and
    failing a case for that would be a grader bug wearing a verdict's clothes.
    """
    out = []
    for path, original in sorted(seed.items()):
        actual = workspace / path
        if not actual.is_file():
            out.append(f"{path} (deleted)")
        elif _normalise(actual.read_text(encoding="utf-8")) != _normalise(original):
            out.append(path)
    return out


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


# Ways a test can look at the code as *text* rather than run it. Not exhaustive, and not meant to
# be — see `_reads_its_own_source`.
_READS = ("open(", ".read_text(", ".read_bytes(", "getsource", "__file__", "linecache")


def _reads_its_own_source(seed: dict[str, str], workspace: Path) -> str:
    """The test file that inspects the source under test instead of exercising it, or `""`.

    The one way to score 1.00 here while testing nothing. `assert "if attempt < 1:" in
    open("retry.py").read()` passes against the correct source and fails against every mutation of
    that line, so it kills the whole corpus and asserts nothing about what the code *does*. It is
    also precisely the cheat this skill's own `references/mutation-testing.md` forbids: "never kill
    a mutant by writing a test that mirrors the mutated line".

    **A heuristic, and deliberately a narrow one.** It fires only when a seeded filename appears as
    a literal *and* the same file calls something that reads a file — because a module docstring
    saying "Tests for retry.py" is normal and must not be a failure. It defends against a
    deliberate cheat, not against a determined one; a test could still reach the source through an
    import trick this never sees. That is an acceptable bar for a grader whose subject is a model
    writing tests in good faith, and it is written down rather than left as a surprise.
    """
    for path in sorted(p for p in workspace.rglob("*.py") if p.is_file()):
        name = path.relative_to(workspace).as_posix()
        if name in seed:
            continue  # the source itself, already checked for tampering
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not any(token in text for token in _READS):
            continue
        if any(f'"{s}"' in text or f"'{s}'" in text for s in seed):
            return name
    return ""


def _kills(workspace: Path, mutant: dict[str, Any]) -> bool:
    """Whether the skill's tests notice this mutant.

    Run against a *copy*, never the workspace itself. `whetstone eval task --keep` exists so a
    failing case can be read afterwards, and the thing worth reading is what the skill actually
    wrote — mutating in place and restoring would leave that evidence one crashed grader away from
    being the mutant instead.
    """
    with tempfile.TemporaryDirectory(prefix="qa-mutant-") as tmp:
        copy = Path(tmp) / "w"
        shutil.copytree(workspace, copy, ignore=shutil.ignore_patterns("__pycache__", ".*_cache"))
        target = _target(copy, mutant)
        source = target.read_text(encoding="utf-8")
        find = _required(mutant, "find")
        # Exactly one, not "at least one". `str.replace(..., 1)` patches the *first* occurrence, so
        # a find-string that also appears in a comment or a docstring mutates the prose and leaves
        # the code alone — an equivalent mutant by accident, reported as survived, marking the skill
        # down for a line the grader never touched. Nothing about the YAML would show it.
        hits = source.count(find)
        if hits != 1:
            # A case-authoring error, not a skill failure. Fatal on purpose: scoring the mutant as
            # survived would silently mark the skill down for a line this file never patched, and
            # scoring it as killed would hand out a pass nobody earned.
            was = "is not in" if hits == 0 else f"appears {hits} times in"
            raise SystemExit(
                f"mutant {mutant.get('id', '?')!r} does not apply cleanly: {find!r} {was} "
                f"{mutant['file']} — a mutant must name exactly one place in the source"
            )
        target.write_text(source.replace(find, _required(mutant, "replace"), 1), encoding="utf-8")
        try:
            return _pytest(copy).returncode != 0
        except subprocess.TimeoutExpired:
            # The mutant made the tests hang. They did not pass, and something in them clearly
            # depends on the mutated line — counted as killed, which is the reading that cannot
            # flatter the skill's score by mistake.
            return True


def _required(mutant: dict[str, Any], key: str) -> str:
    """A mutant's field, or a sentence instead of a `KeyError` traceback.

    Same class of failure as a mutant that does not apply: the case is malformed, the grader cannot
    answer, and the run stops. It may as well say which key is missing.
    """
    if key not in mutant:
        raise SystemExit(f"mutant {mutant.get('id', '?')!r} has no {key!r}")
    return str(mutant[key])


def _target(root: Path, mutant: dict[str, Any]) -> Path:
    """The file a mutant patches, checked to be inside the workspace copy.

    `file:` comes from committed config and is as trusted as the `run:` line that invoked this
    grader — so this is not a security boundary. It is a diagnostic one: a typo'd path currently
    surfaces as a `FileNotFoundError` traceback naming a temp directory, which reads like a broken
    grader rather than a broken case.
    """
    path = (root / _required(mutant, "file")).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise SystemExit(
            f"mutant {mutant.get('id', '?')!r} names {mutant['file']!r}, which is not a file in "
            f"the case's workspace — `file:` is relative to the case's own files/"
        )
    return path


def _pytest(cwd: Path) -> subprocess.CompletedProcess[str]:
    """`pytest -q` in `cwd`, with every cache disabled.

    Bytecode and pytest caches are the classic way a mutation run lies: the second invocation
    imports the *first* one's compiled module, the mutant never executes, and it is scored as
    killed or survived on the strength of code that was not the code under test.
    """
    return subprocess.run(  # noqa: S603 - our own interpreter, on the skill's own workspace
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=PYTEST_TIMEOUT_S,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _baseline_detail(done: subprocess.CompletedProcess[str]) -> str:
    if done.returncode == NO_TESTS_COLLECTED:
        return (
            "no tests were collected — nothing was written, or the file is not named `test_*.py`. "
            "A description of a test is not a test."
        )
    return (
        "the tests do not pass against the unmodified source, so they describe code that does not "
        f"exist:\n{(done.stdout or done.stderr).strip()[-_TAIL:]}"
    )


def _detail(killed: int, total: int, survivors: list[dict[str, Any]]) -> str:
    """The verdict, and — when there is one — the gap list.

    Phrased the way `references/mutation-testing.md` says to phrase it: a survivor is not a score,
    it is the sentence "your tests permit this specific wrong behaviour", and the fix is a specific
    assertion. The skill is told this in its own guidance; the grader owes it the same courtesy.
    """
    head = f"{killed}/{total} mutants killed"
    if not survivors:
        return f"{head} — every planted bug made a test go red"
    lines = [f"{head}. These bugs were planted and the tests stayed green:"]
    for m in survivors:
        says = m.get("describes") or f"{m['find']} -> {m['replace']}"
        # Collapsed: `describes` is written as a folded YAML scalar, which keeps a trailing newline
        # and would otherwise blank-line the gap list apart in every console and CLI that shows it.
        lines.append(f"  - {m.get('id', '?')}: {' '.join(str(says).split())}")
    return "\n".join(lines)


def _emit(*, passed: bool, score: float, metrics: dict[str, float], detail: str) -> int:
    json.dump({"passed": passed, "score": score, "metrics": metrics, "detail": detail}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
