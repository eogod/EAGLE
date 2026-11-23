"""
Module for test the SPA static router: files inside frontend/dist are served,
path traversal is rejected and unknown routes fall back to index.html
"""

from pathlib import Path

import pytest

from app.api import api_router
from app.api.api_router import serve_frontend

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(name="dist")
def fixture_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """pretend the vite bundle was built here"""
    (tmp_path / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(api_router, "_frontend_dist", tmp_path)
    return tmp_path


async def test_frontend_router_serves_a_file_from_dist(dist: Path) -> None:
    response = await serve_frontend("app.js")

    assert response.status_code == 200
    assert Path(response.path) == dist / "app.js"


async def test_frontend_router_serves_a_nested_file(dist: Path) -> None:
    (dist / "assets").mkdir()
    (dist / "assets" / "index.js").write_text("console.log(2)", encoding="utf-8")

    response = await serve_frontend("assets/index.js")

    assert response.status_code == 200
    assert Path(response.path) == dist / "assets" / "index.js"


async def test_frontend_router_ignores_a_symlink_out_of_dist(dist: Path) -> None:
    """a link inside the bundle must not be followed outside it"""
    secret = dist.parent / "secret"
    secret.write_text("top secret", encoding="utf-8")
    (dist / "linked.js").symlink_to(secret)

    response = await serve_frontend("linked.js")

    assert response.status_code == 200
    assert response.body == b"<html>spa</html>"


@pytest.mark.parametrize("full_path", ["../secret", "assets/../../secret"])
async def test_frontend_router_rejects_traversal(dist: Path, full_path: str) -> None:
    """the file exists outside dist, but it must not be served"""
    (dist.parent / "secret").write_text("top secret", encoding="utf-8")

    response = await serve_frontend(full_path)

    assert response.status_code == 404


async def test_frontend_router_falls_back_to_index(dist: Path) -> None:
    response = await serve_frontend("some/spa/route")

    assert response.status_code == 200
    assert response.body == b"<html>spa</html>"
