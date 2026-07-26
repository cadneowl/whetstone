---
id: demo-rust-errors
name: Rust error handling review
description: Flags panics in Rust service code.
version: 1
triggers:
  paths: ["**/*.rs"]
---

# Rust error handling review

Guidance the reviewer applies to Rust changes.

- **R1 — no `.unwrap()` in service code.** Calling `.unwrap()` on a `Result` or `Option` aborts the
  process when the value is absent. Replace it with `?` and a mapped error.
