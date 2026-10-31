import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 绕过系统代理，确保 Vite proxy 直连后端
process.env.NO_PROXY = 'localhost,127.0.0.1'
process.env.no_proxy = 'localhost,127.0.0.1'

// 后端端口需与 backend/main.py 的 uvicorn 端口一致（默认 8000）。
// 如改了后端端口，用 VITE_API_TARGET 覆盖，例如：
//   VITE_API_TARGET=http://127.0.0.1:9000 npm run dev
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        configure: (proxy) => {
          // 强制不走系统代理
          proxy.on('proxyReq', (proxyReq) => {
            delete proxyReq.agent
          })
        },
      },
    },
  },
})
