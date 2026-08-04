"""Hourly settlement. A batch job, not a request path."""


class ReconciliationJob:
    def __init__(self, conn, ledger):
        self._conn = conn
        self._ledger = ledger

    def run(self, window_start, window_end):
        rows = self._conn.execute(
            "SELECT id, order_id, amount_cents FROM reconciliation_window "
            "WHERE opened_at >= ? AND opened_at < ? ORDER BY id",
            (window_start, window_end),
        ).fetchall()
        return [self._settle(row) for row in rows]

    def _settle(self, row):
        return {"id": row[0], "order_id": row[1], "amount_cents": row[2]}
