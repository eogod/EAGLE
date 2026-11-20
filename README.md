# EAGLE - Emulated Attack Generator w/ Layered Engine

[Russian version](docs/README-ru.md)

[Architecture](docs/architecture.md)

The Emulated Attack Generator with Layered Engine is an interactive system for building
and executing realistic attack chains based on the [Unified Cyber Kill Chain](https://www.unifiedkillchain.com/) stages and supports the dynamic formation of attack chains in real
time. It works in the context of an API that uses GraphQL and the Mythics library plus a minimal frontend.

The platform operates through a C2 infrastructure (Mythic C2 + agents, zero agent) and
supports the generation of new agents during an attack, emulating Lateral movement and
deep penetration into the infrastructure.

Commands that are executed locally are executed for the first stages of the chain only through zero agent.

The project is developed using `FastAPI` + `PostgreSQL` (`SQLAlchemy 2.0` + `alembic`), based on [Minimal async FastAPI + PostgreSQL template](https://github.com/rafsaf/minimal-fastapi-postgres-template/tree/main) 

## Requirements

So far, this has been tested on latest `Kali Linux` and `Parrot OS`. Debian, if the version is lower than 13, may need to build new python [from source](https://docs.python.org/3/using/unix.html#building-python
). The latest `Astra Linux` (Debian 10-12 with MAC policy) have the same problem, but for some reason install_docker_kali.sh works for the mythic, and it even considers the mythic and EAGLE containers secure. EAGLE itself can also work if mythic is on a remote host, zero agent is on another, and postgres is not in the container. On Windows, it is easiest to run it via `WSL2` on Ubuntu or Kali.

The main requirement is `python3.14` and above.

If you decide not to deploy using Docker, you should use `PostgreSQL 18`.

The minimum RAM requirement is 2-4GB, but 8GB is recommended due to agents, profiles from Mythic C2, and zero agent itself.
A minimum dual-core CPU , but it 's difficult with containers.

## Installation 
Initially, Mythic C2 must be installed and running https://docs.mythic-c2.net/installation

For now, the `zero agent` should also work. In fact, you infect yourself in debug mode and connect to C2. The important number here is the first `callback_display_id` to connect to the EAGLE , you will need to specify it in the interface. In Mythic C2 it is located to the left of the active callback.

Clone repository
```bash
git clone https://github.com/eogod/EAGLE.git
```
```bash
cd ./EAGLE/backend
```

At this point, you need to set .env in EAGLE/backend like this, port of db must be different than Mythic C2 postgres, `MYTHIC__` variables should be taken from your `Mythic/.env`
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
Valkey is configured through the `REDIS__*` variables (Redis protocol).
If you need local LLM via [ollama](https://github.com/ollama/ollama) add to env also this. We recommend these models [qwen3.5 4-9b](https://ollama.com/library/qwen3.5), [qwen3.8 27b](https://ollama.com/library/qwen3.8), [gemma4](https://ollama.com/library/gemma4), [nemotron3.5](https://ollama.com/library/nemotron-3.5-lightning) which depends on your [resources](https://github.com/AlexsJones/llmfit).
```env
LLMSERVICE__LOCAL=1
LLMSERVICE__API_URL=http://localhost:11434
LLMSERVICE__API_KEY=super_secret_key
LLMSERVICE__TIMEOUT=120
LLMSERVICE__DEFAULT_MODEL=qwen3.5
```
Model choice reference: https://artificialanalysis.ai/models/open-source/small

Otherwise, the values will be taken from the [config](backend/app/core/config.py), and you can see the validation there.

OpenRouter is optional and only needs the key (and base URL if you proxy it):
```env
LLMSERVICE__OPENROUTER_API_KEY=sk-or-v1-...
# LLMSERVICE__OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

Install dependencies
```bash
# Poetry 2.x on python3.14
poetry install
# or on debian / parrot / kali
python3.14 -m poetry install
```
If it throws an error (like on the kali 2023-2024), then it might help:
```bash
python3.14 -m venv .venv
source .venv/bin/activate
# then the same as above
```
Start the database (PostgreSQL + Valkey) and run migrations
```bash
# Setup database
docker compose up -d

# poetry run executes inside the project venv
poetry run alembic upgrade head
```
Run backend FastAPI
```bash
# not secure but for tests
poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Usage

By default, the application runs at **http://127.0.0.1:8000** and connects to `Mythic C2` on startup.
All three front doors below drive the same chains.

- **Web UI** - the React frontend at the root URL: chain building and execution (agent / local), LLM-assisted output analysis, the document-analysis agent panel.
- **MCP server** - a `/mcp` mount for MCP clients.
- **Swagger / API** - `Swagger UI` at **/api/docs**.

Chains, phases, LLM suggestions, direct bash on the host, chain export, the MCP key and the cookie / bearer setup are described in [docs/usage.md](docs/usage.md).

Similar to how diving into [Mythic python library](https://github.com/MythicMeta/Mythic_Scripting/blob/master/mythic/mythic.py) source code clarifies its functionality, examining the other Swagger endpoints and read `EAGLE` codebase provides deeper insight into using it effectively. If anything is unclear, write it in the [issues](https://github.com/eogod/EAGLE/issues) section.

## Tests

Backend tests run on a throwaway SQLite database by default (`poetry run pytest` from `backend/`); set `EAGLE_TEST_DB=postgres` to run them against a real Postgres. Frontend checks: `bun run check && bun run build`.

## TODO

Below are the larger pieces a full scenario run still needs. The smaller ones are marked with `# TODO` in the code.

- **/import-chain** should read the same `JSON` / `YAML` that **/export-chain** writes and create planned steps. A separate run record would keep the plan apart from each execution, so a replay appends history instead of overwriting `AttackStep`.

- A chain should declare variables, capture values from `raw_log` with a regex or JSONPath, and substitute them into later commands. This covers hostnames, ports, artifact names and keys that change between runs.

- Listeners and tunnels need start, stop and health checks that outlive one step. Interactive tools need a driver that sends a line and waits for the prompt.

- A step should declare which agent or tunnel carries its traffic, and the runner should check the route and source address before it executes.

- Upload and download steps, an artifact registry with hashes and a naming policy, and local build recipes with a pinned toolchain are still missing. Mythic payloads are the only build path today.

- A run manifest should export the run id, timings, artifacts and assertion results. Optional post-run checks can query external telemetry and record the outcome.

- Independent services and scans should run concurrently, and each step should choose whether a failure stops the chain, retries or jumps to a fallback.
