---
role: arch-review
status: confirmed
confirmed_at_tree: 3d90fe1
confirmed_by: run/2026-07-14/812
see: [payments]
---

- Excepts R1 (no direct database access outside the repository layer): this package is a batch
  job, not a request path. It reads whole settlement windows in one pass, and routing that through
  the per-row repository turned an hourly job into a four-hour one. Direct SQL here is deliberate
  and reviewed.
  <!-- src: HUB-47733#r505 @ a71ce02, adr: ADR-22 -->

The exception is for reads and for the reconciler's own tables. It does not extend to
`payments_ledger`, whose invariants are in `payments/.agents/context.md` and hold here too.
