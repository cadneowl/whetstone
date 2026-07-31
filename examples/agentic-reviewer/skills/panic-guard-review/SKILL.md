---
id: panic-guard-review
name: Panic-guard review
description: Flags changes that call functions the source documents as able to panic.
version: 1
triggers:
  paths: ["**/*.py"]
---

# Panic-guard review

Flag any change that calls a function the codebase documents as able to **panic** — its source
docstring says `PANICS` — without guarding the result.

Whether a call is dangerous depends on the *called* function, whose definition lives in the source
tree, not in the diff. So this skill's reviewer reads the source (`context.source_root`) to decide,
rather than guessing from the change alone. See the example's `README.md` for how that works.
