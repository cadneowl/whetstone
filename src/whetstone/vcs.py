from __future__ import annotations

import io
import subprocess
import tarfile
import tempfile
from pathlib import Path


def export_tree(repo: str | Path, ref: str, path: str) -> Path:
    """Export `path` from a git repo at `ref` into a fresh temp directory and return that directory.

    The requested `path` is reproduced under the returned root (e.g. the skill folder lands at
    ``<root>/skills/<id>/``). Uses ``git archive`` piped through Python's ``tarfile`` so no external
    ``tar`` binary is needed and nothing in the working tree is touched. The caller owns cleanup.

    Raises ``subprocess.CalledProcessError`` if the ref or path is unknown to git.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", ref, path],
        capture_output=True,
        check=True,
    )
    root = Path(tempfile.mkdtemp(prefix="whetstone-vcs-"))
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tf:
        tf.extractall(root, filter="data")
    return root
