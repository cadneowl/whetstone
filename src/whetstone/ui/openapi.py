"""Dump the console's OpenAPI schema.

The frontend's TypeScript types are generated from this, so a change to a pydantic model in
`domain/` or `service.py` surfaces as a compile error in the UI rather than a runtime surprise.

    python -m whetstone.ui.openapi ui/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from whetstone.config import Config
from whetstone.ui.app import create_app


def schema() -> dict[str, Any]:
    """The schema, built from a default-configured app — no repo or run store is touched."""
    openapi: dict[str, Any] = create_app(Config()).openapi()
    return openapi


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    text = json.dumps(schema(), indent=2, sort_keys=True)
    if not args:
        sys.stdout.write(text)
        return 0
    out = Path(args[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    sys.stderr.write(f"wrote {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
