"""Provider adapters: verify wire parsing, config errors and failure mapping."""

import json

import httpx
import pytest

from app.core.config import LLMservice
from app.services.llm_service import (
    AgentLLMError,
    G4FProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderNotConfigured,
    _ollama_content,
    _openai_sse_delta,
    _rank_openrouter_models,
    _text_chat_models,
    build_provider,
)

MESSAGES = [{"role": "user", "content": "hello"}]


def _use_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.llm_service.httpx.AsyncClient",
        lambda **kw: original(**{**kw, "transport": transport}),
    )


def test_openai_sse_delta_variants():
    assert _openai_sse_delta("") == ""
    assert _openai_sse_delta("event: ping") == ""
    assert _openai_sse_delta("data: [DONE]") == ""
    assert _openai_sse_delta("data: not-json") == ""
    assert _openai_sse_delta("data: {}") == ""
    assert _openai_sse_delta('data: {"choices": []}') == ""
    assert _openai_sse_delta('data: {"choices": [{"delta": {"content": null}}]}') == ""
    assert (
        _openai_sse_delta('data: {"choices": [{"delta": {"content": "hi"}}]}') == "hi"
    )


def test_ollama_content_shapes():
    assert _ollama_content({"message": {"content": "dict"}}) == "dict"
    assert _ollama_content({"message": {}}) == ""
    assert _ollama_content({"message": "weird"}) == ""

    class Obj:
        message = type("M", (), {"content": "attr"})()

    assert _ollama_content(Obj()) == "attr"
    assert _ollama_content(object()) == ""


def test_build_provider_dispatch():
    assert isinstance(build_provider(LLMservice()), OllamaProvider)
    with pytest.raises(ProviderNotConfigured):
        build_provider(LLMservice(openrouter_api_key=None), provider="openrouter")
    with pytest.raises(ProviderNotConfigured):
        build_provider(
            LLMservice(openrouter_api_key="sk-or-v1-x"), provider="openrouter"
        )
    assert isinstance(build_provider(LLMservice(), provider="g4f"), G4FProvider)
    with pytest.raises(ProviderNotConfigured):
        build_provider(LLMservice(), provider="nope")


@pytest.mark.asyncio
async def test_openrouter_requires_key_and_model():
    with pytest.raises(ProviderNotConfigured):
        OpenRouterProvider(LLMservice(openrouter_api_key=None), model="m")
    with pytest.raises(ProviderNotConfigured):
        OpenRouterProvider(LLMservice(openrouter_api_key="sk-or-v1-x"))
    provider = OpenRouterProvider(
        LLMservice(openrouter_api_key="sk-or-v1-x"), model="openai/gpt-4o-mini"
    )
    assert provider.model == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_openrouter_streams_and_sends_json_mode(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        body = (
            'data: {"choices": [{"delta": {"content": "{\\"a\\""}}]}\n\n'
            'data: {"choices": [{"delta": {"content": ": 1}"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    _use_transport(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMservice(openrouter_api_key="sk-or-v1-x"), model="openai/gpt-4o-mini"
    )
    text = await provider.complete(MESSAGES, json_mode=True)
    assert text == '{"a": 1}'
    assert seen["body"]["stream"] is True
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["temperature"] == 0.1
    assert seen["auth"] == "Bearer sk-or-v1-x"


@pytest.mark.asyncio
async def test_openrouter_http_error_maps_to_agent_error(monkeypatch):
    _use_transport(
        monkeypatch, lambda request: httpx.Response(429, content=b"slow down")
    )
    provider = OpenRouterProvider(
        LLMservice(openrouter_api_key="sk-or-v1-x"), model="m"
    )
    with pytest.raises(AgentLLMError, match="429"):
        await provider.complete(MESSAGES)


@pytest.mark.asyncio
async def test_openrouter_transport_error_maps_to_agent_error(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    _use_transport(monkeypatch, boom)
    provider = OpenRouterProvider(
        LLMservice(openrouter_api_key="sk-or-v1-x"), model="m"
    )
    with pytest.raises(AgentLLMError, match="request failed"):
        await provider.complete(MESSAGES)


class _FakeOllama:
    def __init__(self, reply=None, error=None):
        self.reply = reply or []
        self.error = error

    async def chat(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error

        async def gen():
            for item in self.reply:
                yield item

        return gen()


@pytest.mark.asyncio
async def test_ollama_streams_dict_and_object_chunks(monkeypatch):
    fake = _FakeOllama(
        reply=[
            {"message": {"content": "a"}},
            type("P", (), {"message": type("M", (), {"content": "b"})()})(),
        ]
    )
    monkeypatch.setattr("app.services.llm_service.OllamaAsyncClient", lambda **kw: fake)
    provider = OllamaProvider(LLMservice(), model="m")
    assert await provider.complete(MESSAGES, json_mode=True) == "ab"
    assert fake.kwargs["format"] == "json"
    assert fake.kwargs["model"] == "m"


@pytest.mark.asyncio
async def test_ollama_failure_maps_to_agent_error(monkeypatch):
    fake = _FakeOllama(error=RuntimeError("down"))
    monkeypatch.setattr("app.services.llm_service.OllamaAsyncClient", lambda **kw: fake)
    provider = OllamaProvider(LLMservice())
    with pytest.raises(AgentLLMError, match="ollama request failed"):
        await provider.complete(MESSAGES)


def test_base_provider_stream_is_abstract():
    from app.services.llm_service import BaseProvider  # noqa: PLC0415

    with pytest.raises(NotImplementedError):
        BaseProvider().stream(MESSAGES)


def test_ollama_init_failure_is_a_config_error(monkeypatch):
    # pydantic does not validate defaults, so a broken endpoint must fail here
    def boom(**kwargs):
        raise ValueError("bad host")

    monkeypatch.setattr("app.services.llm_service.OllamaAsyncClient", boom)
    with pytest.raises(ProviderNotConfigured, match="invalid ollama endpoint"):
        OllamaProvider(LLMservice())


@pytest.mark.asyncio
async def test_ollama_passes_agent_error_through(monkeypatch):
    fake = _FakeOllama(error=AgentLLMError("already mapped"))
    monkeypatch.setattr("app.services.llm_service.OllamaAsyncClient", lambda **kw: fake)
    provider = OllamaProvider(LLMservice())
    with pytest.raises(AgentLLMError, match="already mapped"):
        await provider.complete(MESSAGES)


def test_openai_compatible_can_reuse_generic_api_key():
    provider = OpenRouterProvider(LLMservice(api_key="sk-generic"), model="local-model")
    assert provider.model == "local-model"


def test_text_chat_models_drops_non_chat_entries():
    items = [
        {
            "id": "text/model",
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        },
        {
            "id": "image/model",
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["image"],
            },
        },
        {
            "id": "audio/model",
            "architecture": {
                "input_modalities": ["audio"],
                "output_modalities": ["text"],
            },
        },
        {"id": "legacy/model"},
        {"id": ""},
        {},
    ]
    assert _text_chat_models(items) == ["text/model", "legacy/model"]


def test_openrouter_free_models_come_first_and_default_free():
    ranked, default = _rank_openrouter_models(
        ["openai/gpt-4o-mini", "zeta/model:free", "alpha/model:free", "ai/model"]
    )
    assert ranked == [
        "alpha/model:free",
        "zeta/model:free",
        "ai/model",
        "openai/gpt-4o-mini",
    ]
    assert default == "alpha/model:free"
    assert _rank_openrouter_models([]) == ([], "")
