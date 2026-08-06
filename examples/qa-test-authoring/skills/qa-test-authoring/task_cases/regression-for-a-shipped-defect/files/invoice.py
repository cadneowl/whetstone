"""Splitting an invoice across payers."""


def split_evenly(total_cents: int, ways: int) -> list[int]:
    """Split `total_cents` into `ways` parts that sum to exactly `total_cents`.

    Parts differ by at most one cent, and the larger parts come first.
    """
    if ways < 1:
        raise ValueError(f"ways must be 1 or more, got {ways}")
    base, remainder = divmod(total_cents, ways)
    return [base + (1 if i < remainder else 0) for i in range(ways)]
