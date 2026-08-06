// `vitest/config`, not `vite`, so the `test` block below typechecks — it is the same
// `defineConfig` with vitest's options folded in.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

// The build output lands inside the Python package so wheels ship a console that runs without Node.
// `src/whetstone/ui/static/` is gitignored and force-included by hatchling at packaging time.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: '../src/whetstone/ui/static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // `whetstone ui --dev` runs the API on 8787 and points a browser here.
    proxy: { '/api': 'http://127.0.0.1:8787' },
  },
  test: {
    // Node by default, and deliberately: almost every test here is a pure module — a filter, a
    // summary, a selection — and standing a DOM up for those costs more than running them. The few
    // that genuinely need one (a hook that invalidates queries, a component with state) opt in with
    // `// @vitest-environment jsdom` on the first line, so only they pay for it.
    environment: 'node',
  },
})
