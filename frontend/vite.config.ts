import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // host: true 同时监听 0.0.0.0 与 ::。默认只绑 localhost(::1) 时，
    // 用 127.0.0.1:5173 会连不上，手机同局域网也访问不到（真机联调要用）。
    host: true,
    // 同源代理（开发说明 §11：优先同源代理，Cookie 自动携带，无 CORS 烦恼）。
    // 生产部署用 Nginx 等把 /api 反代到后端，前端代码无需任何修改。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
})
