---
status: confirmed
confirmed_at_tree: 9f2c1ab
confirmed_by: run/2026-07-14/812
---

`payments/` owns money movement. Every balance change lands in `payments_ledger`, which the
reconciler settles hourly by reading rows in insertion order.

- `PaymentService.record()` is the only writer to `payments_ledger`. Anything else that needs a
  ledger row calls it. A write that goes around it skips the idempotency check, and the retry that
  follows a timeout books the amount twice.
  <!-- src: HUB-48163#r527 @ 3d90fe1 -->

- The ledger is append-only. A correction is a new compensating row, never an edit to an existing
  one — the reconciler has already settled everything before the current hour, and an in-place
  edit changes a day that was closed without leaving a trace that it moved.
  <!-- src: HUB-46002#r318 @ 71b0c4d, adr: ADR-22 -->
