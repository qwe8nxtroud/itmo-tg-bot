"""Погода: выбор города, неоднозначность, ошибки и устойчивость HTTP-клиента."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from mcp_server.errors import ToolFailure
from mcp_server.weather import FORECAST_URL, GEOCODING_URL, HttpJsonFetcher, OpenMeteoWeather


def place(name, country, admin1, population, code="PPLA", lat=55.0, lon=37.0):
    return {
        "name": name,
        "country": country,
        "country_code": {"Россия": "RU", "Грузия": "GE", "Беларусь": "BY"}.get(country, "XX"),
        "admin1": admin1,
        "latitude": lat,
        "longitude": lon,
        "timezone": "Europe/Moscow",
        "feature_code": code,
        "population": population,
    }


FORECAST = {
    "timezone": "Europe/Moscow",
    "utc_offset_seconds": 10800,
    "current": {
        "time": "2026-10-12T09:00",
        "temperature_2m": 7.4,
        "weather_code": 61,
        "wind_speed_10m": 3.1,
    },
}


class ScriptedFetcher:
    """Отвечает заранее заданными телами на адреса геокодера и прогноза."""

    def __init__(self, geocoding: object, forecast: object = FORECAST) -> None:
        self.responses = {GEOCODING_URL: geocoding, FORECAST_URL: forecast}
        self.requests: list[tuple[str, dict]] = []

    async def get_json(self, url, params):
        self.requests.append((url, dict(params)))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


async def test_weather_for_existing_city():
    # Arrange: город и одноимённое село в 10+ раз меньше — выбор однозначен.
    fetcher = ScriptedFetcher(
        {
            "results": [
                place("Казань", "Россия", "Татарстан", 1_243_500, lat=55.79, lon=49.12),
                place("Казань", "Россия", "Кировская область", 0, code="PPL"),
            ]
        }
    )
    # Act
    result = await OpenMeteoWeather(fetcher).current("Казань")
    # Assert
    assert result.status == "ok"
    assert (result.location.name, result.location.admin1) == ("Казань", "Татарстан")
    assert result.current.model_dump() == {
        "temperature_c": 7.4,
        "condition_code": 61,
        "condition": "небольшой дождь",
        "wind_speed": 3.1,
        "wind_speed_unit": "m/s",
        "observed_at": "2026-10-12T09:00:00+03:00",
        "timezone": "Europe/Moscow",
    }
    assert fetcher.requests[1][1]["latitude"] == "55.79"
    assert fetcher.requests[0][1]["name"] == "Казань"


async def test_ambiguous_city_returns_candidates_instead_of_first_result():
    # Arrange: четыре Кировска сопоставимого размера.
    fetcher = ScriptedFetcher(
        {
            "results": [
                place("Кировск", "Россия", "Ленинградская область", 24_678),
                place("Кировск", "Беларусь", "Могилёвская область", 7_911),
                place("Кировск", "Россия", "Мурманская область", 29_605, code="PPL"),
            ]
        }
    )
    # Act
    result = await OpenMeteoWeather(fetcher).current("Кировск")
    # Assert
    assert result.status == "ambiguous"
    assert result.current is None
    assert [(c.country, c.admin1) for c in result.candidates] == [
        ("Россия", "Мурманская область"),
        ("Россия", "Ленинградская область"),
        ("Беларусь", "Могилёвская область"),
    ]
    assert len(fetcher.requests) == 1, "погода не запрашивается, пока город не выбран"


async def test_country_or_region_after_comma_resolves_ambiguity():
    # Arrange
    fetcher = ScriptedFetcher(
        {
            "results": [
                place("Кировск", "Россия", "Ленинградская область", 24_678),
                place("Кировск", "Россия", "Мурманская область", 29_605, code="PPL"),
            ]
        }
    )
    # Act
    result = await OpenMeteoWeather(fetcher).current("Кировск, Мурманская область")
    # Assert
    assert result.status == "ok"
    assert result.location.admin1 == "Мурманская область"


async def test_non_settlements_are_ignored():
    # Аэропорт с тем же названием не делает запрос неоднозначным.
    fetcher = ScriptedFetcher(
        {
            "results": [
                place("Тбилиси", "Грузия", "Тбилиси", 1_049_498, code="PPLC"),
                place("Тбилиси", "Грузия", "Квемо-Картли", 0, code="AIRP"),
            ]
        }
    )
    result = await OpenMeteoWeather(fetcher).current("Тбилиси, Грузия")
    assert (result.status, result.location.country) == ("ok", "Грузия")


@pytest.mark.parametrize("body", [{"results": []}, {"generationtime_ms": 0.5}])
async def test_city_not_found(body):
    with pytest.raises(ToolFailure) as error:
        await OpenMeteoWeather(ScriptedFetcher(body)).current("Нетгорода")
    assert (error.value.code, error.value.message) == (
        "city_not_found",
        "Город не найден. Проверьте название.",
    )


@pytest.mark.parametrize(
    "forecast",
    [
        {"current": {"time": "2026-10-12T09:00"}},  # нет обязательных полей
        {**FORECAST, "current": {**FORECAST["current"], "temperature_2m": "тепло"}},
        ["не объект"],
    ],
)
async def test_bad_forecast_response(forecast):
    fetcher = ScriptedFetcher({"results": [place("Казань", "Россия", "Татарстан", 1)]}, forecast)
    with pytest.raises(ToolFailure) as error:
        await OpenMeteoWeather(fetcher).current("Казань")
    assert error.value.code == "weather_bad_response"


async def test_bad_geocoding_response():
    with pytest.raises(ToolFailure) as error:
        await OpenMeteoWeather(ScriptedFetcher({"results": "много"})).current("Казань")
    assert error.value.code == "weather_bad_response"


# --- HTTP-клиент: тайм-аут, повтор, неверный JSON ----------------------------------------


class Service:
    """Локальный HTTP-сервер, имитирующий сбои погодного API."""

    def __init__(self, *behaviours: str) -> None:
        self.behaviours = list(behaviours)
        self.hits = 0

    async def handle(self, request: web.Request) -> web.Response:
        self.hits += 1
        behaviour = self.behaviours.pop(0) if self.behaviours else "ok"
        if behaviour == "slow":
            await asyncio.sleep(1)
        if behaviour == "500":
            return web.Response(status=500)
        if behaviour == "404":
            return web.Response(status=404)
        if behaviour == "garbage":
            return web.Response(text="<html>не JSON</html>")
        return web.json_response({"ok": True})


@pytest.fixture
async def service():
    created: list[TestServer] = []

    async def start(*behaviours: str) -> tuple[Service, str]:
        state = Service(*behaviours)
        app = web.Application()
        app.router.add_get("/", state.handle)
        server = TestServer(app)
        await server.start_server()
        created.append(server)
        return state, str(server.make_url("/"))

    yield start
    for server in created:
        await server.close()


async def test_timeout_after_one_retry(service):
    # Arrange
    state, url = await service("slow", "slow")
    fetcher = HttpJsonFetcher(timeout=0.2, retries=1)
    # Act
    with pytest.raises(ToolFailure) as error:
        await fetcher.get_json(url, {})
    await fetcher.close()
    # Assert
    assert error.value.code == "weather_timeout"
    assert state.hits == 2, "одна исходная попытка и один повтор"


async def test_server_error_is_retried_once(service):
    state, url = await service("500", "ok")
    fetcher = HttpJsonFetcher(timeout=2, retries=1)
    assert await fetcher.get_json(url, {}) == {"ok": True}
    await fetcher.close()
    assert state.hits == 2


async def test_client_error_is_not_retried(service):
    state, url = await service("404")
    fetcher = HttpJsonFetcher(timeout=2, retries=1)
    with pytest.raises(ToolFailure) as error:
        await fetcher.get_json(url, {})
    await fetcher.close()
    assert (error.value.code, state.hits) == ("weather_unavailable", 1)


async def test_bad_response_not_json(service):
    _, url = await service("garbage")
    fetcher = HttpJsonFetcher(timeout=2, retries=1)
    with pytest.raises(ToolFailure) as error:
        await fetcher.get_json(url, {})
    await fetcher.close()
    assert error.value.code == "weather_bad_response"


async def test_connection_refused_is_unavailable():
    # На Windows отказ соединения с localhost приходит через ~2 с: запас по тайм-ауту.
    fetcher = HttpJsonFetcher(timeout=5, retries=0)
    with pytest.raises(ToolFailure) as error:
        await fetcher.get_json("http://127.0.0.1:9/", {})
    await fetcher.close()
    assert error.value.code == "weather_unavailable"
