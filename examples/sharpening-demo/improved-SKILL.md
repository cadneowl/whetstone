---
id: demo-rust-errors
name: Rust error handling review
description: Flags panics and swallowed errors in non-test Rust service code.
version: 2
triggers:
  paths: ["**/*.rs"]
---

# Rust error handling review

Guidance the reviewer applies to Rust changes.

- **R1 — no panicking unwraps in service code.** `.unwrap()` **and `.expect()`** both abort the
  process when the value is absent. `.expect()` is not safer for carrying a message — it panics
  identically. Replace either with `?` and a mapped error.
- **R2 — no swallowed errors.** `let _ = f();` on a function returning `Result` discards the error
  silently, so a failure leaves no trace anywhere. Propagate it with `?`, or handle and log it
  explicitly.

R1 and R2 do **not** apply to test code — inside `#[test]` functions, `#[cfg(test)]` modules,
`*_test.rs` files or `tests/` directories, `.unwrap()` is idiomatic and must not be flagged.
