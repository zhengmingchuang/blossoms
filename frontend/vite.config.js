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

  build: {
    // element-plus 走全量引入（换取主题变量与全部组件的稳定行为），
    // 单独成 chunk 后约 285 kB gzip，因此把告警阈值放宽到 1000 kB；
    // 业务代码本身仍在 30 kB 量级，首屏不背这个包。
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        /**
         * 手动分包：把体积大且很少变动的第三方库拆成独立 chunk。
         * 好处是发版时业务代码一变，vendor chunk 的 hash 不变，浏览器仍能命中强缓存。
         */
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (id.includes('element-plus') || id.includes('@element-plus')) return 'vendor-element'
          if (id.includes('echarts') || id.includes('zrender')) return 'vendor-charts'
          if (id.includes('/vue/') || id.includes('@vue/') || id.includes('vue-router')) return 'vendor-vue'
          return 'vendor'
        },
      },
    },
  },

  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        configure: proxy => {
          // 强制不走系统代理
          proxy.on('proxyReq', proxyReq => {
            delete proxyReq.agent
          })
        },
      },
    },
  },
})
