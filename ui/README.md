# Whetstone console — frontend

React + TypeScript + Vite, built into `../src/whetstone/ui/static/` and served by the Python app.

**Users never need Node.** Wheels ship the built assets; `pip install 'whetstone[ui]'` then
`whetstone ui` is the whole story. Node is a maintainer requirement only.

## Working on it

```bash
npm install
npm run gen:api      # regenerate TypeScript types from the live OpenAPI schema
npm run build        # typecheck + tests + build  ->  ../src/whetstone/ui/static/
npm test             # vitest
npm run typecheck    # types only
```

`parse.ts` is pure logic that everything downstream depends on — expectation regions and finding
locations are both expressed in the new-file line numbers it produces — so it carries unit tests.
It shipped with a bug (a phantom trailing line, selectable and off the end of the file) precisely
because it had none.

Live reload, two terminals:

```bash
whetstone ui --dev   # API on :8787, no browser
npm run dev          # Vite on :5173, proxying /api
```

## Types are generated, not written

`src/api/schema.d.ts` comes from the app's OpenAPI schema via `npm run gen:api`. Do not edit it.
Changing a pydantic model in `domain/` or `service.py` and rebuilding surfaces the mismatch as a
TypeScript error — which is the point. It has already caught one real bug: `SkillScore`'s metrics
were plain properties and never serialized, so every API consumer received raw confusion counts.

Run `gen:api` after any API change, and commit the regenerated `schema.d.ts`.

## Dependency advisories

`npm audit` reports advisories with no upgrade path available. Assessed:

| Advisory                                                                                       | Assessment                                                                                                                                                                                           |
| ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `react-router` RSC-mode CSRF (7.12.0–8.2.0)                                                    | **Not applicable.** No fixed 7.x release exists. The advisory concerns React Server Components mode and server actions; this is a static SPA with a Python API and no RSC or server-action pipeline. |
| `js-yaml`, `brace-expansion`, `minimatch` (via `@redocly/openapi-core` ← `openapi-typescript`) | **Not shipped.** Dev-only codegen, run by a maintainer against our own schema file. DoS-class issues against untrusted input; there is no untrusted input here.                                      |

Re-check when a fixed `react-router` 7.x ships. Do not add `npm audit` to CI as a blocking gate
until these clear, or it will fail on findings that do not apply.

## Conventions

- **Colour never carries meaning alone.** Outcome chips pair colour with the `tp`/`fn`/`fp`/`tn`
  label and a `title` explaining it.
- **No `dangerouslySetInnerHTML`.** `Guidance.tsx` renders SKILL.md as React nodes, so there is no
  sanitiser to keep correct.
- **Server state via TanStack Query**, local state via `useState`. There is no global store, and
  adding one is a signal that something belongs on the server instead.
