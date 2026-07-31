# What counts as a panic, and what counts as a guard

This page exists to be read *when the instructions send you here* — not to be pasted into every
prompt. That is the difference an agent skill makes: `SKILL.md` is a short instruction sheet, and
this is the reference it points at.

## A panic

A function can panic if its docstring in the source tree contains the marker `PANICS:`. That marker
is the convention this codebase uses to mean "this aborts the process rather than returning an
error". Nothing else counts:

- a function that raises a documented, catchable exception is **not** a panic;
- a function with no docstring is **not** a panic — you have no evidence, so you have no finding;
- a name that merely sounds dangerous (`force_`, `unsafe_`) is **not** a panic.

## A guard

A call is guarded when the change itself does something about the failure. Any of these:

- wrapping the call in `try` / `except`;
- assigning through a helper whose name begins `try_` or `safe_`;
- an explicit `if ... is None` check on the result before it is used.

A call is **unguarded** when the result is used directly, returned, or discarded.

## Severity

- `error` — the unguarded call is on a request path or a startup path.
- `warning` — anywhere else.

Report the line of the call in the NEW file, not the line of the function definition.
