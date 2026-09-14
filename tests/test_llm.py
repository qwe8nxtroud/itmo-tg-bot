"""Клиент модели против локального HTTP-сервера: без сети и без настоящего API."""

import asyncio
import json

import pytest
from aiohttp import ClientSession, web

from app.llm import (
    LLMClient,
    LLMEmptyResponseError,
    LLMError,
    LLMTimeoutError,
    LLMUnavailableError,
)

API_KEY = "llm-secret-key"


class FakeAPI:
    """OpenAI-совместимый эндпоинт с настраиваемым ответом; запоминает запросы."""

    def __init__(self) -> None:
        self.requests: list[tuple[web.Request, dict]] = []
        self.status = 200
        self.body: object = {
            "choices": [{"message": {"role": "assistant", "content": "  Привет!  "}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
        self.raw: bytes | None = None
        self.delay = 0.0

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append((request, await request.json()))
        await asyncio.sleep(self.delay)
        if self.raw is not None:
            return web.Response(body=self.raw, status=self.status)
        return web.json_response(self.body, status=self.status)


@pytest.fixture
async def api(unused_tcp_port):
    fake = FakeAPI()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    fake.base_url = f"http://127.0.0.1:{unused_tcp_port}/v1"
    try:
        yield fake
    finally:
        await runner.cleanup()


@pytest.fixture
async def session():
    async with ClientSession() as client:
        yield client


def client(session, api, **overrides) -> LLMClient:
    options = {
        "base_url": api.base_url,
        "api_key": API_KEY,
        "model": "test-model",
        "timeout": 1.0,
        "max_tokens": 256,
    }
    return LLMClient(session, **{**options, **overrides})


async def test_request_body_headers_and_parsed_response(session, api):
    # Arrange
    messages = [{"role": "system", "content": "инструкция"}, {"role": "user", "content": "вопрос"}]
    # Act
    response = await client(session, api).complete(messages, temperature=0.3)
    # Assert
    request, body = api.requests[0]
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.path == "/v1/chat/completions"
    assert body == {
        "model": "test-model",
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 256,
    }
    assert response.text == "Привет!"
    assert (response.prompt_tokens, response.completion_tokens) == (12, 3)


async def test_trailing_slash_in_base_url_and_zero_max_tokens(session, api):
    # Act
    await client(session, api, base_url=api.base_url + "/", max_tokens=0).complete(
        [{"role": "user", "content": "x"}], temperature=1.0
    )
    # Assert
    request, body = api.requests[0]
    assert request.path == "/v1/chat/completions"
    assert "max_tokens" not in body
    assert body["temperature"] == 1.0


async def test_missing_usage_gives_none_tokens(session, api):
    # Arrange
    api.body = {"choices": [{"message": {"content": "ок"}}]}
    # Act
    response = await client(session, api).complete(
        [{"role": "user", "content": "x"}], temperature=0
    )
    # Assert
    assert response.text == "ок"
    assert response.prompt_tokens is None and response.completion_tokens is None


@pytest.mark.parametrize("status", [429, 500, 401])
async def test_http_error_is_unavailable(session, api, status):
    # Arrange
    api.status = status
    api.body = {"error": {"message": f"key {API_KEY} rejected"}}
    # Act / Assert
    with pytest.raises(LLMUnavailableError) as exc:
        await client(session, api).complete([{"role": "user", "content": "x"}], temperature=0)
    assert str(status) in str(exc.value)
    assert API_KEY not in str(exc.value)


async def test_timeout(session, api):
    # Arrange
    api.delay = 1.0
    # Act / Assert
    with pytest.raises(LLMTimeoutError):
        await client(session, api, timeout=0.2).complete(
            [{"role": "user", "content": "x"}], temperature=0
        )


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"text": "старый формат"}]},
        {"id": "no-choices"},
        [],
    ],
)
async def test_empty_or_unexpected_response(session, api, body):
    # Arrange
    api.body = body
    # Act / Assert
    with pytest.raises(LLMEmptyResponseError):
        await client(session, api).complete([{"role": "user", "content": "x"}], temperature=0)


async def test_non_json_body(session, api):
    # Arrange
    api.raw = b"<html>gateway</html>"
    # Act / Assert
    with pytest.raises(LLMEmptyResponseError):
        await client(session, api).complete([{"role": "user", "content": "x"}], temperature=0)


async def test_connection_refused_is_unavailable(session, unused_tcp_port):
    # Arrange
    dead = LLMClient(
        session,
        base_url=f"http://127.0.0.1:{unused_tcp_port}/v1",
        api_key=API_KEY,
        model="m",
        timeout=1.0,
    )
    # Act / Assert
    with pytest.raises(LLMUnavailableError) as exc:
        await dead.complete([{"role": "user", "content": "x"}], temperature=0)
    assert isinstance(exc.value, LLMError)
    assert API_KEY not in str(exc.value)


def test_json_payload_is_serializable():
    # Arrange / Act / Assert: сообщения с не-ASCII текстом сериализуются без потерь.
    payload = {"messages": [{"role": "user", "content": "привет 👋"}]}
    assert json.loads(json.dumps(payload)) == payload
