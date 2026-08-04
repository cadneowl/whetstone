---
role: arch-review
status: confirmed
confirmed_at_tree: a71ce02
confirmed_by: run/2026-07-14/812
---

- Retries against the card processor cap at **3**. The upstream rate-limits at 4 attempts per
  authorization and answers the 4th with a 30-minute block on the whole merchant account, so a
  higher cap does not degrade this request — it takes every other request down with it.
  <!-- src: HUB-45814#r411 @ 9f2c1ab -->

## stripe.py

- `MAX_RETRIES` is the cap that number refers to. Raising it needs the processor's rate limit
  raised first, which is a contract change and goes through the payments guild.
  <!-- src: HUB-45814#r411 @ 9f2c1ab -->
