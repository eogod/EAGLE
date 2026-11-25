"""
Main module responsible for the upper-level API design,
security middleware layers, and Swagger parameters
"""

from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import select
from starlette.datastructures import Headers

from app.api.api_router import api_router, auth_router, frontend_router
from app.api.deps import AgentRunController, ChainController
from app.api.endpoints import tasks_mcp
from app.api.endpoints.tasks_mcp import mcp as eagle_mcp
from app.cmd.c2_tool import MythicClient
from app.cmd.local_bash import kill_all
from app.cmd.mythic_events import MythicEventsService
from app.core import database_session
from app.core.config import DEBUG_MODE_C, get_settings
from app.core.security.password import get_password_hash
from app.mitre_loader import build_apt_chain_index, load_attack_graph
from app.models import User
from app.services.ttp_info_service import TTPInfoService

# mount only the raw ASGI handler for mcp
eagle_mcp.streamable_http_app(streamable_http_path="/")
_mcp_session_manager = eagle_mcp._lowlevel_server._session_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """create and manage services for C2"""
    mythic_client = MythicClient()
    events_service = MythicEventsService()
    try:
        await mythic_client.connect()
    except Exception as e:
        print(
            "\033[1;33mWARNING:   \033[0mok, you can test some without mythic because",
            e,
        )
    # same websocket reused for task status events by events service
    events_service.bind_client(mythic_client)
    mythic_client.bind_events(events_service)
    await events_service.start()
    app.state.mythic_client = mythic_client
    app.state.events_service = events_service
    app.state.chain_controller = ChainController()
    app.state.agent_run_controller = AgentRunController()
    tasks_mcp._events_service = events_service

    # bootstrap admin until roles exist
    cfg = get_settings().security
    if cfg.admin_email and cfg.admin_password:
        async with database_session.get_async_session() as admin_session:
            exists = await admin_session.scalar(
                select(User).where(User.email == cfg.admin_email)
            )
            if exists is None:
                admin_session.add(
                    User(
                        email=cfg.admin_email,
                        username=cfg.admin_email.split("@", 1)[0],
                        role="admin",
                        hashed_password=get_password_hash(cfg.admin_password),
                    )
                )
                await admin_session.commit()
                print("\033[1mBootstrap admin \033[0m created from .env")

    # load MITRE ATT&CK data for TTP queries in MCP
    settings = get_settings()
    try:
        graph = load_attack_graph(
            settings.mitre.enterprise_attack_path,
            settings.mitre.threat_groups_path,
        )
        # build APT co-use chain index for suggest_next_ttps
        _ttp_svc = TTPInfoService(graph)
        chain_index = build_apt_chain_index(graph, _ttp_svc.get_phase_for_ttp)
        # the finished service is handed to the MCP tools, they touch no raw data
        tasks_mcp._ttp_service = TTPInfoService(graph, chain_index)
    except Exception as e:
        print("\033[1;33mWARNING:   \033[0mMITRE data load failed:", e)
        tasks_mcp._ttp_service = None

    # required for streamable HTTP in mcp
    async with _mcp_session_manager.run():
        yield

    # no orphan nmap on the Kali box, close bash children first
    kill_all()
    await events_service.stop()
    await mythic_client.disconnect()


app = FastAPI(
    title="EAGLE",
    version="0.0.1",
    description="Emulated Attack Generator w/ Layered Engine <br>"
    "<a href='https://github.com/eogod/EAGLE'>source</a> "
    "<a href='/'>GUI</a> <br><br>",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    lifespan=lifespan,
)


def _api_key_guard(app_asgi):
    """fail-closed when MCP__API_KEY is unset"""

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            await app_asgi(scope, receive, send)
            return
        api_key = get_settings().mcp.api_key
        headers = Headers(scope=scope)
        provided = headers.get("x-api-key")
        if not provided:
            auth = headers.get("authorization")
            if auth and auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if not api_key or not provided or not compare_digest(api_key, provided):
            body = b'{"detail": "invalid or missing MCP API key"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await app_asgi(scope, receive, send)

    return guarded


_api_key = get_settings().mcp.api_key
app.mount("/mcp", _api_key_guard(_mcp_session_manager.asgi_app))
if not _api_key:
    print(
        "\033[1;33mWARNING:   \033[0mMCP__API_KEY is not set, /mcp mount will"
        " answer 401 until you add one to .env"
    )

app.include_router(auth_router, prefix="/api")
app.include_router(api_router, prefix="/api")

app.include_router(frontend_router)
# cookies need an exact origin, not a wildcard
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        str(o).rstrip("/") for o in get_settings().security.backend_cors_origins
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Guards against HTTP Host Header attacks
# app.add_middleware(
#    TrustedHostMiddleware,
#    allowed_hosts=get_settings().security.allowed_hosts,)


@app.middleware("http")
async def log_requests_body(request: Request, call_next):
    if DEBUG_MODE_C:
        print(f"\033[1;33mDEBUG:   Request \033[0m: {request.method} {request.url}")
        try:
            body = await request.body()
            if body:
                print(f"\033[1;33mDEBUG:   Request body \033[0m:{body.decode()}")
        except Exception:
            return None

    response = await call_next(request)
    return response


app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent.parent.parent / "frontend"),
    name="static",
)


@app.exception_handler(AssertionError)
async def assertion_exception_handler(request: Request, exc: AssertionError):
    """we don't need 500 at bad input"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    """we don't need 500 at bad value inside"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ConnectionRefusedError)
async def connection_refused_exception_handler(
    request: Request, exc: ConnectionRefusedError
):
    """if something wrong with DB or itz env"""
    print("\033[1;33mWARNING:   \033[0m DB connection:", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "DB connection error, check docker logs and .env creds"},
    )
