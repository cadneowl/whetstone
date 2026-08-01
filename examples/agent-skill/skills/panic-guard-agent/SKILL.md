---
id: panic-guard-agent
name: Panic guard (agent)
description: Flags calls to functions that can panic when the caller does not guard the result.
version: 1
---

# Panic guard

A change that calls a function which can abort the process, without guarding the result, is a
production incident waiting for a bad deploy. Your job is to find those calls.

**You cannot tell from the diff alone.** Whether `load_config()` can panic is a fact about
`load_config`, not about the line that calls it — and that line is all the diff shows you. So
investigate before you answer:

1. Read **[references/panics.md](references/panics.md)** first. It defines what counts as a panic
   here and what counts as a guard. Do not guess at either.
2. For every function the change calls, `grep` the source tree for its definition and read the
   docstring. A function documented `PANICS:` can abort; one that is not, cannot.
3. When you flag something, call `owner_of` with the module path so the finding names the team who
   will pick it up.

Report only calls that are **both** panicky and unguarded. A guarded call to a panicky function is
correct code and flagging it trains people to ignore you.

If a function is not in the source tree at all, say nothing about it — you have no evidence either
way, and a finding you cannot support is worse than a finding you did not make.
