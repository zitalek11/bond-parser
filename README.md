# Bond Emission Parser

Сервис для автоматического парсинга и извлечения параметров выпуска из эмиссионной документации по облигациям.

## Назначение и цели

Цель сервиса — превратить PDF/DOCX эмиссионных документов в структурированные данные по выпуску (купоны, амортизация, даты, номинал, тип дохода и др.), автоматически проверить качество извлечения и при необходимости отправить кейс на ручную проверку (HITL).

Что делает сервис:

- принимает эмиссионную документацию (загрузка файла или сбор из доступных источников);
- извлекает текст и таблицы из PDF/DOCX;
- парсит параметры выпуска через локальный или облачный LLM (Qwen) со Structured Output;
- прогоняет Data Quality Gate (бизнес-правила и антигаллюцинации);
- отдаёт оператору HITL-очередь и FastAPI UI для ручной проверки сложных кейсов;
- экспортирует подтверждённые выпуски в XLSX/CSV.

Целевой ориентир качества — **99.9%** за счёт связки автоматических проверок и обязательного Human-in-the-Loop для флоатеров, неоднозначных формулировок и подозрений на галлюцинации модели.

## Пайплайн

1. **Collector** — сбор документов (RSS/источники + ручная загрузка)
2. **Extractor** — PDF/DOCX → Markdown (Docling или PyMuPDF/pdfplumber)
3. **LLM Parser** — Structured Output через OpenAI-compatible API + `instructor`
4. **Data Quality Gate** — жёсткие бизнес-правила → `APPROVED` или `NEEDS_REVIEW`
5. **HITL** — split-screen веб-интерфейс оператора (Jinja2 + HTMX + Tailwind)

## Стек

| Слой | Технологии |
|------|------------|
| API / UI | FastAPI, Jinja2, HTMX, TailwindCDN |
| ORM | SQLAlchemy 2 async |
| БД | SQLite (`aiosqlite`) → PostgreSQL (`asyncpg`) одной строкой `.env` |
| LLM | **Qwen 2.5** локально (Ollama / vLLM) или облачно (OpenRouter) + instructor `MD_JSON` |
| OCR/parse | pymupdf + pdfplumber (+ optional docling), stitch таблиц по страницам |
| Scheduler | APScheduler |

## Для разработчика контура (клонирование и запуск)

1. Клонируйте репозиторий и перейдите в корень сервиса.
2. Скопируйте окружение: `cp .env.example .env` — **реальные ключи только в `.env`**, файл в git не коммитится.
3. Для облачного Qwen через OpenRouter в `.env` задайте:
   ```env
   LLM_PROVIDER=openrouter
   LLM_BASE_URL=https://openrouter.ai/api/v1
   LLM_MODEL_NAME=qwen/qwen-2.5-72b-instruct
   LLM_API_KEY=REPLACE_WITH_OPENROUTER_KEY
   ```
   Для on-prem оставьте `LLM_PROVIDER=ollama` / `vllm` и `LLM_API_KEY=not-needed`.
4. Локально: `python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`, затем `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`.
5. Либо Docker: `docker compose up -d --build` (PostgreSQL + Ollama; GPU — через `docker-compose.gpu.yml`).
6. UI: http://localhost:8000 — дашборд `/`, очередь HITL `/queue`, API `/docs`. Отдельного логина в MVP нет.

**Важно:** живой RSS/сайт источника может отдавать CAPTCHA. Для контура по умолчанию `RSS_USE_MOCK=true`; боевой сбор включайте осознанно (`RSS_USE_MOCK=false`) и учитывайте антибот-ограничения источника.

## Быстрый старт (локально)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# OpenRouter: LLM_PROVIDER=openrouter + LLM_API_KEY=...
# Или Ollama: ollama pull qwen2.5:32b
# Или офлайн: LLM_USE_MOCK=true

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Откройте http://localhost:8000

- Дашборд: `/`  
- Очередь HITL: `/queue`  
- OpenAPI: `/docs`  

## Docker Compose (app + PostgreSQL 16 + Ollama/Qwen 2.5)

```bash
cp .env.example .env
docker compose up -d --build
# с NVIDIA GPU:
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

`ollama-init` автоматически делает `pull qwen2.5:32b` при первом запуске.  
Контекст Ollama: `OLLAMA_CONTEXT_LENGTH=32768`.

### Демо-документ

```bash
curl -F "file=@samples/demo_emission.md" http://localhost:8000/api/tasks/upload
```

или загрузите файл кнопкой на дашборде.

## Переключение SQLite → PostgreSQL

В `.env` достаточно заменить одну переменную:

```env
# MVP
DATABASE_URL=sqlite+aiosqlite:///./storage/bond_parser.db

