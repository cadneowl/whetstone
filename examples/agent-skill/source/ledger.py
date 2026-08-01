"""The 'source tree' the agent searches. Whetstone never puts this in a prompt.

Whether a call is dangerous is a fact about the *callee*, and the callee is not in the diff. The
agent has to go and find it — `grep` for the function, read the docstring, decide. That is the whole
reason a skill runs as an agent rather than as one prompt: the judgement depends on code outside the
change, and no amount of prompt stuffing puts an unknown-in-advance file in front of the model.
"""


def load_config():
    """Read the service config from disk.

    PANICS: aborts when the config file is missing, which is a normal condition on a fresh deploy,
    so a caller must guard the result rather than let it take the worker down.
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


def balance_of(account):
    """Return an account balance.

    Raises ``LookupError`` for an unknown account. That is a catchable, documented error and not a
    panic — the distinction references/panics.md exists to make.
    """
    raise LookupError(account)
