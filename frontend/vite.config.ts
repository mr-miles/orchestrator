import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The controller serves the build from legion_orch/server/static/dist; in development `bun dev`
// proxies the API (and its event streams) to a controller on the default operator port.
const controller = process.env.LEGION_CONTROLLER ?? 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, './src') } },
  build: {
    outDir: path.resolve(import.meta.dirname, '../legion_orch/server/static/dist'),
    emptyOutDir: true,
    // one bundle for a local operator tool, loaded once and cached (hashed file names)
    chunkSizeWarningLimit: 1024,
  },
  server: { proxy: { '/api': { target: controller, changeOrigin: true } } },
})
