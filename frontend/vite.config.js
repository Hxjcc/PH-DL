import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'vue-vendor': ['vue', 'vue-router', 'pinia'],
          'ui-primitives': ['reka-ui', 'vue-sonner', 'lucide-vue-next'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api/ws': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReqWs', (proxyReq) => {
            proxyReq.setHeader('origin', 'http://127.0.0.1:8000')
          })
        },
      },
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
})
