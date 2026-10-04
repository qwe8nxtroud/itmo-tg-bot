"""Текущая погода через Open-Meteo: геокодирование названия и запрос текущих данных.

Open-Meteo выбран потому, что он бесплатный, не требует ключа, отвечает из РФ и даёт
геокодирование на русском языке с регионом и населением, по которым различаются
одноимённые города.
"""

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

import aiohttp
from pydantic import BaseModel, Field

from mcp_server.errors import ToolFailure

logger = logging.getLogger("mcp_server.weather")

# Сервер обращается только к этим двум адресам; модель и пользователь их не задают.
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE = "Open-Meteo"
MAX_CANDIDATES = 5
# Город выбирается среди одноимённых, только если он заметно крупнее всех остальных.
DOMINANCE_RATIO = 10

# Коды погоды WMO, которые возвращает Open-Meteo.
WMO_CONDITIONS = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "туман с изморосью",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "слабая ледяная морось",
    57: "ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "слабый ледяной дождь",
    67: "ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежные зёрна",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "небольшой снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с небольшим градом",
    99: "гроза с сильным градом",
}


class Location(BaseModel):
    name: str
    country: str
    admin1: str | None = Field(description="Регион (область, штат)")
    latitude: float
    longitude: float
    timezone: str = Field(description="Часовой пояс места (IANA)")


class Candidate(BaseModel):
    name: str
    country: str
    admin1: str | None


class CurrentWeather(BaseModel):
    temperature_c: float = Field(description="Температура воздуха, °C")
    condition_code: int = Field(description="Код погоды WMO")
    condition: str = Field(description="Описание погоды по-русски")
    wind_speed: float
    wind_speed_unit: Literal["m/s"]
    observed_at: str = Field(description="Время наблюдения, ISO 8601 со смещением")
    timezone: str = Field(description="Часовой пояс времени наблюдения (IANA)")


class WeatherResult(BaseModel):
    status: Literal["ok", "ambiguous"] = Field(
        description="ok — погода получена; ambiguous — несколько одноимённых городов, "
        "нужно выбрать из candidates"
    )
    source: str
    location: Location | None = None
    current: CurrentWeather | None = None
    candidates: list[Candidate] = []


class JsonFetcher(Protocol):
    async def get_json(self, url: str, params: Mapping[str, str | int]) -> object: ...


class HttpJsonFetcher:
    """GET с конечным тайм-аутом и ограниченным повтором (операции только читают)."""

    def __init__(self, *, timeout: float, retries: int = 1) -> None:
        self._timeout = timeout
        self._retries = retries
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def get_json(self, url: str, params: Mapping[str, str | int]) -> object:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            try:
                async with self._session.get(url, params=dict(params)) as response:
                    if response.status >= 500 and not last:
                        raise _Retry(f"HTTP {response.status}")
                    if response.status >= 400:
                        raise ToolFailure(
                            "weather_unavailable", "Погодный сервис сейчас недоступен."
                        )
                    try:
                        return await response.json(content_type=None)
                    except ValueError:
                        raise ToolFailure(
                            "weather_bad_response", "Погодный сервис вернул некорректный ответ."
                        ) from None
            except TimeoutError:
                logger.warning("Погода: тайм-аут %.1f с, попытка %d", self._timeout, attempt + 1)
                if last:
                    raise ToolFailure(
                        "weather_timeout", "Погодный сервис не ответил вовремя."
                    ) from None
            except (_Retry, aiohttp.ClientError, OSError) as exc:
                logger.warning("Погода: %s, попытка %d", type(exc).__name__, attempt + 1)
                if last:
                    raise ToolFailure(
                        "weather_unavailable", "Погодный сервис сейчас недоступен."
                    ) from None
            await asyncio.sleep(0.3 * (attempt + 1))
        raise AssertionError("недостижимо")


class _Retry(Exception):
    """Временная ошибка сервиса, после которой допустим повтор."""


