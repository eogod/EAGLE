# Usage

[Русская версия](usage-ru.md)

By default, the application runs at **http://127.0.0.1:8000** and connects to `Mythic C2` on startup.
The React frontend is served at that URL; the API with a `Swagger UI` lives at **/api/docs**.

Accounts follow the Mythic model: the admin comes from `.env` (`SECURITY__ADMIN_EMAIL` / `SECURITY__ADMIN_PASSWORD`, created on startup if missing), open self-registration is off (`SECURITY__REGISTRATION_ENABLED`), and only the admin creates other users via **/auth/register** (role system admin/operator/auditor is TODO). The browser session uses HttpOnly cookies set by **/auth/access-token**; `Authorization: Bearer` stays for Swagger/scripts. AttackChains are attached to users by ID. Logout via **/auth/logout**. Set `SECURITY__COOKIE_SECURE=true` behind TLS.
> The login field is the account email.

The /mcp mount requires a static key: `MCP__API_KEY=...` in `.env`; in the MCP client config pass it as `{"url": "http://<kali>:8000/mcp", "headers": {"Authorization": "Bearer ${EAGLE_MCP_API_KEY}"}}`.

## Direct bash on host

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
(websearch via DuckDuckGo, page fetch with an SSRF guard, CVE lookup via NVD/GitHub,
and CVE search by CPE or software version with a similar-CPE fallback) feeds short
operational notes into the chain draft. Budgets and the on/off switch live in
`WebTools` config (`WEBTOOLS__ENABLED=0` disables it, default on); no API keys needed.

Export a chain (with or without LLM analysis) via **/export-chain/json** or **/export-chain/yaml** to `JSON` or `YAML`.
