"""Часовые пояса IANA: проверка имени и перевод локального времени без угадывания.

Список зон берётся из пакета tzdata, а не из системы, поэтому одинаков в Linux, macOS и
Windows. Переход на летнее время проверяется явно: неоднозначное или несуществующее
локальное время не нормализуется молча, а возвращается пользователю на уточнение.
"""

import zoneinfo
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib import resources
from zoneinfo import ZoneInfo


def use_bundled_tzdata() -> None:
    """Правила зон — только из пакета tzdata, без системной базы: одинаково на всех ОС."""
    zoneinfo.reset_tzpath(to=[])
    ZoneInfo.clear_cache()


use_bundled_tzdata()

TIMEZONE_HINT = (
    "Укажите часовой пояс из базы IANA в формате Регион/Город, например "
    "/timezone Europe/Moscow или /timezone Asia/Yekaterinburg."
)


class LocalTimeError(ValueError):
    """Локальное время нельзя однозначно перевести в момент времени."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@cache
def _zones() -> dict[str, str]:
    """Канонические имена зон tzdata: ключ — имя в нижнем регистре."""
    names = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8").split()
    return {name.casefold(): name for name in names}


def normalize_timezone(name: str) -> str | None:
    """Каноническое IANA-имя (`europe/moscow` → `Europe/Moscow`) или None, если зоны нет."""
    return _zones().get(name.strip().casefold())


def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def localize(naive: datetime, timezone: str) -> datetime:
    """Привязывает локальное время к зоне; отклоняет время в переходах на летнее время."""
    tz = ZoneInfo(timezone)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset():
        return first
    # Время, которого не было на часах, после круга через UTC не совпадает с исходным.
    round_trip = first.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if round_trip != naive:
        raise LocalTimeError(
            "nonexistent_time",
            f"Время {naive:%d.%m.%Y %H:%M} не существует в зоне {timezone}: "
            "в этот момент часы переводятся вперёд. Укажите другое время.",
        )
    raise LocalTimeError(
        "ambiguous_time",
        f"Время {naive:%d.%m.%Y %H:%M} в зоне {timezone} встречается дважды из-за перевода "
        "часов. Укажите другое время.",
    )


def format_offset(moment: datetime) -> str:
    offset = moment.utcoffset() or timedelta(0)
    sign = "-" if offset < timedelta(0) else "+"
    minutes = abs(int(offset.total_seconds())) // 60
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def format_local(moment: datetime, timezone: str) -> str:
    local = moment.astimezone(ZoneInfo(timezone))
    return f"{local:%d.%m.%Y %H:%M} ({timezone}, {format_offset(local)})"
