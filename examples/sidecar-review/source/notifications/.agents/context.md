---
status: confirmed
confirmed_at_tree: 71b0c4d
confirmed_by: run/2026-06-30/104
---

`notifications/` is best-effort by design. A push that does not arrive is a worse outcome than a
checkout that fails because a push did not arrive, so delivery failures are counted and dropped.

- Excepts R3 (no swallowed exceptions) for transport failures in the send path. `_dropped` is the
  intended handling: the counter is scraped and alerts on rate, and re-raising would put the
  notification bus in the caller's failure domain.
  <!-- src: HUB-44120#r208 @ 71b0c4d, adr: ADR-19 -->

The exception is transport failures only. A programming error must still surface.
