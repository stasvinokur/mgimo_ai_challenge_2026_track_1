.PHONY: setup check lint format test arch secrets live demo demo-check demo-update web ui-check

setup:            ## установить зависимости и git-хуки
	uv sync
	uv run pre-commit install

check: lint arch secrets test   ## всё, что должно быть зелёным перед коммитом

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

arch:             ## контракты слоёв чистой архитектуры
	uv run lint-imports

secrets:          ## сканер секретов по файлам репозитория
	uv run python scripts/check_secrets.py

test:             ## unit + интеграционные тесты без сети и моделей
	uv run pytest

live:             ## тесты с реальными моделями Ollama (нужен демон и ollama signin)
	uv run pytest -m live

demo:             ## сквозной прогон демо-кейса в режиме воспроизведения записанных ответов
	uv run strategist demo --replay

demo-check:       ## воспроизведение во временный каталог: падает на промахе кассеты и расхождении отчёта
	tmp=$$(mktemp -d) && uv run strategist demo --replay --check --work-dir "$$tmp"; rc=$$?; rm -rf "$$tmp"; exit $$rc

demo-update:      ## дозапись кассет после правки шага: модель зовётся только для новых запросов
	uv run strategist demo --update

# Панель на shadcn: пересобрать страницу в src/strategist/interfaces/web/static (сборка коммитится)
web:
	cd web && npm ci && npx tsc -b && npm run lint && npm run build

# Панель в браузере: демо по записанным ответам — от «Открыть демо» до пересмотра (Playwright, без моделей)
ui-check:
	uv run --with playwright==1.64.1 python -m playwright install chromium-headless-shell
	uv run --with playwright==1.64.1 pytest -m browser tests/e2e/test_web_ui.py
