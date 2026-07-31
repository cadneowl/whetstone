"""A stand-in for a large service library — the 'source' the reviewer reads, not the diff.

Whetstone hands a reviewer the change under review, but never this file. The example reviewer opens
it from ``context.source_root`` to learn which functions are documented as able to panic — knowledge
that lives here, in the source, and appears nowhere in the diff. That is the whole point of a
source-aware reviewer: the judgement depends on code outside the change.
"""


def load_config():
    """Read the service config from disk.

    PANICS: raises if the config file is missing, which is a normal condition on a fresh deploy,
    so a caller must guard the result rather than let it abort the worker.
    """
    raise NotImplementedError


def open_ledger():
    """Open the ledger database.

    PANICS: aborts the process when the ledger is already locked by another writer.
    """
    raise NotImplementedError


def safe_get(key):
    """Return the value for ``key`` or ``None``. Never raises — safe to call unguarded."""
    return None
