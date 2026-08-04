"""The subprocess reviewer: the payload it sends, the findings it parses, and how it fails."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from whetstone.context import ResolvedContext
from whetstone.core.harness import RunCancelled
from whetstone.domain.change import parse_unified_diff
from whetstone.domain.refs import RepoRef
from whetstone.domain.skill import Skill
from whetstone.reviewer.subprocess_reviewer import SubprocessReviewer
from whetstone.steps import StepError

_DIFF = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -0,0 +1,1 @@
+print("hi")
"""


def _change() -> object:
    return parse_unified_diff(_DIFF, RepoRef.parse("local:x"))


def _skill() -> Skill:
    return Skill(id="s", body="rule R1")


def _script(tmp_path: Path, code: str) -> str:
    path = tmp_path / "rev.py"
    path.write_text(code, encoding="utf-8")
    return str(path)


def _reviewer(tmp_path: Path, code: str, **kw: object) -> SubprocessReviewer:
    return SubprocessReviewer(
        [sys.executable, _script(tmp_path, code)], cwd=tmp_path, timeout_s=30, **kw
    )


def test_returns_parsed_findings(tmp_path: Path) -> None:
    code = (
        "import sys, json; json.load(sys.stdin); "
        'print(json.dumps({"findings": [{"path": "x.py", "line": 1, "severity": "warning", '
        '"message": "m", "rule_id": "R1", "confidence": 0.9}]}))'
    )
    out = _reviewer(tmp_path, code).review(_skill(), _change())
    assert len(out) == 1
    assert out[0].path == "x.py"
    assert out[0].rule_id == "R1"
    assert out[0].line == 1


def test_payload_carries_guidance_and_context(tmp_path: Path) -> None:
    # Echo the guidance and a context value straight back through a finding, to assert the shape.
    code = (
        "import sys, json; d = json.load(sys.stdin); "
        'print(json.dumps({"findings": [{"path": d["context"]["src"], "line": 1, '
        '"severity": "info", "message": d["guidance"]}]}))'
    )
    context = ResolvedContext(values={"src": "/repo"}, redacted={"src": "<env:SRC>"})
    out = _reviewer(tmp_path, code, context=context).review(_skill(), _change())
    assert out[0].path == "/repo"
    assert out[0].message == "rule R1"


def test_the_program_gets_values_and_the_record_gets_the_redacted_view(tmp_path: Path) -> None:
    """The split that keeps a secret out of a record: the program is given the resolved value, the
    provenance stored alongside the findings names only where it came from."""
    context = ResolvedContext(
        values={"token": "s3cret"},
        redacted={"token": "<env:TOKEN>"},
        hashable={"ref": "abc123"},
    )
    code = 'import sys, json; json.load(sys.stdin); print(\'{"findings":[]}\')'
    reviewer = _reviewer(tmp_path, code, context=context)
    assert reviewer.provenance.context == {"token": "<env:TOKEN>"}
    assert "s3cret" not in str(reviewer.provenance.context)
    assert reviewer.provenance.context_digest == context.digest != ""
    assert reviewer.provenance.identity == reviewer.identity


def test_a_cancelled_run_stops_the_program_instead_of_waiting_out_its_timeout(
    tmp_path: Path,
) -> None:
    """Cancel has to reach the subprocess: the harness can only check between reviews, so a program
    that sleeps for its whole timeout would make Cancel look hung."""
    cancel = threading.Event()
    cancel.set()
    reviewer = SubprocessReviewer(
        [sys.executable, _script(tmp_path, "import time; time.sleep(60)")],
        cwd=tmp_path,
        timeout_s=60,
    )
    reviewer.bind_cancel(cancel)
    started = time.monotonic()
    with pytest.raises(RunCancelled):
        reviewer.review(_skill(), _change())
    assert time.monotonic() - started < 10  # not the 60s timeout


def test_payload_includes_the_change_refs(tmp_path: Path) -> None:
    # The program gets the whole change (repo + refs), not only the rendered diff — for a checkout.
    code = (
        "import sys, json; d = json.load(sys.stdin); "
        'assert "change" in d and "diff" in d; '
        'print(json.dumps({"findings": []}))'
    )
    assert _reviewer(tmp_path, code).review(_skill(), _change()) == []


def test_nonzero_exit_raises_step_error_with_stderr(tmp_path: Path) -> None:
    code = 'import sys; sys.stderr.write("boom"); sys.exit(3)'
    with pytest.raises(StepError, match="exited 3"):
        _reviewer(tmp_path, code).review(_skill(), _change())


def test_unparseable_output_raises(tmp_path: Path) -> None:
    with pytest.raises(StepError, match="findings"):
        _reviewer(tmp_path, 'print("not json")').review(_skill(), _change())


def test_missing_program_raises(tmp_path: Path) -> None:
    reviewer = SubprocessReviewer(
        ["definitely-not-a-real-binary-xyz"], cwd=tmp_path, timeout_s=5
    )
    with pytest.raises(StepError, match="cannot run"):
        reviewer.review(_skill(), _change())


def test_identity_names_the_program(tmp_path: Path) -> None:
    reviewer = SubprocessReviewer(["python", "rev.py"], cwd=tmp_path, timeout_s=5)
    assert reviewer.identity == "subprocess: python rev.py"


def test_unparseable_output_reports_stderr_too(tmp_path: Path) -> None:
    """A program that exits 0 with unusable stdout has usually already said why on stderr.

    The non-zero path always showed it; this one threw away the single line that explained the
    failure, leaving "got 'not json'" as the whole diagnosis.
    """
    code = (
        'import sys; sys.stderr.write("could not reach source root, reviewed blind"); '
        'print("not json")'
    )
    with pytest.raises(StepError, match="could not reach source root"):
        _reviewer(tmp_path, code).review(_skill(), _change())
