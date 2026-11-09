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
при запуске. На этом адресе отдаётся React-фронтенд, а `Swagger UI` с документацией API
доступен по ручке **/api/docs**.

Аккаунты по модели Mythic: админ задаётся в `.env` (`SECURITY__ADMIN_EMAIL` /
`SECURITY__ADMIN_PASSWORD`, создаётся при запуске, если его нет), открытая
самостоятельная регистрация выключена (`SECURITY__REGISTRATION_ENABLED`), а пользователей
создаёт только админ через **/auth/register** (роли admin/operator/auditor - TODO).
Браузерная сессия использует HttpOnly cookies, которые выставляет **/auth/access-token**;
`Authorization: Bearer` остаётся для Swagger и скриптов. AttackChains привязаны к
пользователям по ID. Выход через **/auth/logout**. За TLS установите
`SECURITY__COOKIE_SECURE=true`.
> Учтите, что поле логина - это email аккаунта.

Для монтирования /mcp нужен статический ключ: `MCP__API_KEY=...` в `.env`; в конфиге
MCP-клиента передайте его так: `{"url": "http://<kali>:8000/mcp", "headers": {"Authorization": "Bearer ${EAGLE_MCP_API_KEY}"}}`.

### Прямой bash на хосте

`LOCAL_BASH__ENABLED=1` в `.env` включает POST /cmd/run-bash и MCP-инструмент
run_bash_command: команда выполняется прямо на хосте, где работает EAGLE (разместите EAGLE
на Kali VM), и один вызов возвращает `output / exit_code / truncated / full_log_path`.
Каждый запуск сохраняется как одна строка AttackStep + одна BashTask, статус и вывод
зеркалируются в Valkey `eagle:bash:*`, поэтому цепочки продолжают воспроизводиться.
Безопасность: regex deny-list из `LOCAL_BASH__REJECT` (по умолчанию блокирует команды
класса `rm -rf /`, mkfs, `dd of=/dev/`, fork-бомбы, shutdown, интерактивные клиенты вроде
ssh/telnet/ftp/evil-winrm, psql/mysql без -c/-e, impacket-psexec/wmiexec/smbexec/dcomexec,
зависающие формы nc/socat с -l или LISTEN) + таймаут `LOCAL_BASH__TIMEOUT` (по умолчанию
300 с, максимум 3600) + kill группы процессов. Полные необрезанные логи попадают в
backend/data/bash/ (в gitignore). LLM-анализ остаётся ручным через
**/cmd/step/{id}/analyze**.

После аутентификации можно создать новую цепочку атак через **/cmd/new-chain/**.
Новая цепочка с указанным именем получит ID, который используется для привязки шагов к
этой цепочке.
> Для простоты имена утилит и их типы связаны: цепочка состоит из отдельных шагов, каждый
> подключён к агентам и содержит информацию о текущей фазе атаки.

Команды можно генерировать через LLM, почти как *пентест с LLM*. Сейчас, если команда не
выполняется, она не сохраняется в базу данных, но интеграция с LLM со временем станет
интуитивнее.

Первые команды часто относятся к разведке и выполняются локально, поэтому нужен
`Zero agent` и его `display_id`, который вы указываете в запросах к **/cmd/run-command**.
Если вы ошиблись и команда всё же сохранилась в БД, можно отклонить последний сохранённый
шаг в цепочке и удалить его через **/cmd/reject-s/{chain_name}**

Можно кратко описать задачу и сгенерировать команду EAGLE через `LLM` через POST
**/llm/suggest-action**, затем при необходимости исправить и применить её через POST
**/cmd/approve-action**, указав служебные данные вроде agent_id (он же display id). При
подготовке предложения `AI` учитывает текущую фазу килчейна, значения последних
команд/шагов (например, статус и вывод), текущую цепочку, а затем пытается предложить
потенциально эффективные векторы атаки, команду и целевую ОС.

Для команд, специфичных для агента, используйте **/cmd/run-agent-command**, также указав
имя утилиты агента; для локальных оно задано как `shell`.
Выполненные так команды сразу сохраняются в цепочку, а LLM можно использовать для помощи
в генерации команд и анализе вывода.
> Учтите, что LLM не пишет напрямую в базу данных, а решения LLM принимает пользователь.

payload можно создавать через LLM и проводить их через proc, затем запустить
**/cmd/update-agents**, чтобы сохранить удалённых агентов в базе для воспроизводимости
цепочки. Если задаче нужны более точные настройки или LLM где-то ошибается, используйте
**/cmd/new-agent**, чтобы создать mythic payload через EAGLE в контексте цепочки, затем
**/cmd/run-command**, чтобы загрузить payload на rhost, и снова **/cmd/update-agents**.

Фазы атаки задаются через **/cmd/next-phase/{chain_id}** или конкретная фаза через
**/cmd/set-phase/{chain_id}**.
Статус цепочки можно получить через **/cmd/chain-phase/{chain_id}**.
> Учтите: если вы, например, загрузили payload через curl-команду с Mythic payload UUID,
> при воспроизведении останется старый payload, потому что шаг атаки типа agent уже
> сохранил исходный payload. Это сделано для простоты и является одним из примеров того,
> почему стабильность проекта зависит от стабильности Mythic; Mythic всё равно придётся
> использовать. *Профили* для payload агентов также создаются до запуска EAGLE.

Чтобы запустить цепочку, используйте **/cmd/run-chain/{chain_id}**, который стримит вывод
каждого шага HTTP-чанками.
Аварийная остановка доступна через WebSocket или HTTP-эндпоинты отмены.
Фронтенд на корневом URL закрывает сборку и запуск цепочек (agent / local), анализ вывода
через LLM и панель агента для разбора документов.
В режимах plan/build панель агента может сначала исследовать веб: ограниченный цикл
инструментов (websearch через DuckDuckGo, загрузка страниц, поиск CVE
через NVD/GitHub и поиск CVE по CPE или версии ПО с подбором похожих CPE) передаёт
краткие оперативные заметки в черновик цепочки. Бюджеты и выключатель живут в конфиге
`WebTools` (`WEBTOOLS__ENABLED=0` отключает, по умолчанию включено); API-ключи не нужны.

Экспорт цепочки (с анализом LLM или без) через **/export-chain/json** или
**/export-chain/yaml** в `JSON` или `YAML`.

Аналогично тому, как изучение исходного кода
[библиотеки Mythic python](https://github.com/MythicMeta/Mythic_Scripting/blob/master/mythic/mythic.py)
проясняет её функциональность, изучение других ручек Swagger и чтение кода `EAGLE`
даёт более глубокое понимание его эффективного использования. Если что-то непонятно,
напишите об этом в разделе [issues](https://github.com/eogod/EAGLE/issues).
