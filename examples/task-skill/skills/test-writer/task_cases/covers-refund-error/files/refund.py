class RefundTooLarge(ValueError):
    """Raised when a refund exceeds the original charge."""


def refund(charge_cents: int, amount_cents: int) -> int:
    """Return the remaining balance after refunding `amount_cents`."""
    if amount_cents > charge_cents:
        raise RefundTooLarge(f"cannot refund {amount_cents} of a {charge_cents} charge")
    return charge_cents - amount_cents
