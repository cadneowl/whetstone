---
role: arch-review
status: confirmed
confirmed_at_tree: 9f2c1ab
confirmed_by: run/2026-07-14/812
---

- Treat a ledger write from outside `PaymentService` as an error, not a style note. The fix is
  always the same shape: call `PaymentService.record()` with an idempotency key and let it own the
  insert.
  <!-- src: HUB-48163#r527 @ 3d90fe1, adr: ADR-22 -->
