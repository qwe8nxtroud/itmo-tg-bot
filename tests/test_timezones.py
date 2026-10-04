"""Часовые пояса: имена IANA и локальное время на переходах летнего времени."""

from datetime import datetime

import pytest

from app.timezones import LocalTimeError, format_local, localize, normalize_timezone


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Europe/Moscow", "Europe/Moscow"),
        ("  europe/moscow ", "Europe/Moscow"),
        ("Asia/Yekaterinburg", "Asia/Yekaterinburg"),
        ("UTC", "UTC"),
    ],
)
def test_known_timezones_are_normalized(name, expected):
    assert normalize_timezone(name) == expected


@pytest.mark.parametrize(
    "name", ["Mars/Olympus", "MSK", "+03:00", "UTC+3", "../../etc/passwd", "Europe/", ""]
)
def test_unknown_timezone_is_rejected(name):
    assert normalize_timezone(name) is None


def test_regular_local_time():
    moment = localize(datetime(2026, 10, 13, 18, 30), "Europe/Moscow")
    assert moment.isoformat() == "2026-10-13T18:30:00+03:00"


def test_ambiguous_time_on_fall_back_is_rejected():
    # 25.10.2026 в Берлине 02:30 бывает дважды (+02:00 и +01:00).
    with pytest.raises(LocalTimeError) as error:
        localize(datetime(2026, 10, 25, 2, 30), "Europe/Berlin")
    assert error.value.code == "ambiguous_time"


def test_nonexistent_time_on_spring_forward_is_rejected():
    # 29.03.2026 в Берлине после 01:59 сразу 03:00.
    with pytest.raises(LocalTimeError) as error:
        localize(datetime(2026, 3, 29, 2, 30), "Europe/Berlin")
    assert error.value.code == "nonexistent_time"


def test_format_local_shows_zone_and_offset():
    moment = datetime.fromisoformat("2026-10-13T15:30:00+00:00")
    assert format_local(moment, "Europe/Moscow") == "13.10.2026 18:30 (Europe/Moscow, UTC+03:00)"
