import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/screenshots': 'http://localhost:8000',
      '/recordings': 'http://localhost:8000',
      // 런 폴더 내 파일(실제 스크린샷, 회차별 녹화). main.py의 /results-files 마운트.
      // 이게 빠져 있으면 dev(5173)에서 SPA catch-all이 index.html을 돌려줘
      // 실제 이미지는 깨진 아이콘, 녹화는 로드 실패로 보인다.
      '/results-files': 'http://localhost:8000',
      '/docs': 'http://localhost:8000',
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
})
