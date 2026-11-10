// Панель стратега: сборка — прямо в пакет Python (src/strategist/interfaces/web/static), её отдаёт `strategist ui`.
// В разработке `npm run dev` проксирует /api на запущенный `uv run strategist ui` (порт 8501).
import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: {
    outDir: path.resolve(__dirname, "../src/strategist/interfaces/web/static"),
    emptyOutDir: true,
    chunkSizeWarningLimit: 900, // одна страница панели, грузится один раз с локального сервера
  },
  server: { proxy: { "/api": "http://127.0.0.1:8501" } },
})
