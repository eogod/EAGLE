"""Provider adapters: verify wire parsing, config errors and failure mapping."""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import HttpUrl

import app.services.llm_service as llm_module
from app.core.config import LLMservice
from app.services.llm_service import (
    AgentLLMError,
    BaseProvider,
    G4FProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderNotConfigured,
    ToolCall,
    ToolTurn,
    _ollama_content,
    _ollama_tool_message,
    _openai_sse_delta,
    _parse_emulated_tool_reply,
    _rank_openrouter_models,
    _text_chat_models,
    build_provider,
    list_models,
)

MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "hello"}]


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


class _FakeModels:
    default = "default"
    Model = dict


class _FakeG4F:
    models = _FakeModels


def _stub_g4f(monkeypatch: pytest.MonkeyPatch) -> None:
    """dispatch only needs a module object, so the real g4f stays unimported"""
    monkeypatch.setattr(
        llm_module, "_g4f_state", SimpleNamespace(module=_FakeG4F, client=object())
    )


def test_build_provider_dispatch(monkeypatch: pytest.MonkeyPatch):
    assert isinstance(build_provider(LLMservice()), OllamaProvider)
    with pytest.raises(ProviderNotConfigured):
        build_provider(LLMservice(openrouter_api_key=None), provider="openrouter")
    with pytest.raises(ProviderNotConfigured):
        build_provider(
            LLMservice(openrouter_api_key="sk-or-v1-x"), provider="openrouter"
        )
    # off by default, so neither startup nor an agent run imports g4f
    with pytest.raises(ProviderNotConfigured):
        build_provider(LLMservice(), provider="g4f")

    _stub_g4f(monkeypatch)
    assert isinstance(build_provider(LLMservice(g4f=1), provider="g4f"), G4FProvider)
    with pytest.raises(ProviderNotConfigured):
        build_provider(LLMservice(), provider="nope")


@pytest.mark.asyncio
async def test_list_models_caches_discovery(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def fake_ollama(_cfg: LLMservice) -> dict[str, Any]:
        calls.append("ollama")
        return {"available": True, "models": ["m"], "default": "m", "error": None}

    async def fake_openrouter(_cfg: LLMservice) -> dict[str, Any]:
        calls.append("openrouter")
        return {"available": True, "models": [], "default": "", "error": None}

    clock = [0.0]
    monkeypatch.setattr(llm_module, "_ollama_models", fake_ollama)
    monkeypatch.setattr(llm_module, "_openrouter_models", fake_openrouter)
    monkeypatch.setattr(llm_module, "_model_list_cache", llm_module._ModelListCache())
    monkeypatch.setattr(llm_module, "monotonic", lambda: clock[0])

    cfg = LLMservice()
    first = await list_models(cfg)
    assert await list_models(cfg) is first
    assert calls == ["ollama", "openrouter"]

    other = LLMservice(api_url=HttpUrl("http://other:11434"))
    assert await list_models(other) is not first
    clock[0] = llm_module._MODEL_LIST_TTL + 1
    await list_models(cfg)
    assert calls.count("ollama") == 3


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


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "websearch",
            "description": "search the web",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }
]

ASSISTANT_CALL = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "type": "function",
            "id": "call_1",
            "function": {
                "name": "websearch",
                "arguments": '{"query": "cats"}',
            },
        }
    ],
}

TOOL_RESULT = {
    "role": "tool",
    "tool_call_id": "call_1",
    "name": "websearch",
    "content": "some web page",
}

TOOL_TRANSCRIPT: list[dict[str, Any]] = [
    ASSISTANT_CALL,
    TOOL_RESULT,
    {"role": "user", "content": "then answer"},
]


def _sse(payload: str) -> bytes:
    chunk = json.dumps({"choices": [{"delta": {"content": payload}}]})
    return f"data: {chunk}\n\ndata: [DONE]\n\n".encode()


@pytest.mark.asyncio
async def test_openrouter_complete_tools_native_happy(monkeypatch):
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "websearch",
                                            "arguments": '{"query": "cats"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
        )

    _use_transport(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMservice(openrouter_api_key="sk-or-v1-x"), model="m"
    )
    turn = await provider.complete_tools(TOOL_TRANSCRIPT, TOOLS)
    assert turn == ToolTurn(
        content="",
        tool_calls=[
            ToolCall(id="call_1", name="websearch", arguments={"query": "cats"})
        ],
    )
    assert seen["body"]["tools"] == TOOLS
    assert seen["body"]["tool_choice"] == "auto"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"] == TOOL_TRANSCRIPT
    assert seen["accept"] == "application/json"


@pytest.mark.asyncio
async def test_openrouter_complete_tools_400_falls_back_to_emulated(monkeypatch):
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "tools" in body:
            return httpx.Response(400, content=b"tools unsupported")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse('{"final": "the answer"}'),
        )

    _use_transport(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMservice(openrouter_api_key="sk-or-v1-x"), model="m"
    )
    turn = await provider.complete_tools(TOOL_TRANSCRIPT, TOOLS)
    assert turn == ToolTurn(content="the answer", tool_calls=[])
    assert provider.tools_fallback is True
    assert len(bodies) == 2
    assert "tools" not in bodies[1]
    assert bodies[1]["response_format"] == {"type": "json_object"}

    # the native path is skipped from now on
    turn = await provider.complete_tools(MESSAGES, TOOLS)
    assert turn.content == "the answer"
    assert len(bodies) == 3
    assert "tools" not in bodies[2]


