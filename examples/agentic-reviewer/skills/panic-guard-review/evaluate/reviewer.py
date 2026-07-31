#!/usr/bin/env python3
"""A source-aware reviewer program for Whetstone (see examples/agentic-reviewer/README.md).

Whetstone runs this once per case. It gets a JSON payload on stdin — the guidance, the numbered
diff, the full change, and the resolved ``context`` bag — and prints the findings on stdout. Its
judgement depends on code *outside* the diff: it opens the source tree named by
``context.source_root``, learns which functions are documented ``PANICS``, and flags a change that
calls one. Nothing in the diff says ``load_config`` can panic; only the source does.

The contract is the same one the built-in reviewer produces:
    stdin :  {"guidance", "pages", "change", "diff", "context", "wiki", "limits"}
    stdout:  {"findings": [{"path", "line", "severity", "message", "rule_id", "confidence"}]}
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_DEF = re.compile(r"^\s*def\s+(\w+)\s*\(")
# A line of the numbered diff Whetstone sends: "  12 | +    cfg = load_config()".
_ADDED = re.compile(r"^\s*(\d+) \| \+(.*)$")
_CALL = re.compile(r"\b(\w+)\s*\(")


def panicky_functions(source_root: Path) -> dict[str, str]:
    """Function names the source documents as able to panic, mapped to the file that says so."""
    found: dict[str, str] = {}
    for path in sorted(source_root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for i, line in enumerate(lines):
            match = _DEF.match(line)
            if match and "PANICS" in "\n".join(lines[i : i + 6]):
                found[match.group(1)] = str(path.relative_to(source_root))
    return found


def added_lines(diff: str):
    """Yield (path, new_line_number, added_text) for each added line of the numbered diff."""
    path = ""
    for line in diff.splitlines():
        if "+++ " in line:
            path = line.split("+++ ", 1)[1].strip().removeprefix("b/")
            continue
        match = _ADDED.match(line)
        if match and path:
            yield path, int(match.group(1)), match.group(2)


def main() -> int:
    payload = json.load(sys.stdin)
    source_root = Path(payload["context"]["source_root"])
    panicky = panicky_functions(source_root)

    findings = []
    for path, line_no, text in added_lines(payload.get("diff", "")):
        for name in _CALL.findall(text):
            if name in panicky:
                findings.append(
                    {
                        "path": path,
                        "line": line_no,
                        "severity": "warning",
                        "rule_id": "PG1",
                        "message": (
                            f"`{name}()` can panic — {panicky[name]} documents it PANICS. "
                            "Guard the result rather than let a normal failure abort the worker."
                        ),
                        "confidence": 0.9,
                    }
                )
                break  # one finding per line

    print(json.dumps({"findings": findings}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
