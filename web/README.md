# Панель стратега (React + shadcn/ui)

Минималистичная панель руководителя: вкладки «Решение», «Факты», «Отчёт». Данные — JSON API
`src/strategist/interfaces/web/api.py` (FastAPI), ход работы — события SSE.

Собранная страница лежит в пакете (`src/strategist/interfaces/web/static`), поэтому для запуска Node не нужен:

```bash
uv run strategist ui          # http://127.0.0.1:8501
uv run strategist ui --demo   # демо по записанным ответам моделей, без моделей
```

Разработка (Node 26):

```bash
cd web && npm ci
npm run dev        # http://localhost:5173, /api проксируется на запущенный `uv run strategist ui`
npm run build      # пересобрать static/ — сборка коммитится вместе с изменениями
npx tsc -b && npm run lint
```

Компоненты — shadcn/ui (пресет Nova на Radix, `components.json`), шрифт — Onest (кириллица).
