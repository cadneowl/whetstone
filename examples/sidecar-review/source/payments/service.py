"""Money movement. Nothing outside this module puts a row in the ledger."""


class PaymentService:
    def __init__(self, ledger):
        self._ledger = ledger

    def record(self, order_id, amount_cents, kind, idempotency_key):
        """Append one ledger row, once, for this idempotency key."""
        if self._ledger.seen(idempotency_key):
            return
        self._ledger.insert(
            order_id=order_id,
            amount_cents=amount_cents,
            kind=kind,
            idempotency_key=idempotency_key,
        )
