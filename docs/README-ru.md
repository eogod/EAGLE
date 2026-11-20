# EAGLE - Emulated Attack Generator w/ Layered Engine

[English version](../README.md)

[Архитектура](architecture.md)

Генератор атак с многоуровневым движком (Emulated Attack Generator with Layered Engine)
- интерактивная система для построения и выполнения реалистичных цепочек атак на основе
стадий [Unified Cyber Kill Chain](https://www.unifiedkillchain.com/) с динамическим
формированием цепочек в реальном времени. Работает в контексте API, использующего
GraphQL и библиотеку Mythic, плюс минимальный фронтенд.

Платформа работает через инфраструктуру C2 (Mythic C2 + агенты, нулевой агент) и
поддерживает генерацию новых агентов во время атаки, эмулируя боковое перемещение и
закрепление в инфраструктуре.

Команды, выполняемые локально, на первых стадиях цепочки идут только через нулевой агент.

Проект разработан на `FastAPI` + `PostgreSQL` (`SQLAlchemy 2.0` + `alembic`), за основу взят [Minimal async FastAPI + PostgreSQL template](https://github.com/rafsaf/minimal-fastapi-postgres-template/tree/main)

## Требования

Пока что протестировано на последних `Kali Linux` и `Parrot OS`. В Debian версии ниже 13
может потребоваться [сборка нового Python](https://docs.python.org/3/using/unix.html#building-python
) из исходников. В последнем `Astra Linux` (Debian 10-12 с политикой МКЦ/МРД) та же
проблема, но скрипт install_docker_kali.sh почему-то работает для mythic, и
даже считает контейнеры mythic и EAGLE безопасными. Сам EAGLE тоже может работать, если
mythic на удалённом хосте, нулевой агент на другом, а postgres не в контейнере. В Windows
проще всего запускать через `WSL2` на Ubuntu или Kali.

Основное требование - `python3.14` и выше.

Если вы решите не разворачивать через Docker, используйте `PostgreSQL 18`.

Минимальные требования к RAM - 2-4 ГБ, но рекомендуется 8 ГБ из-за агентов, профилей
Mythic C2 и самого нулевого агента. Минимум - двухъядерный CPU, но с контейнерами это
бывает сложно.

## Установка

Сначала нужно установить и запустить Mythic C2 https://docs.mythic-c2.net/installation

Пока что `zero agent` тоже должен работать. По сути, вы заражаете себя в режиме отладки и
подключаетесь к C2. Важный момент - первый `callback_display_id` для подключения к EAGLE,
его нужно указать в интерфейсе. В Mythic C2 он находится слева от активного callback.

Клонирование репозитория
```bash
git clone https://github.com/eogod/EAGLE.git
```
```bash
cd ./EAGLE/backend
```

На этом этапе задайте `.env` в EAGLE/backend следующим образом: порт БД должен отличаться
от postgres в Mythic C2, переменные `MYTHIC__` берутся из вашего `Mythic/.env`
```env
SECURITY__JWT_SECRET_KEY=super_secret_key
SECURITY__BACKEND_CORS_ORIGINS=["http://127.0.0.1:3001","http://localhost:8001", "http://localhost:8000", "http://127.0.0.1:8000"]
SECURITY__ALLOWED_HOSTS=["localhost", "127.0.0.1"]

DATABASE__HOSTNAME=localhost
DATABASE__USERNAME=super_secret_user
DATABASE__PASSWORD=super_secret_password
DATABASE__PORT=5455
DATABASE__DB=default_db

MYTHIC__SERVER_IP=127.0.0.1
MYTHIC__USERNAME=super_secret_user
MYTHIC__PASSWORD=super_secret_password
MYTHIC__SERVER_PORT=7443
MYTHIC__TIMEOUT=-1
MYTHIC__PAYLOAD_PORT_HTTP=1337

REDIS__PASSWORD=super_secret_password
REDIS__PORT=6379
```
Valkey настраивается через переменные `REDIS__*` (протокол Redis).
Если нужен локальный LLM через [ollama](https://github.com/ollama/ollama), добавьте в
`.env` ещё это. Рекомендуем модели [qwen3.5 4-9b](https://ollama.com/library/qwen3.5),
[qwen3.8 27b](https://ollama.com/library/qwen3.8),
[gemma4](https://ollama.com/library/gemma4),
[nemotron3.5](https://ollama.com/library/nemotron-3.5-lightning) в зависимости от ваших
[ресурсов](https://github.com/AlexsJones/llmfit).
```env
LLMSERVICE__LOCAL=1
LLMSERVICE__API_URL=http://localhost:11434
LLMSERVICE__API_KEY=super_secret_key
LLMSERVICE__TIMEOUT=120
LLMSERVICE__DEFAULT_MODEL=qwen3.5
```
Ориентир по выбору модели: https://artificialanalysis.ai/models/open-source/small

Иначе значения берутся из [конфига](../backend/app/core/config.py), валидацию можно
посмотреть там.

OpenRouter необязателен, нужен только ключ (и base URL, если используете прокси):
```env
LLMSERVICE__OPENROUTER_API_KEY=sk-or-v1-...
# LLMSERVICE__OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

Установка зависимостей
```bash
# Poetry 2.x на python3.14
poetry install
# или на debian / parrot / kali
python3.14 -m poetry install
```
Если возникает ошибка (например, на Kali 2023-2024), может помочь:
```bash
python3.14 -m venv .venv
source .venv/bin/activate
# затем то же самое, что выше
```
Запуск базы данных (PostgreSQL + Valkey) и миграций
```bash
# поднимаем базу
docker compose up -d

# poetry run выполняет команду в venv проекта
poetry run alembic upgrade head
```
Запуск backend FastAPI
```bash
# небезопасно, только для тестов
poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Использование

По умолчанию приложение работает на **http://127.0.0.1:8000** и подключается к `Mythic C2`
при запуске. Все три входа ниже работают с одними и теми же цепочками.

- **Веб-интерфейс** - React-фронтенд на корневом URL: сборка и запуск цепочек (agent / local), анализ вывода через LLM, панель агента для разбора документов.
- **MCP-сервер** - монтирование `/mcp` для MCP-клиентов.
- **Swagger / API** - `Swagger UI` по ручке **/api/docs**.

Цепочки, фазы, LLM-подсказки, прямой bash на хосте, экспорт цепочек, ключ MCP и настройка cookie / bearer описаны в [usage-ru.md](usage-ru.md).

Аналогично тому, как изучение исходного кода
[библиотеки Mythic python](https://github.com/MythicMeta/Mythic_Scripting/blob/master/mythic/mythic.py)
проясняет её функциональность, изучение других ручек Swagger и чтение кода `EAGLE`
даёт более глубокое понимание его эффективного использования. Если что-то непонятно,
напишите об этом в разделе [issues](https://github.com/eogod/EAGLE/issues).

## Тесты

Бэкенд-тесты по умолчанию используют временную базу SQLite (`poetry run pytest` из `backend/`); `EAGLE_TEST_DB=postgres` переключает их на настоящий Postgres. Проверки фронтенда: `bun run check && bun run build`.
