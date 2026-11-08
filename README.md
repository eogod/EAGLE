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
The React frontend is served at that URL; the API with a `Swagger UI` lives at **/api/docs**.

Accounts follow the Mythic model: the admin comes from `.env` (`SECURITY__ADMIN_EMAIL` / `SECURITY__ADMIN_PASSWORD`, created on startup if missing), open self-registration is off (`SECURITY__REGISTRATION_ENABLED`), and only the admin creates other users via **/auth/register** (role system admin/operator/auditor is TODO). The browser session uses HttpOnly cookies set by **/auth/access-token**; `Authorization: Bearer` stays for Swagger/scripts. AttackChains are attached to users by ID. Logout via **/auth/logout**. Set `SECURITY__COOKIE_SECURE=true` behind TLS.
> The login field is the account email.

The /mcp mount requires a static key: `MCP__API_KEY=...` in `.env`; in the MCP client config pass it as `{"url": "http://<kali>:8000/mcp", "headers": {"Authorization": "Bearer ${EAGLE_MCP_API_KEY}"}}`.

### Direct bash on host

`LOCAL_BASH__ENABLED=1` in `.env` enables POST /cmd/run-bash and the MCP tool run_bash_command: a command runs directly on the host where EAGLE runs (put EAGLE on the Kali VM) and one call returns `output / exit_code / truncated / full_log_path`. Each run is stored as one AttackStep + one BashTask row, status and output are mirrored to Valkey `eagle:bash:*`, so chains keep replaying. Safety: regex deny-list from `LOCAL_BASH__REJECT` (default blocks `rm -rf /`-class, mkfs, `dd of=/dev/`, fork bombs, shutdown, interactive clients like ssh/telnet/ftp/evil-winrm, psql/mysql without -c/-e, impacket-psexec/wmiexec/smbexec/dcomexec, hang forms of nc/socat with -l or LISTEN) + timeout `LOCAL_BASH__TIMEOUT` (default 300s, max 3600) + process-group kill. Full uncut logs land in backend/data/bash/ (gitignored). LLM analysis stays manual via **/cmd/step/{id}/analyze**.


Once authenticated, you can create a new attack chain with **/cmd/new-chain/**.
A new chain with your specified name will be assigned an ID, which is used to attach steps to that chain.
> For simplicity, utility names and their types are linked, a chain consists of individual steps, each connected to agents and containing information about the current attack phase.

You can generate commands via the LLM, almost like a *pentest with LLM*.
Currently, if a command not execute, it will not be saved to the database but LLM integration will be made more intuitive over time.

The first commands are often Reconnaissance and run locally, so you need `Zero agent` and its `display_id`,
which you include in requests to **/cmd/run-command**.
If you may have made a mistake and any command was somewhy saved to the DB, you can reject the last saved command in the chain and delete it via **/cmd/reject-s/{chain_name}**

You can briefly describe the task and generate the EAGLE command via `LLM` via POST **/llm/suggest-action** , then correct and finalize it if necessary and apply it via POST **/cmd/approve-action** specifying some service data as agent_id (same as display id) . When making an suggestion, the `AI` considers the current phase of the kill chain, the values of the most recent commands/steps (like status and output), the current chain, and then attempts to suggest potentially effective attack vectors and command, target os.

For agent-specific commands, use **/cmd/run-agent-command**, also specifying the agent utility name, for local it's set as `shell`.
Commands executed this way are immediately saved to the chain, 
you can use the LLM to assist with command generation and output analysis.
> Note that the LLM does not write directly to the database and LLM decisions are user-driven.

You can create payloads via the LLM and route them through proc, 
then run **/cmd/update-agents** to save remote agents in the database for reproducibility of the chain. If the task needs more precise adjustments, or the LLM makes a mistake somewhere, you should use **/cmd/new-agent** for create mythic payload via EAGLE in context of chain, then **/cmd/run-command** for uploading payload to the rhost and then also **/cmd/update-agents** .

You can set attack phases with **/cmd/next-phase/{chain_id}**
or set a specific phase via **/cmd/set-phase/{chain_id}**.
Chain status can be retrieved with **/cmd/chain-phase/{chain_id}**.
> Note that if, for example, you uploaded a payload via a curl command using Mythic payload UUID, it will remain the old payload during replay because the agent-type attack step has already stored the original payload. This is done for simplicity and is one example of why our project’s stability depends on Mythic’s stability; you still need to use Mythic to some extent. *Profiles* for agent payloads are also created before run EAGLE.

To run chain, use **/cmd/run-chain/{chain_id}**, which streams each step’s output as HTTP chunks.
Emergency stop is available via WebSocket or HTTP cancel endpoints.
The frontend at the root URL covers chain building and execution (agent / local),
LLM-assisted output analysis, and the document-analysis agent panel.
In plan/build the agent panel can research the web first: a bounded tool loop
(websearch via DuckDuckGo, page fetch with an SSRF guard, CVE lookup via NVD/GitHub)
feeds short operational notes into the chain draft. Budgets and the on/off switch live
in `WebTools` config (`WEBTOOLS__ENABLED=0` disables it, default on); no API keys needed.

Export a chain (with or without LLM analysis) via **/export-chain/json** or **/export-chain/yaml** to `JSON` or `YAML`.

Similar to how diving into [Mythic python library](https://github.com/MythicMeta/Mythic_Scripting/blob/master/mythic/mythic.py) source code clarifies its functionality, examining the other Swagger endpoints and read `EAGLE` codebase provides deeper insight into using it effectively. If anything is unclear, write it in the [issues](https://github.com/eogod/EAGLE/issues) section.
