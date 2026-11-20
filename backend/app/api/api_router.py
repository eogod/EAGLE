"""module for route http paths, format http responses"""

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_messages, deps
from app.api.endpoints import (
    agent_chat,
    agent_drafts,
    agent_sessions,
    auth,
    kill_chain,
    llm,
    tasks,
    users,
)

auth_router = APIRouter()
auth_router.include_router(auth.router, prefix="/auth", tags=["auth"])

api_router = APIRouter(
    responses={
        401: {
            "description": "No `Authorization` access token header, "
            "token is invalid or user removed",
            "content": {
                "application/json": {
                    "examples": {
                        "not authenticated": {
                            "summary": "No authorization token header",
                            "value": {"detail": "Not authenticated"},
                        },
                        "invalid token": {
                            "summary": "Token validation failed, "
                            "decode failed, it may be expired or malformed",
                            "value": {"detail": "Token invalid: {detailed error msg}"},
                        },
                        "removed user": {
                            "summary": api_messages.JWT_ERROR_USER_REMOVED,
                            "value": {"detail": api_messages.JWT_ERROR_USER_REMOVED},
                        },
                    }
                }
            },
        },
    }
)
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(tasks.router, prefix="/cmd", tags=["tasks"])
api_router.include_router(
    llm.router,
    prefix="/llm",
    tags=["llm"],
    dependencies=[Depends(deps.get_current_user)],
)
api_router.include_router(
    kill_chain.router, prefix="/export-chain", tags=["kill-chain"]
)
for _agent_router in (agent_sessions.router, agent_chat.router, agent_drafts.router):
    api_router.include_router(
        _agent_router,
        prefix="/agent",
        tags=["agent"],
        dependencies=[Depends(deps.get_current_user)],
    )

# since same repo
_frontend_dist = (
    Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"
)

frontend_router = APIRouter(include_in_schema=False)

# vite application bundle
_assets_dir = _frontend_dist / "assets"
if _assets_dir.is_dir():
    frontend_router.mount(
        "/assets",
        StaticFiles(directory=_assets_dir),
        name="frontend_assets",
    )


@frontend_router.get("/{full_path:path}", response_class=HTMLResponse)
async def serve_frontend(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if full_path:
        # reject traversal before joining: resolve() + is_relative_to below is
        # what really contains the path, this makes the intent literal
        if ".." in Path(full_path).parts:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (_frontend_dist / full_path).resolve()
        dist_root = _frontend_dist.resolve()
        if candidate.is_file() and candidate.is_relative_to(dist_root):
            return FileResponse(candidate)

    index = _frontend_dist / "index.html"
    # print(index)
    if not index.exists():
        return HTMLResponse(
            content=(
                "<!doctype html><meta charset='utf-8'>"
                "<title>EAGLE</title>"
                "<p>frontend is not built, RTFM bro</p>"
            ),
            status_code=503,
        )
    return HTMLResponse(content=index.read_text(encoding="utf-8"), status_code=200)