@pytest.mark.asyncio
async def test_openrouter_native_bad_json_args_kept_raw(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_9",
                                        "function": {
                                            "name": "websearch",
                                            "arguments": "not json at all",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ),
        )

    _use_transport(monkeypatch, handler)
    provider = OpenRouterProvider(
        LLMservice(openrouter_api_key="sk-or-v1-x"), model="m"
    )
    turn = await provider.complete_tools(MESSAGES, TOOLS)
    assert turn.tool_calls == [
        ToolCall(id="call_9", name="websearch", arguments={"_raw": "not json at all"})
    ]


def test_ollama_tool_message_conversion():
    assert _ollama_tool_message(ASSISTANT_CALL) == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "websearch", "arguments": {"query": "cats"}}}
        ],
    }
    assert _ollama_tool_message(TOOL_RESULT) == {
        "role": "tool",
        "content": "some web page",
        "tool_name": "websearch",
    }
    assert _ollama_tool_message({"role": "user", "content": "hi"}) == {
        "role": "user",
        "content": "hi",
    }


@pytest.mark.asyncio
async def test_ollama_complete_tools_native_dict_response(monkeypatch):
    reply = {
        "message": {
            "content": "thinking...",
            "tool_calls": [
                {"function": {"name": "websearch", "arguments": {"query": "cats"}}}
            ],
        }
    }
    fake = _FakeOllama(reply=[])

    async def chat(**kwargs):
        fake.kwargs = kwargs
        return reply

    fake.chat = chat
    monkeypatch.setattr("app.services.llm_service.OllamaAsyncClient", lambda **kw: fake)
    provider = OllamaProvider(LLMservice(), model="m")
    turn = await provider.complete_tools(MESSAGES, TOOLS)
    assert fake.kwargs["stream"] is False
    assert fake.kwargs["tools"] == TOOLS
    assert fake.kwargs["options"] == {"temperature": 0.1}
    assert fake.kwargs["model"] == "m"
    assert turn == ToolTurn(
        content="thinking...",
        tool_calls=[
            ToolCall(id="call_0", name="websearch", arguments={"query": "cats"})
        ],
    )


@pytest.mark.asyncio
async def test_ollama_complete_tools_falls_back_when_native_rejected(monkeypatch):
    fake = _FakeOllama(reply=[{"message": {"content": '{"final": "done here"}'}}])

    async def chat(**kwargs):
        fake.kwargs = kwargs
        if "tools" in kwargs:
            raise AgentLLMError("tool unsupported")

        async def gen():
            for item in fake.reply:
                yield item

        return gen()

    monkeypatch.setattr("app.services.llm_service.OllamaAsyncClient", lambda **kw: fake)
    provider = OllamaProvider(LLMservice(), model="m")
    fake.chat = chat
    turn = await provider.complete_tools(MESSAGES, TOOLS)
    assert turn == ToolTurn(content="done here", tool_calls=[])
    assert provider.tools_fallback is True
    assert fake.kwargs.get("format") == "json"
    assert fake.kwargs["messages"][-1]["role"] == "user"
    assert "Choose tool calls" in fake.kwargs["messages"][-1]["content"]


class _TranscriptStub(BaseProvider):
    def __init__(self, text: str):
        self.text = text
        self.seen: list[list[dict[str, str]]] = []

    async def stream(self, messages: list[dict[str, str]], *, json_mode: bool = False):
        self.seen.append(messages)
        yield self.text


@pytest.mark.asyncio
async def test_base_provider_emulated_builds_flat_prompt_and_parses_calls():
    stub = _TranscriptStub(
        '{"tool_calls": [{"name": "websearch", "arguments": {"query": "cats"}}]}'
    )
    turn = await stub.complete_tools(TOOL_TRANSCRIPT, TOOLS)
    assert turn.tool_calls == [
        ToolCall(id="call_0_websearch", name="websearch", arguments={"query": "cats"})
    ]
    assert turn.content == ""

    flat = "\n".join(m["content"] for m in stub.seen[0])
    assert 'TOOL websearch({"query": "cats"}): some web page' in flat
    assert "ASSISTANT: tried tool calls (websearch)" in flat
    assert "SYSTEM: then answer" not in flat
    assert '"parameters"' in flat  # tool schemas included
    assert "untrusted" in flat
    assert stub.seen[0][0]["role"] == "system"
    assert (
        stub.seen[0][-1]["content"]
        == "Choose tool calls or write the final answer as JSON."
    )


def test_parse_emulated_tool_reply_shapes():
    turn = _parse_emulated_tool_reply('{"final": "all done"}')
    assert turn == ToolTurn(content="all done", tool_calls=[])
    turn = _parse_emulated_tool_reply("Hello, plain text answer")
    assert turn == ToolTurn(content="Hello, plain text answer", tool_calls=[])
    fenced = '```json\n{"tool_calls": [{"name": "websearch", "arguments": "{\\"q\\": 1}"}]}\n```'
    turn = _parse_emulated_tool_reply(fenced)
    assert turn == ToolTurn(
        content="",
        tool_calls=[
            ToolCall(id="call_0_websearch", name="websearch", arguments={"q": 1})
        ],
    )
