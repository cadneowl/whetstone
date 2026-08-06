"""Retry scheduling for outbound payment calls."""

MAX_ATTEMPTS = 5


class BudgetExhausted(RuntimeError):
    """Raised when a caller asks for a delay past the retry budget."""


def next_delay_ms(attempt: int, *, base_ms: int = 100, cap_ms: int = 500) -> int:
    """How long to wait before `attempt`, doubling each time up to a ceiling.

    Attempts are 1-based: the first retry is attempt 1 and waits `base_ms`. Doubling stops at
    `cap_ms`, which with the defaults means the last two attempts in the budget wait the same.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be 1 or more, got {attempt}")
    if attempt > MAX_ATTEMPTS:
        raise BudgetExhausted(f"attempt {attempt} exceeds the budget of {MAX_ATTEMPTS}")
    return min(base_ms * 2 ** (attempt - 1), cap_ms)
