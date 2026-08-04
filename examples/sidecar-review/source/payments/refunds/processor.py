"""Refunds own the approval workflow. They do not own the money movement."""


class RefundProcessor:
    def __init__(self, ledger, approvals):
        self._ledger = ledger
        self._approvals = approvals

    def approve(self, order_id, amount_cents, approver):
        """Record the decision. Settlement is a separate step."""
        self._approvals.insert(
            order_id=order_id,
            amount_cents=amount_cents,
            approver=approver,
        )
