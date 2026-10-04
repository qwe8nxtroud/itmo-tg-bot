"""Общие фикстуры: агент с настоящим MCP-сервером в памяти и фейковыми границами."""

import json
import secrets
import socket
import time
import uuid
from dataclasses import dataclass

import pytest

from app.agent import Agent, AgentRequest
from app.assistant import Assistant
from app.config import Settings
from app.mcp_client import McpGateway
from scripts.common import CommandError, run_command
from tests.fakes import (
    TRUST_SECRET,
    Clock,
    FakeLLM,
    FakeStorage,
    FakeWeather,
    MemoryReminderStore,
    build_test_server,
)

ALICE, BOB = 1001, 1002


@dataclass
class Env:
    agent: Agent
    llm: FakeLLM
    storage: FakeStorage
    weather: FakeWeather
    reminders: MemoryReminderStore
    clock: Clock
    gateway: McpGateway

    async def say(self, text_: str, *, user: int = ALICE, message_id: int | None = None):
        request = AgentRequest(
            user_id=user,
            chat_id=user,
            message_id=message_id or uuid.uuid4().int % 10**9,
            text=text_,
        )
        return await self.agent.reply(request)

    async def confirm(self, action_id, *, user: int = ALICE):
        return await self.agent.confirm(action_id, user_id=user, chat_id=user, request_id="t")

    async def cancel(self, action_id, *, user: int = ALICE):
        return await self.agent.cancel(action_id, user_id=user, chat_id=user, request_id="t")

    async def last_event(self, user: int = ALICE):
        return await self.storage.last_message_event(user)


@pytest.fixture
async def make_env():
    gateways: list[McpGateway] = []

    async def make(
        *responses,
        weather=None,
        schedule=None,
        reminders=None,
        timezone="Europe/Moscow",
        connect=True,
    ) -> Env:
        clock = Clock()
        weather = weather or FakeWeather()
        reminders = reminders or MemoryReminderStore()
        server, _ = build_test_server(
            weather=weather, schedule=schedule, reminders=reminders, clock=clock
        )
        gateway = McpGateway(server, trust_secret=TRUST_SECRET, reconnect_delay=60)
        if connect:
            await gateway.start(wait=10)
            gateways.append(gateway)
        storage = FakeStorage()
        if timezone:
            storage.timezones[ALICE] = timezone
            storage.timezones[BOB] = timezone
        llm = FakeLLM(*responses)
        assistant = Assistant(storage, llm, history_max_messages=20, history_max_chars=12000)  # type: ignore[arg-type]
        agent = Agent(
            assistant,
            storage,  # type: ignore[arg-type]
            llm,  # type: ignore[arg-type]
            gateway,
            temperature=0.2,
            history_max_messages=20,
            history_max_chars=12000,
            clock=clock,
        )
        return Env(agent, llm, storage, weather, reminders, clock, gateway)

    yield make
    for gateway in gateways:
        await gateway.close()


@pytest.fixture
def database():
    name = "itmo-test-" + secrets.token_hex(6)
    volume = name + "-data"
    password = secrets.token_urlsafe(24)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def docker(*args):
        return run_command(["docker", *args], label="Интеграционный Docker-тест", timeout=180)

    def wait_until_ready():
        deadline = time.monotonic() + 60
        while True:
            try:
                docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "bot", "-d", "bot")
                return
            except CommandError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    docker("info")
    docker("volume", "create", volume)
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            "POSTGRES_DB=bot",
            "-e",
            "POSTGRES_USER=bot",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "--mount",
            f"type=volume,source={volume},target=/var/lib/postgresql/data",
            "postgres:16-bookworm",
        )
        info = json.loads(docker("inspect", name))[0]
        assert int(info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]) == port
        wait_until_ready()
        yield (
            Settings(
                bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                postgres_password=password,
                postgres_port=port,
            ),
            docker,
            wait_until_ready,
            name,
        )
    finally:
        try:
            docker("rm", "-f", name)
        finally:
            docker("volume", "rm", volume)
