import { defineConfig } from 'vite'
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
})
