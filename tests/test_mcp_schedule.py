"""Расписание: занятия на дату, чередование недель, исключения и границы недели."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcp_server.errors import ToolFailure
from mcp_server.schedule import ScheduleSource, parse_schedule_date
from tests.fakes import day, schedule_data

REPO_SCHEDULE = Path(__file__).resolve().parent.parent / "data" / "schedule.json"


def test_lessons_on_date():
    # Arrange
    source = ScheduleSource(schedule_data())
    # Act: 2026-10-12 — понедельник первой недели цикла (6 недель от cycle_start).
    result = source.day(day("2026-10-12"))
    # Assert
    assert result.model_dump() == {
        "date": "2026-10-12",
        "timezone": "Europe/Moscow",
        "lessons": [
            {
                "start": "2026-10-12T10:00:00+03:00",
                "end": "2026-10-12T11:30:00+03:00",
                "title": "Матанализ",
                "location": "ауд. 101",
            }
        ],
    }


def test_second_week_of_cycle_has_other_lessons():
    source = ScheduleSource(schedule_data())
    assert [lesson.title for lesson in source.lessons_on(day("2026-10-19"))] == ["Физика"]


def test_no_lessons_on_sunday():
    source = ScheduleSource(schedule_data())
    assert source.day(day("2026-10-18")).lessons == []


@pytest.mark.parametrize("value", ["2026-11-04", "2026-08-31", "2027-01-04"])
def test_holiday_and_dates_outside_term_are_empty(value):
    # 04.11 — праздник (среда), 31.08 и 04.01 — понедельники вне семестра.
    assert ScheduleSource(schedule_data()).lessons_on(day(value)) == []


def test_lessons_sorted_by_start():
    # Arrange
    late = {"start": "15:00", "end": "16:30", "title": "Поздно", "location": ""}
    early = {"start": "08:20", "end": "09:50", "title": "Рано", "location": ""}
    source = ScheduleSource(schedule_data(weeks=[{"friday": [late, early]}]))
    # Act
    lessons = source.lessons_on(day("2026-10-16"))
    # Assert
    assert [lesson.title for lesson in lessons] == ["Рано", "Поздно"]


@pytest.mark.parametrize(
    ("now", "week_start", "week_end"),
    [
        # Граница месяца: среда 30.09 → неделя 28.09–04.10.
        (datetime(2026, 9, 30, 12, 0, tzinfo=UTC), "2026-09-28", "2026-10-04"),
        # Граница года: четверг 31.12.2026 → неделя 28.12.2026–03.01.2027.
        (datetime(2026, 12, 31, 12, 0, tzinfo=UTC), "2026-12-28", "2027-01-03"),
        # Воскресенье 23:30 UTC — уже понедельник 05.10 по Москве: новая неделя.
        (datetime(2026, 10, 4, 23, 30, tzinfo=UTC), "2026-10-05", "2026-10-11"),
    ],
)
def test_week_crosses_month_and_year_boundaries(now, week_start, week_end):
    # Arrange
    source = ScheduleSource(schedule_data(term_end="2027-01-31"))
    # Act
    week = source.week(now)
    # Assert
    assert (week.week_start, week.week_end) == (week_start, week_end)
    assert [d.weekday for d in week.days][0] == "понедельник"
    assert len(week.days) == 7
    assert [d.date for d in week.days] == sorted(d.date for d in week.days)


def test_week_groups_lessons_by_date():
    source = ScheduleSource(schedule_data())
    week = source.week(datetime(2026, 10, 14, 9, 0, tzinfo=UTC))
    by_date = {d.date: [lesson.title for lesson in d.lessons] for d in week.days}
    assert by_date["2026-10-12"] == ["Матанализ"]
    assert by_date["2026-10-14"] == ["Алгебра"]
    assert by_date["2026-10-18"] == []


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("12.10.2026", "invalid_date"),
        ("2026-10-12T10:00", "invalid_date"),
        ("2026-02-30", "invalid_date"),
        ("2030-01-01", "date_out_of_range"),
    ],
)
def test_bad_dates_are_rejected(value, code):
    with pytest.raises(ToolFailure) as error:
        parse_schedule_date(value, today=day("2026-10-12"))
    assert error.value.code == code


def test_broken_schedule_file_is_reported_without_details(tmp_path):
    # Arrange
    path = tmp_path / "schedule.json"
    path.write_text('{"timezone": "Mars/Base"}', encoding="utf-8")
    # Act
    with pytest.raises(ToolFailure) as error:
        ScheduleSource.load(path)
    # Assert
    assert (error.value.code, error.value.message) == (
        "schedule_unavailable",
        "Расписание сейчас недоступно.",
    )


def test_repository_schedule_is_valid_and_covers_two_weeks():
    source = ScheduleSource.load(REPO_SCHEDULE)
    assert source.timezone == "Europe/Moscow"
    assert source.lessons_on(day("2026-10-12")) != source.lessons_on(day("2026-10-19"))
    assert source.lessons_on(day("2026-10-18")) == []