# Production
DATABASE_URL=postgresql+asyncpg://bond:bond@localhost:5432/bond_parser
```

Остальной код менять не нужно.

## LLM: Qwen 2.5 (локально или облачно)

```env
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL_NAME=qwen2.5:32b
LLM_API_KEY=not-needed
LLM_TEMPERATURE=0.05
LLM_MAX_TOKENS=8192
LLM_CONTEXT_WINDOW=32768
LLM_REPEAT_PENALTY=1.05
LLM_INSTRUCTOR_MODE=MD_JSON
```

| Провайдер | BASE_URL | MODEL |
|-----------|----------|-------|
| Ollama | `http://localhost:11434/v1` | `qwen2.5:32b` (или `14b`) |
| vLLM | `http://localhost:8000/v1` | `Qwen/Qwen2.5-32B-Instruct-AWQ` |
| OpenRouter | `https://openrouter.ai/api/v1` | `qwen/qwen-2.5-72b-instruct` |

Особенности Qwen 2.5 в коде:
- instructor mode **`MD_JSON`** (стабильнее TOOLS на вложенных схемах);
- `extra_body.options.num_ctx` / `repeat_penalty` для Ollama;
- `max_retries=3` — самоисправление по ошибкам Pydantic;
- перед LLM — `extract_relevant_sections()` (купоны / погашение / формула дохода).

## Опциональный Docling

```bash
pip install -r requirements-docling.txt
# .env
EXTRACTOR_BACKEND=docling   # или auto
```

## Data Quality Gate

Автоматический `APPROVED` только если нет ошибок:

- контрольная сумма ИНН (10/12)
- `maturity_date > placement_start_date`
- купоны последовательны без разрывов (допуск 3 дня)
- сумма амортизации = 100 ± 0.01
- `FLOATER` / `OTHER` → всегда HITL
- кросс-проверка ключевых полей с внешним справочником (если доступен)
- цитаты подтверждают ключевые поля (anti-hallucination)

## REST API (кратко)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/health` | liveness |
| GET | `/api/stats` | метрики дашборда |
| GET | `/api/tasks?status=NEEDS_REVIEW` | список задач |
| POST | `/api/tasks/upload` | загрузка файла |
| POST | `/api/collect` | ручной цикл сбора |
| POST | `/api/tasks/{id}/approve` | подтверждение HITL |
| POST | `/api/tasks/{id}/reject` | отклонение |
| GET | `/api/bonds` | golden records |

## Тесты и Golden Dataset (качество Qwen 2.5)

```bash
pytest -v

# Self-check фреймворка (expected vs expected → 100%)
python -m tests.golden_dataset.run_benchmark --self-check

# Регрессия против локальной Qwen 2.5 (нужен Ollama)
python -m tests.golden_dataset.run_benchmark --min-accuracy 80

# Офлайн-smoke на MockLLM
python -m tests.golden_dataset.run_benchmark --mock
```

Кейсы: `tests/golden_dataset/cases/` (FIXED+амортизация, CBR/RUONIA флоатеры, multipage tables).

## Экспорт APPROVED выпусков

- `GET /api/export/xlsx?only_new=true` — новые (ещё не выгружались), ставит `exported_at`
- `GET /api/export/xlsx` — вся база (4 листа: Issues / Coupons / Amortization / Floaters)
- `GET /api/export/csv` — плоский реестр UTF-8 BOM, разделитель `;`

Кнопки экспорта также на дашборде `/`.

## Структура

```text
bond_parser/
├── app/
│   ├── api/            # web HITL + REST
│   ├── core/           # config, db, enums, logging
│   ├── models/         # ParsingTask, BondIssue, schedules
│   ├── schemas/        # BondDocumentSchema + ValidationResult
│   ├── services/       # collector, extractor, llm, validator, pipeline
│   ├── templates/      # dashboard / queue / review
│   └── main.py
├── samples/
├── storage/
├── tests/
├── .env.example
└── requirements.txt
```

## Замечания по production

- Для эволюции схемы БД подключите Alembic (сейчас `create_all` на старте).
- Добавьте аутентификацию операторов HITL.
- Вынесите тяжёлый пайплайн в отдельный worker (RQ/Arq/Celery), оставив FastAPI тонким.
- Храните PDF в S3-совместимом object storage вместо локального `storage/`.
