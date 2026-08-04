---
id: hub-arch-review
name: Hub architecture review
description: Flags layering, retry and error-handling violations in the hub service.
version: 1
triggers:
  paths: ["**/*.py"]
sidecar:
  # The `.agents/<role>.md` file this skill reads from the folders a change touches. The id comes
  # from here and never from the skill's folder name, so forking this skill into `hub-arch-review-v2`
  # does not mean renaming sidecars across the monorepo.
  role: arch-review
---

# Hub architecture review

General architecture rules for the hub service. They hold everywhere; the particulars that hold in
*one* folder live beside that folder's code in `.agents/arch-review.md`, not here.

- **R1 — no direct database access outside the repository layer.** Handlers, services and jobs go
  through a repository. Inline SQL elsewhere couples the schema to its caller and moves transaction
  management somewhere nobody looks for it.
- **R2 — every call to an external service retries a bounded number of times.** A retry loop with
  no ceiling turns a slow dependency into an outage. The cap must be a named constant.
- **R3 — no swallowed exceptions.** An exception that is caught and neither logged nor re-raised
  hides a failure that someone is on call for.
