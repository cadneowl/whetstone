r"""`.env` loading.

Whetstone's secrets are all environment variables — `ANTHROPIC_API_KEY`, `GITLAB_TOKEN`,
`JIRA_TOKEN` — and none of them belong in `whetstone.toml`, which is committed. A `.env` file is
where they go instead, so this is what reads it.

**A real environment variable always wins.** `.env` fills in what the environment has not already
said, never the other way round. That is what lets CI inject a token without editing a file, and it
means `GITLAB_TOKEN=... whetstone corpus pull` does what it looks like it does even with a `.env`
sitting in the directory. The full order is:

    CLI flag  →  real environment  →  .env  →  whetstone.toml  →  built-in default

`.env` sits at the environment tier rather than the file tier, because its contents *are*
environment variables: `WHETSTONE_UI_PORT` written there behaves exactly as if it were exported.

Discovery walks upward from the working directory, matching `whetstone.toml`, so running from a
subdirectory behaves the same as running from the repo root.

**Values are literal.** dotenv's `${NAME}` substitution is switched off, because this file holds
credentials and that substitution has no escape: `\${NAME}` still expands (and keeps the backslash),
and quoting does not stop it either. A token containing `${` would be silently rewritten with no way
to prevent it, which is the same shape of bug as the byte-order mark below and worse in consequence.
Turning it off can only produce a *visible* surprise — a value that reads `${HOST}` because that is
what was written — and anyone who wants composition can write the value out in full.

Named `envfile` rather than `dotenv` so it cannot shadow the library it imports. The shadowing is
harmless under absolute imports, but `from dotenv import …` inside a module called `dotenv` is a
trap for the next reader, and does break for anyone whose `sys.path` includes this directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_ENV_FILENAME = ".env"

# Set by `--env-file` so that later `load_config()` calls resolve the same file. Passing the choice
# through the environment rather than a module global keeps it visible to subprocesses and to code
# that never sees the CLI, and leaves nothing to reset between tests.
ENV_FILE_VAR = "WHETSTONE_ENV_FILE"


def find_env_file(start: str | Path | None = None) -> Path | None:
    """Nearest `.env` at or above `start` (default: CWD)."""
    current = Path(start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / DEFAULT_ENV_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_env_file(
    path: str | Path | None = None, *, start: str | Path | None = None
) -> Path | None:
    """Load a `.env` into `os.environ`, returning the file used or None if there wasn't one.

    Resolution is explicit argument → `WHETSTONE_ENV_FILE` → discovery. Naming a file that does not
    exist raises: an operator who points at `staging.env` and gets silence would go looking for the
    bug in their credentials.

    Safe to call repeatedly — nothing already set is touched, so the second call is a no-op.
    """
    source = _resolve(path, start)
    if source is None:
        return None

    # `utf-8-sig`, not `utf-8`: Notepad, VS Code on Windows, and PowerShell's `Set-Content -Encoding
    # utf8` all write a byte-order mark, and plain utf-8 decoding turns the first key into
    # "\ufeffANTHROPIC_API_KEY" — dropping exactly one variable, always the first, with no error at
    # all. About the least debuggable failure this file could have. Decoding is otherwise identical.
    for key, value in dotenv_values(source, encoding="utf-8-sig", interpolate=False).items():
        # `dotenv_values` yields None for a bare `KEY` with no `=`. Setting that to "" would look
        # like a deliberate empty value, which some settings read as meaningful.
        if value is not None:
            os.environ.setdefault(key, value)
    return source


def _resolve(path: str | Path | None, start: str | Path | None) -> Path | None:
    explicit = path if path is not None else os.environ.get(ENV_FILE_VAR) or None
    if explicit is None:
        return find_env_file(start)
    resolved = Path(explicit).expanduser()
    if not resolved.is_file():
        raise FileNotFoundError(f"env file {resolved} does not exist")
    return resolved