class OpenMeteoWeather:
    def __init__(self, fetcher: JsonFetcher) -> None:
        self._fetcher = fetcher

    async def current(self, query: str) -> WeatherResult:
        name, *hints = [part.strip() for part in query.split(",") if part.strip()] or [""]
        places = _parse_places(
            await self._fetcher.get_json(
                GEOCODING_URL, {"name": name, "count": 20, "language": "ru", "format": "json"}
            )
        )
        places = _filter_places(places, name, hints)
        if not places:
            raise ToolFailure("city_not_found", "Город не найден. Проверьте название.")
        chosen = _choose(places)
        if chosen is None:
            return WeatherResult(
                status="ambiguous",
                source=SOURCE,
                candidates=[
                    Candidate(name=p["name"], country=p["country"], admin1=p["admin1"])
                    for p in places[:MAX_CANDIDATES]
                ],
            )
        body = await self._fetcher.get_json(
            FORECAST_URL,
            {
                "latitude": str(chosen["latitude"]),
                "longitude": str(chosen["longitude"]),
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "timezone": "auto",
                "wind_speed_unit": "ms",
            },
        )
        return WeatherResult(
            status="ok",
            source=SOURCE,
            location=Location(**{key: chosen[key] for key in Location.model_fields}),
            current=_parse_current(body),
        )


def _normalize(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").replace("-", " ").split())


def _bad_response() -> ToolFailure:
    return ToolFailure("weather_bad_response", "Погодный сервис вернул некорректный ответ.")


def _parse_places(body: object) -> list[dict]:
    if not isinstance(body, dict):
        raise _bad_response()
    results = body.get("results", [])
    if not isinstance(results, list):
        raise _bad_response()
    places = []
    for item in results:
        if not isinstance(item, dict):
            raise _bad_response()
        try:
            name, latitude, longitude = item["name"], item["latitude"], item["longitude"]
            zone = item.get("timezone") or "UTC"
        except KeyError:
            raise _bad_response() from None
        if not isinstance(name, str) or not all(
            isinstance(value, int | float) and not isinstance(value, bool)
            for value in (latitude, longitude)
        ):
            raise _bad_response()
        population = item.get("population")
        places.append(
            {
                "name": name,
                "country": item.get("country") or item.get("country_code") or "—",
                "country_code": item.get("country_code") or "",
                "admin1": item.get("admin1") or None,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "timezone": zone if isinstance(zone, str) else "UTC",
                "feature_code": item.get("feature_code") or "",
                "population": population if isinstance(population, int) else 0,
            }
        )
    return places


def _filter_places(places: list[dict], name: str, hints: list[str]) -> list[dict]:
    """Населённые пункты с тем же названием, уточнённые страной или регионом."""
    settlements = [p for p in places if p["feature_code"].startswith("PPL")]
    exact = [p for p in settlements if _normalize(p["name"]) == _normalize(name)]
    selected = exact or settlements
    for hint in map(_normalize, hints):
        selected = [
            p
            for p in selected
            if any(
                hint and (hint == _normalize(value) or hint in _normalize(value))
                for value in (p["country"], p["country_code"], p["admin1"] or "")
            )
        ]
    # Одинаковые для пользователя варианты (название, страна, регион) склеиваются в крупнейший.
    unique: dict[tuple, dict] = {}
    for place in sorted(selected, key=lambda p: p["population"], reverse=True):
        unique.setdefault((place["name"], place["country"], place["admin1"]), place)
    return list(unique.values())


def _choose(places: list[dict]) -> dict | None:
    if len(places) == 1:
        return places[0]
    first, second = places[0]["population"], places[1]["population"]
    if first > 0 and first >= DOMINANCE_RATIO * second:
        return places[0]
    return None


def _parse_current(body: object) -> CurrentWeather:
    try:
        current = body["current"]  # type: ignore[index]
        offset = body["utc_offset_seconds"]  # type: ignore[index]
        zone = body["timezone"]  # type: ignore[index]
        observed = datetime.fromisoformat(current["time"])
        temperature = current["temperature_2m"]
        code = current["weather_code"]
        wind = current["wind_speed_10m"]
    except (TypeError, KeyError, ValueError):
        raise _bad_response() from None
    numbers_ok = all(
        isinstance(value, int | float) and not isinstance(value, bool)
        for value in (temperature, wind, offset)
    )
    if not numbers_ok or type(code) is not int or not isinstance(zone, str):
        raise _bad_response()
    observed = observed.replace(tzinfo=timezone(timedelta(seconds=offset)))
    return CurrentWeather(
        temperature_c=float(temperature),
        condition_code=code,
        condition=WMO_CONDITIONS.get(code, f"код погоды {code}"),
        wind_speed=float(wind),
        wind_speed_unit="m/s",
        observed_at=observed.isoformat(),
        timezone=zone,
    )
