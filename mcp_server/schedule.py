"""Учебное расписание из файла: занятия на дату и неделя с понедельника по воскресенье."""

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mcp_server.errors import ToolFailure

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
# Дальше этого горизонта расписание не планируется: защита от опечаток в годе.
MAX_DAYS_FROM_TODAY = 400
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# --- Выходные данные инструмента и ресурса -------------------------------------------


class Lesson(BaseModel):
    start: str = Field(description="Начало занятия, ISO 8601 со смещением")
    end: str = Field(description="Окончание занятия, ISO 8601 со смещением")
    title: str = Field(description="Название дисциплины и вид занятия")
    location: str = Field(description="Аудитория или формат проведения")


class DaySchedule(BaseModel):
    date: str = Field(description="Дата YYYY-MM-DD")
    timezone: str = Field(description="Часовой пояс расписания (IANA)")
    lessons: list[Lesson] = Field(description="Занятия по времени начала; пустой — занятий нет")


class WeekDay(BaseModel):
    date: str
    weekday: str
    lessons: list[Lesson]


class WeekSchedule(BaseModel):
    timezone: str
    week_start: str = Field(description="Понедельник недели, YYYY-MM-DD")
    week_end: str = Field(description="Воскресенье недели, YYYY-MM-DD")
    days: list[WeekDay]


# --- Формат файла расписания ---------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LessonTemplate(_Strict):
    start: time
    end: time
    title: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def _ordered(self) -> "LessonTemplate":
        if self.end <= self.start:
            raise ValueError("занятие должно заканчиваться позже, чем начинается")
        return self


class Override(_Strict):
    date: date
    lessons: list[LessonTemplate]
    note: str = ""


class ScheduleFile(_Strict):
    timezone: str
    term_start: date
    term_end: date
    # Понедельник первой недели цикла: от него считается чётность недель.
    cycle_start: date
    weeks: list[dict[Weekday, list[LessonTemplate]]] = Field(min_length=1, max_length=4)
    overrides: list[Override] = []

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("неизвестный часовой пояс") from None
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "ScheduleFile":
        if self.term_end < self.term_start:
            raise ValueError("term_end раньше term_start")
        if self.cycle_start.weekday() != 0:
            raise ValueError("cycle_start должен быть понедельником")
        return self


class ScheduleSource:
    """Расписание в памяти: чередование недель, границы семестра и исключения."""

    def __init__(self, data: ScheduleFile) -> None:
        self._data = data
        self._zone = ZoneInfo(data.timezone)
        self._overrides = {item.date: item.lessons for item in data.overrides}

    @classmethod
    def load(cls, path: Path) -> "ScheduleSource":
        try:
            return cls(ScheduleFile.model_validate_json(path.read_bytes()))
        except (OSError, ValidationError) as exc:
            # Детали остаются в логе сервера; пользователь получает общий код ошибки.
            raise ToolFailure("schedule_unavailable", "Расписание сейчас недоступно.") from exc

    @property
    def timezone(self) -> str:
        return self._data.timezone

    def lessons_on(self, day: date) -> list[Lesson]:
        if not self._data.term_start <= day <= self._data.term_end:
            return []
        if day in self._overrides:
            templates = self._overrides[day]
        else:
            week = (day - self._data.cycle_start).days // 7 % len(self._data.weeks)
            templates = self._data.weeks[week].get(WEEKDAYS[day.weekday()], [])
        return [
            Lesson(
                start=datetime.combine(day, item.start, tzinfo=self._zone).isoformat(),
                end=datetime.combine(day, item.end, tzinfo=self._zone).isoformat(),
                title=item.title,
                location=item.location,
            )
            for item in sorted(templates, key=lambda item: item.start)
        ]

    def day(self, day: date) -> DaySchedule:
        return DaySchedule(
            date=day.isoformat(), timezone=self.timezone, lessons=self.lessons_on(day)
        )

    def today(self, now: datetime) -> date:
        return now.astimezone(self._zone).date()

    def week(self, now: datetime) -> WeekSchedule:
        """Неделя с понедельника по воскресенье, содержащая текущую дату в зоне расписания."""
        monday = self.today(now) - timedelta(days=self.today(now).weekday())
        days = [monday + timedelta(days=offset) for offset in range(7)]
        return WeekSchedule(
            timezone=self.timezone,
            week_start=days[0].isoformat(),
            week_end=days[-1].isoformat(),
            days=[
                WeekDay(
                    date=day.isoformat(),
                    weekday=WEEKDAYS_RU[day.weekday()],
                    lessons=self.lessons_on(day),
                )
                for day in days
            ],
        )


def parse_schedule_date(value: str, *, today: date) -> date:
    """Строгий разбор YYYY-MM-DD и проверка диапазона относительно текущей даты."""
    if not re.fullmatch(DATE_PATTERN, value):
        raise ToolFailure("invalid_date", "Дата должна быть в формате YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ToolFailure("invalid_date", f"Такой даты нет в календаре: {value}.") from None
    if abs((parsed - today).days) > MAX_DAYS_FROM_TODAY:
        raise ToolFailure(
            "date_out_of_range",
            f"Расписание доступно не дальше {MAX_DAYS_FROM_TODAY} дней от сегодняшней даты.",
        )
    return parsed
