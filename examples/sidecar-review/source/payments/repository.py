"""The only module in `payments/` that speaks SQL."""


class LedgerRepository:
    def __init__(self, conn):
        self._conn = conn

    def seen(self, idempotency_key):
        row = self._conn.execute(
            "SELECT 1 FROM payments_ledger WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def insert(self, *, order_id, amount_cents, kind, idempotency_key):
        self._conn.execute(
            "INSERT INTO payments_ledger (order_id, amount_cents, kind, idempotency_key) "
            "VALUES (?, ?, ?, ?)",
            (order_id, amount_cents, kind, idempotency_key),
        )
