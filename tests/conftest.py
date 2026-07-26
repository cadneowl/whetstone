"""Suite-wide isolation.

The rest of the tree is hermetic by construction — fakes for the LLM, cassettes for HTTP, tmp_path
for everything on disk. `.env` support punches a hole in that: discovery walks up from the working
directory, which during a test run is the developer's own checkout. A `.env` sitting there with a
`WHETSTONE_RUNS_DIR` or an `ANTHROPIC_API_KEY` would quietly change what the suite exercises, and
worse, would do it on one machine and not another.

So discovery is switched off for every test except the ones that exist to exercise it, which opt
back in with `@pytest.mark.uses_dotenv`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from whetstone.envfile import ENV_FILE_VAR


@pytest.fixture(autouse=True)
def _isolate_dotenv(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    # Snapshot the whole environment rather than naming variables. `load_env_file` assigns through
    # `os.environ.setdefault`, and `monkeypatch.delenv(name, raising=False)` records nothing when
    # the name is absent — so a variable a test *creates* that way is never undone. Without this,
    # a fake GITLAB_TOKEN from the .env tests survives into every file that runs after them.
    saved = os.environ.copy()
    try:
        if "uses_dotenv" not in request.keywords:
            monkeypatch.delenv(ENV_FILE_VAR, raising=False)
            monkeypatch.setattr("whetstone.envfile.find_env_file", lambda start=None: None)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
