import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

const containerDevelopment = process.env.BYOF_API_PROXY === 'http://api:8000'
if (process.env.BYOF_API_PROXY && !containerDevelopment) {
  throw new Error('BYOF_API_PROXY must be the local Compose API service')
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  cacheDir: containerDevelopment ? '/tmp/byof-vite' : 'node_modules/.vite',
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: containerDevelopment ? 'http://api:8000' : 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
      '/health/ready': {
        target: containerDevelopment ? 'http://api:8000' : 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
    watch: { usePolling: containerDevelopment },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    clearMocks: true,
    restoreMocks: true,
  },
})
