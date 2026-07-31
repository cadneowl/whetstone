# Conventions the reviewer may consult

A committed reference, passed to the reviewer as the `conventions` context value (the `file` form).
It is hashed by content, so editing it is a change to what the reviewer is given.

- A function that can abort the process documents it with `PANICS:` in its docstring.
- Callers must guard a `PANICS` function's result rather than let a routine failure crash the worker.
