"""Агент: ограниченный цикл «модель предлагает — приложение проверяет — MCP выполняет».

На одно сообщение — не больше `max_tool_calls` MCP-вызовов. Инструменты с побочным
эффектом не вызываются из цикла: готовится действие, которое пользователь подтверждает
кнопкой. Владелец, чат и id сообщения берутся только из Telegram update.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.assistant import Assistant, build_messages
from app.llm import LLMClient, LLMError, LLMResponse
from app.mcp_client import McpGateway, ToolCallError, ToolSpec
from app.presenters import confirmation_text, reminder_created_text, tool_result_text
from app.prompts import AGENT
from app.storage import AgentEvent, PendingAction, Storage
from app.timezones import format_offset, normalize_timezone, zone
from app.tool_args import ArgumentError, neutralize, parse_arguments, summarize, validate_arguments
from mcp_server import trust

logger = logging.getLogger("app.agent")

CLARIFY = "ask_clarification"
WEEK_RESOURCE = "schedule://current-week"
CLARIFY_TOOL = {
    "type": "function",
    "function": {
        "name": CLARIFY,
        "description": (
            "Задать пользователю один короткий уточняющий вопрос, если для ответа или действия "
            "не хватает данных: например, не указаны точная дата или время напоминания."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Вопрос пользователю по-русски",
                    "minLength": 1,
                    "maxLength": 300,
                }
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
}
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

UNKNOWN_TOOL_TEXT = (
    "Такого действия у меня нет. Я умею узнавать погоду, показывать расписание и создавать "
    "напоминания — список: /tools."
)
LIMIT_TEXT = (
    "Достигнут лимит: не больше двух обращений к инструментам на одно сообщение. "
    "Разбейте запрос на части."
)
BLOCKED_TEXT = (
    "В данных инструмента встретился текст, похожий на команду. Такие команды я не выполняю, "
    "поэтому действие не подготовлено."
)
BAD_ARGUMENTS_TEXT = "Не получилось выполнить запрос: {message}"
TOOL_FAILED_TEXT = "Не получилось получить данные: {message}"
NOT_FOUND_TEXT = "Действие не найдено или уже недоступно."
EXPIRED_TEXT = "Подтверждение просрочено (оно действует 5 минут). Повторите просьбу."
CANCELLED_TEXT = "Отменено. Напоминание не создано."
ALREADY_CANCELLED_TEXT = "Это действие уже отменено или заменено новым."
IN_PROGRESS_TEXT = "Действие уже выполняется."
FAILED_TEXT = "Это действие не выполнено: {message}"
RETRY_TEXT = "Не получилось создать напоминание: {message} Можно нажать «Подтвердить» ещё раз."


@dataclass(frozen=True)
class AgentRequest:
    """Доверенные данные из Telegram update и текст пользователя."""

    user_id: int
    chat_id: int
    message_id: int
    text: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass(frozen=True)
class AgentReply:
    text: str
    action: PendingAction | None = None


@dataclass
class _Trace:
    """Черновик события аудита, который заполняется по ходу обработки."""

    action: str = "respond"
    tools: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    validation: str = "не требовалась"
    execution: str = "не требовалось"
    reason: str | None = None
    tool_calls: int = 0


class Agent:
    def __init__(
        self,
        assistant: Assistant,
        storage: Storage,
        llm: LLMClient,
        gateway: McpGateway,
        *,
        temperature: float,
        history_max_messages: int,
        history_max_chars: int,
        max_tool_calls: int = 2,
        confirmation_ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._assistant = assistant
        self._storage = storage
        self._llm = llm
        self.gateway = gateway
        self._temperature = temperature
        self._history_max_messages = history_max_messages
        self._history_max_chars = history_max_chars
        self._max_tool_calls = max_tool_calls
        self._ttl = confirmation_ttl
        self._clock = clock

    # --- Сообщение пользователя -----------------------------------------------------

    async def reply(self, request: AgentRequest) -> AgentReply:
        settings = await self._assistant.get_settings(request.chat_id)
        if settings.mode != AGENT.command:
            # Режимы ЛР1 работают как прежде, без инструментов.
            return AgentReply(await self._assistant.answer(request.chat_id, request.text))
        started = time.monotonic()
        trace = _Trace()
        timezone = await self._storage.get_timezone(request.user_id)
        try:
            reply = await self._run(request, timezone, trace)
        except LLMError as exc:
            trace.action, trace.execution = "error", f"ошибка модели ({type(exc).__name__})"
            raise
        finally:
            await self._audit(request.user_id, request.request_id, "message", trace, started)
        await self._storage.append_exchange(
            request.chat_id, request.text, reply.text, keep=self._history_max_messages
        )
        return reply

    async def _run(self, request: AgentRequest, timezone: str | None, trace: _Trace) -> AgentReply:
        now = self._clock()
        local_now = now.astimezone(zone(timezone or "UTC"))
        tools = dict(self.gateway.tools)
        history = await self._storage.get_history(request.chat_id, limit=self._history_max_messages)
        messages: list[dict] = build_messages(
            AGENT,
            history,
            request.text,
            max_messages=self._history_max_messages,
            max_chars=self._history_max_chars,
        )
        messages[0] = {"role": "system", "content": self._system_prompt(local_now, timezone)}
        functions = [spec.as_openai_function() for spec in tools.values()] + [CLARIFY_TOOL]
        last_data: tuple[str, dict] | None = None
        injection = False

        for _ in range(self._max_tool_calls + 1):
            try:
                response = await self._llm.chat(
                    messages, temperature=self._temperature, tools=functions
                )
            except LLMError:
                # Данные уже получены: отвечаем по ним без модели, а не сообщаем о сбое.
                fallback = last_data and tool_result_text(*last_data)
                if not fallback:
                    raise
                trace.execution += "; ответ собран без модели"
                return AgentReply(fallback)
            if not response.tool_calls:
                if trace.tool_calls == 0:
                    trace.reason = "инструменты не нужны: вопрос не требует внешних данных"
                return AgentReply(response.text)

            call = response.tool_calls[0]
            if call.name == CLARIFY:
                return self._clarify(call.arguments, trace)
            spec = tools.get(call.name)
            trace.tools.append(call.name)
            trace.reason = trace.reason or _reason(response, spec)
            if spec is None:
                trace.action, trace.validation = "rejected", "не пройдена: неизвестный инструмент"
                trace.execution = "не выполнялось"
                logger.warning(
                    "Агент %s: модель запросила неизвестный инструмент", request.request_id
                )
                return AgentReply(UNKNOWN_TOOL_TEXT)
            try:
                arguments = validate_arguments(
                    spec, parse_arguments(call.arguments), timezone=timezone, now=local_now
                )
            except ArgumentError as exc:
                trace.action, trace.validation = "rejected", f"не пройдена ({exc.code})"
                trace.execution = "не выполнялось"
                return AgentReply(BAD_ARGUMENTS_TEXT.format(message=exc.message))
            trace.args.append(summarize(arguments, timezone))
            trace.validation = "пройдена"

            if not spec.read_only:
                if injection:
                    trace.action, trace.validation = "rejected", "не пройдена (injection_flagged)"
                    trace.execution = "не выполнялось"
                    return AgentReply(BLOCKED_TEXT)
                return await self._prepare(request, spec, arguments, timezone, now, trace)

            if trace.tool_calls >= self._max_tool_calls:
                break
            trace.action = "call_tool"
            trace.tool_calls += 1
            try:
                data = await self.gateway.call_tool(spec.name, arguments)
            except ToolCallError as exc:
                trace.action, trace.execution = "error", f"ошибка ({exc.code})"
                return AgentReply(TOOL_FAILED_TEXT.format(message=exc.message))
            trace.execution = "успешно"
            data, flagged = neutralize(data)
            if flagged:
                injection = True
                trace.execution += "; в данных скрыт текст, похожий на инструкцию"
                logger.warning(
                    "Агент %s: в результате %s найден текст-инструкция",
                    request.request_id,
                    spec.name,
                )
            if spec.name == "get_weather" and data.get("status") == "ambiguous":
                trace.action = "clarify"
                return AgentReply(tool_result_text(spec.name, data) or "")
            last_data = (spec.name, data)
            messages.append(_assistant_tool_call(call.id, spec.name, arguments))
            messages.append(_tool_message(call.id, spec.name, data))

        trace.action, trace.execution = "limit_exceeded", "остановлено: лимит MCP-вызовов"
        logger.warning("Агент %s: превышен лимит вызовов инструментов", request.request_id)
        return AgentReply(LIMIT_TEXT)

    def _clarify(self, raw: object, trace: _Trace) -> AgentReply:
        trace.action, trace.reason = "clarify", "не хватает данных для действия"
        try:
            question = parse_arguments(raw).get("question")
        except ArgumentError:
            question = None
        if not isinstance(question, str) or not question.strip():
            question = "Уточните, пожалуйста, запрос: каких данных не хватает?"
        return AgentReply(question.strip()[:300])

    async def _prepare(
        self,
        request: AgentRequest,
        spec: ToolSpec,
        arguments: dict,
        timezone: str | None,
        now: datetime,
        trace: _Trace,
    ) -> AgentReply:
        action, created = await self._storage.prepare_action(
            user_id=request.user_id,
            chat_id=request.chat_id,
            source_message_id=request.message_id,
            tool=spec.name,
            arguments=arguments,
            timezone=timezone or "UTC",
            now=now,
            ttl=self._ttl,
        )
        trace.action = "prepare_action"
        trace.execution = "ждёт подтверждения" if created else "повтор update: действие уже готово"
        return AgentReply(confirmation_text(action), action)

    def _system_prompt(self, local_now: datetime, timezone: str | None) -> str:
        if timezone:
            zone_line = f"Часовой пояс пользователя: {timezone} ({format_offset(local_now)})."
        else:
            zone_line = (
                "Часовой пояс пользователя не задан (время ниже — UTC). Для напоминаний его "
                "нужно задать командой /timezone."
            )
        available = ", ".join(self.gateway.tools) if self.gateway.available else ""
        tools_line = (
            f"Доступные инструменты: {available}."
            if available
            else "Инструменты сейчас недоступны: на просьбы о погоде, расписании и "
            "напоминаниях честно отвечай, что функция временно недоступна."
        )
        return AGENT.system_prompt.format(
            now=f"{local_now:%Y-%m-%d %H:%M}",
            weekday=WEEKDAYS[local_now.weekday()],
            offset=format_offset(local_now).removeprefix("UTC"),
            zone_line=zone_line,
            tools_line=tools_line,
        )

    @property
    def temperature(self) -> float:
        return self._temperature

    # --- Команды /timezone, /why, /week ------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    async def get_timezone(self, user_id: int) -> str | None:
        return await self._storage.get_timezone(user_id)

    async def set_timezone(self, user_id: int, name: str) -> str | None:
        """Сохраняет каноническое IANA-имя; None — если такой зоны нет."""
        canonical = normalize_timezone(name)
        if canonical is not None:
            await self._storage.set_timezone(user_id, canonical)
        return canonical

    async def last_event(self, user_id: int) -> AgentEvent | None:
        return await self._storage.last_message_event(user_id)

    async def current_week(self) -> dict:
        return await self.gateway.read_resource(WEEK_RESOURCE)

    # --- Кнопки подтверждения ---------------------------------------------------------

    async def confirm(
        self, action_id: uuid.UUID, *, user_id: int, chat_id: int, request_id: str
    ) -> AgentReply:
        """Выполняет подтверждённое действие. action в ответе — кнопки ещё нужны (повтор)."""
        started = time.monotonic()
        trace = _Trace(action="call_tool", reason="пользователь подтвердил действие")
        try:
            return await self._confirm(action_id, user_id, chat_id, trace)
        finally:
            await self._audit(user_id, request_id, "confirm", trace, started)

    async def _confirm(
        self, action_id: uuid.UUID, user_id: int, chat_id: int, trace: _Trace
    ) -> AgentReply:
        now = self._clock()
        action = await self._storage.claim_action(
            action_id, user_id=user_id, chat_id=chat_id, now=now
        )
        if action is None:
            return AgentReply(await self._explain_unclaimed(action_id, user_id, now, trace))
        trace.tools.append(action.tool)
        trace.args.append(summarize(action.arguments, action.timezone))
        trace.validation = "пройдена"
        token = trust.sign(
            self.gateway.trust_secret,
            trust.TrustedContext(owner_id=user_id, action_id=action.id, timezone=action.timezone),
            action.arguments,
            expires_at=int(now.timestamp()) + trust.TOKEN_TTL_SECONDS,
        )
        trace.tool_calls = 1
        try:
            result = await self.gateway.call_tool(
                action.tool, action.arguments, meta={trust.META_KEY: token}
            )
        except ToolCallError as exc:
            trace.action, trace.execution = "error", f"ошибка ({exc.code})"
            if exc.retryable and now < action.expires_at:
                await self._storage.finish_action(action.id, status="pending")
                return AgentReply(RETRY_TEXT.format(message=exc.message), action)
            await self._storage.finish_action(
                action.id, status="failed", result={"error": exc.code, "message": exc.message}
            )
            return AgentReply(FAILED_TEXT.format(message=exc.message))
        await self._storage.finish_action(action.id, status="done", result=result)
        trace.execution = "успешно" if result.get("created", True) else "повтор: запись уже была"
        return AgentReply(reminder_created_text(result))

    async def _explain_unclaimed(
        self, action_id: uuid.UUID, user_id: int, now: datetime, trace: _Trace
    ) -> str:
        """Почему действие нельзя выполнить: чужое, выполнено, отменено или просрочено."""
        trace.action, trace.validation, trace.execution = (
            "rejected",
            "не пройдена",
            "не выполнялось",
        )
        action = await self._storage.get_action(action_id, user_id=user_id)
        if action is None:
            trace.validation = "не пройдена (действие не найдено)"
            return NOT_FOUND_TEXT
        trace.tools.append(action.tool)
        if action.status == "done" and action.result:
            trace.validation = "не пройдена (повторное подтверждение)"
            return reminder_created_text({**action.result, "created": False})
        if action.status == "executing":
            trace.validation = "не пройдена (уже выполняется)"
            return IN_PROGRESS_TEXT
        if action.status == "pending" and action.expires_at <= now:
            await self._storage.expire_action(action_id, user_id=user_id, now=now)
            trace.validation = "не пройдена (просрочено)"
            return EXPIRED_TEXT
        if action.status == "expired":
            trace.validation = "не пройдена (просрочено)"
            return EXPIRED_TEXT
        if action.status == "failed":
            trace.validation = "не пройдена (действие завершилось ошибкой)"
            return FAILED_TEXT.format(message=(action.result or {}).get("message", "ошибка."))
        trace.validation = "не пройдена (действие отменено)"
        return ALREADY_CANCELLED_TEXT

    async def cancel(
        self, action_id: uuid.UUID, *, user_id: int, chat_id: int, request_id: str
    ) -> AgentReply:
        started = time.monotonic()
        trace = _Trace(action="rejected", reason="пользователь отменил действие")
        try:
            if await self._storage.cancel_action(action_id, user_id=user_id, chat_id=chat_id):
                trace.validation, trace.execution = "пройдена", "отменено пользователем"
                return AgentReply(CANCELLED_TEXT)
            now = self._clock()
            return AgentReply(await self._explain_unclaimed(action_id, user_id, now, trace))
        finally:
            await self._audit(user_id, request_id, "cancel", trace, started)

    # --- Аудит ------------------------------------------------------------------------

    async def _audit(
        self, user_id: int, request_id: str, kind: str, trace: _Trace, started: float
    ) -> None:
        duration = int((time.monotonic() - started) * 1000)
        event = AgentEvent(
            request_id=request_id,
            kind=kind,
            action=trace.action,
            tools=", ".join(trace.tools) or None,
            args_summary="; ".join(trace.args) or None,
            validation=trace.validation,
            execution=trace.execution,
            reason=trace.reason,
            tool_calls=trace.tool_calls,
            duration_ms=duration,
            created_at=self._clock(),
        )
        try:
            await self._storage.add_event(user_id, event)
        except Exception:
            # Сбой аудита не должен отменять ответ пользователю; в журнал — без ID.
            logger.exception("Агент %s: не удалось записать событие аудита", request_id)
        logger.info(
            "Агент %s: %s, инструменты: %s, вызовов MCP: %d, проверка: %s, выполнение: %s, %d мс",
            request_id,
            trace.action,
            event.tools or "—",
            trace.tool_calls,
            trace.validation,
            trace.execution,
            duration,
        )


def _reason(response: LLMResponse, spec: ToolSpec | None) -> str:
    """Краткое объяснение выбора: пояснение модели, если она его дала, иначе назначение."""
    if response.text:
        return " ".join(response.text.split())[:200]
    if spec is None:
        return "модель предложила инструмент, которого нет у сервера"
    return f"нужны данные инструмента: {spec.summary.lower()}"


def _assistant_tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


def _tool_message(call_id: str, name: str, data: dict) -> dict:
    # Результат помечен как данные внешнего источника, а не как инструкция.
    payload = {"source": f"MCP-инструмент {name}", "data": data}
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(payload, ensure_ascii=False),
    }
