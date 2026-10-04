"""Доверенный контекст вызова: владелец и ключ действия, подписанные приложением.

Модель формирует только аргументы инструмента. Владельца напоминания и id подтверждённого
действия приложение берёт из Telegram update и своей БД, подписывает HMAC-SHA256 и кладёт
в `_meta` запроса. Секрет знает только бот и запущенный им дочерний процесс MCP-сервера,
поэтому посторонний MCP-клиент не может создать напоминание от чужого имени.
"""

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass

META_KEY = "ru.itmo.tgbot/trusted-context"
SECRET_ENV = "MCP_TRUST_SECRET"
TOKEN_TTL_SECONDS = 60


class UntrustedContextError(Exception):
    """Контекст отсутствует, подделан, просрочен или не относится к этим аргументам."""


@dataclass(frozen=True)
class TrustedContext:
    owner_id: int
    action_id: uuid.UUID
    timezone: str


def arguments_digest(arguments: dict) -> str:
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sign(secret: bytes, context: TrustedContext, arguments: dict, *, expires_at: int) -> str:
    payload = {
        "v": 1,
        "owner_id": context.owner_id,
        "action_id": str(context.action_id),
        "timezone": context.timezone,
        "args": arguments_digest(arguments),
        "exp": expires_at,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"{body}.{_mac(secret, body)}"


def verify(secret: bytes, token: object, arguments: dict, *, now: int) -> TrustedContext:
    if not secret:
        raise UntrustedContextError("сервер запущен без секрета доверенного контекста")
    if not isinstance(token, str) or token.count(".") != 1:
        raise UntrustedContextError("нет доверенного контекста вызова")
    body, mac = token.split(".")
    if not hmac.compare_digest(mac, _mac(secret, body)):
        raise UntrustedContextError("подпись доверенного контекста неверна")
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
        owner_id = payload["owner_id"]
        action_id = uuid.UUID(payload["action_id"])
        timezone = payload["timezone"]
        expires_at = payload["exp"]
        digest = payload["args"]
        version = payload["v"]
    except (ValueError, KeyError, TypeError):
        raise UntrustedContextError("доверенный контекст повреждён") from None
    if version != 1 or type(owner_id) is not int or owner_id <= 0 or not isinstance(timezone, str):
        raise UntrustedContextError("доверенный контекст повреждён")
    if type(expires_at) is not int or expires_at < now:
        raise UntrustedContextError("доверенный контекст просрочен")
    if not hmac.compare_digest(str(digest), arguments_digest(arguments)):
        raise UntrustedContextError("аргументы не совпадают с подтверждённым действием")
    return TrustedContext(owner_id=owner_id, action_id=action_id, timezone=timezone)


def _mac(secret: bytes, body: str) -> str:
    return hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
