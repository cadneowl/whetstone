"""The one cancellation exception, defined where everything can reach it.

Cancelling is a control-flow signal, not a failure, and every layer has to agree on that or the
operator is told their run crashed when they stopped it themselves. `RunCancelled` lived in
`core.harness`, which the agent loop cannot import without a cycle — so the agent raised its own
unrelated exception, fell past the console's `except RunCancelled`, and landed in the catch-all that
marks a job *failed*.

This module imports nothing, so both sides can subclass one base. `core.harness` re-exports it, and
every existing `from whetstone.core.harness import RunCancelled` keeps working unchanged.
"""

from __future__ import annotations


class RunCancelled(RuntimeError):
    """Raised when a run is stopped through its cancel event."""
