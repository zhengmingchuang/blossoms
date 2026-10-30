import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 绕过系统代理，确保 Vite proxy 直连后端
process.env.NO_PROXY = 'localhost,127.0.0.1'
process.env.no_proxy = 'localhost,127.0.0.1'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8800',
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
