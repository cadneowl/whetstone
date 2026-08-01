"""A tool the skill brings with it — the whole contract, in thirty lines.

Whetstone offers this to the model as `owner_of` and runs it when the model asks. It knows nothing
about what an owner is; that is the point. The same shape reaches Jira, an internal search, or a
schema registry, and a credential for any of them arrives through `context` rather than being
committed here.

stdin:  {"arguments": {"module": "ledger.py"}, "context": {"owners": "{…}"}}
stdout: whatever the model should see

Exiting non-zero is not fatal: stderr goes back to the model as an error result, so an agent told
"no such module" tries something else instead of losing the case.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    module = str(payload.get("arguments", {}).get("module", "")).strip().lstrip("./")
    if not module:
        print("owner_of needs a 'module' path", file=sys.stderr)
        return 1

    # `context.owners` is the *contents* of owners.json, loaded by the `file:` directive in
    # evaluate/step.yaml — the tool never opens the file itself, so what it reads is exactly what
    # the run record hashed.
    owners = json.loads(payload.get("context", {}).get("owners") or "{}")
    owner = owners.get(module)
    if owner is None:
        known = ", ".join(sorted(owners)) or "(none)"
        print(f"no owner recorded for {module!r}. Known modules: {known}", file=sys.stderr)
        return 1
    print(owner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
