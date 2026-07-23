---
id: code-review-rust-error-handling
name: Rust error handling review
description: Flags panics/unwraps and swallowed errors in non-test service code.
version: 1
triggers:
  paths: ["**/*.rs"]
  labels: ["backend"]
---

# Rust error handling review

Guidance the reviewer applies to Rust changes.

- **R1 — no unchecked panics in service code.** `.unwrap()` / `.expect()` outside test modules
  must be replaced with `?` and a mapped error, or justified in a comment. Panicking on a normal
  error path takes the process down.
- **R2 — no swallowed errors.** An error that is caught and discarded without logging or
  propagating hides failures. Propagate with `?` or log-and-handle explicitly.

R1 does **not** apply inside test code (`#[cfg(test)]` modules, `*_test.rs`, `tests/`), where
`unwrap()` is idiomatic.
