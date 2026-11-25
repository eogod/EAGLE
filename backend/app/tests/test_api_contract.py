"""frontend contract file must match the paths, methods and fields the ui uses"""

import json
from pathlib import Path

from app.main import app

CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[3] / "frontend/contracts/api-contract.json"
    ).read_text(encoding="utf-8")
)


def _schema(name: str) -> dict:
    return app.openapi()["components"]["schemas"][name]


def test_every_contract_endpoint_exists() -> None:
    spec = app.openapi()
    for endpoint in CONTRACT["endpoints"]:
        assert endpoint["path"] in spec["paths"], endpoint["path"]
        assert endpoint["method"] in spec["paths"][endpoint["path"]], endpoint


def test_every_consumed_response_field_exists() -> None:
    for name, fields in CONTRACT["responses"].items():
        schema = _schema(name)
        for field in fields:
            assert field in schema["properties"], f"{name}.{field} is gone"


def test_every_sent_request_field_is_accepted() -> None:
    for name, fields in CONTRACT["requests"].items():
        schema = _schema(name)
        for field in fields:
            assert field in schema["properties"], f"{name}.{field} is rejected"
