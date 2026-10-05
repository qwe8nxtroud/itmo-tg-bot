# Контракты MCP-сервера

> Документ сгенерирован `python -m scripts.dump_mcp_contracts` из работающего сервера
> (`tools/list`, `resources/list`, вызовы с тестовыми данными на 12.10.2026). Не правьте
> вручную: тест `tests/test_docs.py` сверяет его с кодом.

Сервер: `itmo-student-assistant`, транспорт `stdio` (`python -m mcp_server`).

## Инструменты

| Инструмент | Назначение | Аннотации |
|---|---|---|
| `get_weather` | Текущая погода в городе. | `title: "Погода"`, `readOnlyHint: true`, `openWorldHint: true` |
| `get_schedule` | Занятия на выбранную дату. | `title: "Расписание"`, `readOnlyHint: true` |
| `add_reminder` | Создание напоминания. | `title: "Напоминание"`, `readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: false` |

### `get_weather`

Текущая погода в городе.

        Используй для любых вопросов о погоде сейчас или сегодня: температура, осадки,
        ветер, нужен ли зонт. Возвращает фактические данные Open-Meteo. Если найдено
        несколько одноимённых городов, вернёт status=ambiguous и варианты для выбора.

**inputSchema**

```json
{
  "type": "object",
  "properties": {
    "city": {
      "description": "Название населённого пункта в именительном падеже, например «Казань». Если пользователь назвал страну или регион, добавь их через запятую: «Тбилиси, Грузия».",
      "maxLength": 100,
      "minLength": 1,
      "title": "City",
      "type": "string"
    }
  },
  "required": [
    "city"
  ],
  "title": "get_weatherArguments",
  "additionalProperties": false
}
```

**outputSchema**

```json
{
  "$defs": {
    "Candidate": {
      "properties": {
        "name": {
          "title": "Name",
          "type": "string"
        },
        "country": {
          "title": "Country",
          "type": "string"
        },
        "admin1": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Admin1"
        }
      },
      "required": [
        "name",
        "country",
        "admin1"
      ],
      "title": "Candidate",
      "type": "object"
    },
    "CurrentWeather": {
      "properties": {
        "temperature_c": {
          "description": "Температура воздуха, °C",
          "title": "Temperature C",
          "type": "number"
        },
        "condition_code": {
          "description": "Код погоды WMO",
          "title": "Condition Code",
          "type": "integer"
        },
        "condition": {
          "description": "Описание погоды по-русски",
          "title": "Condition",
          "type": "string"
        },
        "wind_speed": {
          "title": "Wind Speed",
          "type": "number"
        },
        "wind_speed_unit": {
          "const": "m/s",
          "title": "Wind Speed Unit",
          "type": "string"
        },
        "observed_at": {
          "description": "Время наблюдения, ISO 8601 со смещением",
          "title": "Observed At",
          "type": "string"
        },
        "timezone": {
          "description": "Часовой пояс времени наблюдения (IANA)",
          "title": "Timezone",
          "type": "string"
        }
      },
      "required": [
        "temperature_c",
        "condition_code",
        "condition",
        "wind_speed",
        "wind_speed_unit",
        "observed_at",
        "timezone"
      ],
      "title": "CurrentWeather",
      "type": "object"
    },
    "Location": {
      "properties": {
        "name": {
          "title": "Name",
          "type": "string"
        },
        "country": {
          "title": "Country",
          "type": "string"
        },
        "admin1": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "description": "Регион (область, штат)",
          "title": "Admin1"
        },
        "latitude": {
          "title": "Latitude",
          "type": "number"
        },
        "longitude": {
          "title": "Longitude",
          "type": "number"
        },
        "timezone": {
          "description": "Часовой пояс места (IANA)",
          "title": "Timezone",
          "type": "string"
        }
      },
      "required": [
        "name",
        "country",
        "admin1",
        "latitude",
        "longitude",
        "timezone"
      ],
      "title": "Location",
      "type": "object"
    }
  },
  "properties": {
    "status": {
      "description": "ok — погода получена; ambiguous — несколько одноимённых городов, нужно выбрать из candidates",
      "enum": [
        "ok",
        "ambiguous"
      ],
      "title": "Status",
      "type": "string"
    },
    "source": {
      "title": "Source",
      "type": "string"
    },
    "location": {
      "anyOf": [
        {
          "$ref": "#/$defs/Location"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "current": {
      "anyOf": [
        {
          "$ref": "#/$defs/CurrentWeather"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "candidates": {
      "default": [],
      "items": {
        "$ref": "#/$defs/Candidate"
      },
      "title": "Candidates",
      "type": "array"
    }
  },
  "required": [
    "status",
    "source"
  ],
  "title": "WeatherResult",
  "type": "object"
}
```

**Пример `structuredContent`**

```json
{
  "status": "ok",
  "source": "Open-Meteo",
  "location": {
    "name": "Санкт-Петербург",
    "country": "Россия",
    "admin1": "Санкт-Петербург",
    "latitude": 59.94,
    "longitude": 30.31,
    "timezone": "Europe/Moscow"
  },
  "current": {
    "temperature_c": 12.5,
    "condition_code": 3,
    "condition": "пасмурно",
    "wind_speed": 4.2,
    "wind_speed_unit": "m/s",
    "observed_at": "2026-10-12T09:00:00+03:00",
    "timezone": "Europe/Moscow"
  },
  "candidates": []
}
```

### `get_schedule`

Занятия на выбранную дату.

        Используй для вопросов о парах, занятиях и расписании на конкретный день.
        Пустой список lessons означает, что занятий нет.

**inputSchema**

```json
{
  "type": "object",
  "properties": {
    "date": {
      "description": "Дата в формате ISO 8601 YYYY-MM-DD. «Сегодня», «завтра», «в пятницу» вычисляй от текущей даты пользователя.",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
      "title": "Date",
      "type": "string"
    }
  },
  "required": [
    "date"
  ],
  "title": "get_scheduleArguments",
  "additionalProperties": false
}
```

**outputSchema**

```json
{
  "$defs": {
    "Lesson": {
      "properties": {
        "start": {
          "description": "Начало занятия, ISO 8601 со смещением",
          "title": "Start",
          "type": "string"
        },
        "end": {
          "description": "Окончание занятия, ISO 8601 со смещением",
          "title": "End",
          "type": "string"
        },
        "title": {
          "description": "Название дисциплины и вид занятия",
          "title": "Title",
          "type": "string"
        },
        "location": {
          "description": "Аудитория или формат проведения",
          "title": "Location",
          "type": "string"
        }
      },
      "required": [
        "start",
        "end",
        "title",
        "location"
      ],
      "title": "Lesson",
      "type": "object"
    }
  },
  "properties": {
    "date": {
      "description": "Дата YYYY-MM-DD",
      "title": "Date",
      "type": "string"
    },
    "timezone": {
      "description": "Часовой пояс расписания (IANA)",
      "title": "Timezone",
      "type": "string"
    },
    "lessons": {
      "description": "Занятия по времени начала; пустой — занятий нет",
      "items": {
        "$ref": "#/$defs/Lesson"
      },
      "title": "Lessons",
      "type": "array"
    }
  },
  "required": [
    "date",
    "timezone",
    "lessons"
  ],
  "title": "DaySchedule",
  "type": "object"
}
```

**Пример `structuredContent`**

```json
{
  "date": "2026-10-12",
  "timezone": "Europe/Moscow",
  "lessons": [
    {
      "start": "2026-10-12T10:00:00+03:00",
      "end": "2026-10-12T11:30:00+03:00",
      "title": "Матанализ",
      "location": "ауд. 101"
    }
  ]
}
```

### `add_reminder`

Создание напоминания.

        Используй, когда пользователь просит напомнить о чём-то в конкретное время.
        Если дата или время не названы точно, сначала уточни их у пользователя.
        Вызов выполняется только после подтверждения пользователем.

**inputSchema**

```json
{
  "type": "object",
  "properties": {
    "text": {
      "description": "Что напомнить, без слов «напомни» и указания времени, например «отправить отчёт».",
      "maxLength": 500,
      "minLength": 1,
      "title": "Text",
      "type": "string"
    },
    "remind_at": {
      "description": "Когда напомнить: дата и время ISO 8601 со смещением часового пояса пользователя, например 2026-10-13T18:30:00+03:00. Только будущее время.",
      "maxLength": 40,
      "minLength": 16,
      "title": "Remind At",
      "type": "string"
    }
  },
  "required": [
    "text",
    "remind_at"
  ],
  "title": "add_reminderArguments",
  "additionalProperties": false
}
```

**outputSchema**

```json
{
  "properties": {
    "reminder_id": {
      "description": "Идентификатор напоминания",
      "title": "Reminder Id",
      "type": "integer"
    },
    "text": {
      "title": "Text",
      "type": "string"
    },
    "remind_at": {
      "description": "Время напоминания, ISO 8601 в зоне пользователя",
      "title": "Remind At",
      "type": "string"
    },
    "timezone": {
      "description": "Часовой пояс пользователя (IANA)",
      "title": "Timezone",
      "type": "string"
    },
    "created": {
      "description": "false — запись уже была создана этим же действием",
      "title": "Created",
      "type": "boolean"
    }
  },
  "required": [
    "reminder_id",
    "text",
    "remind_at",
    "timezone",
    "created"
  ],
  "title": "ReminderResult",
  "type": "object"
}
```

Пример отказа без доверенного контекста (`isError: true`, текстовый блок):

```text
Error executing tool add_reminder: {"code": "untrusted_context", "message": "Вызов отклонён: нет доверенного контекста приложения."}
```

## Ресурсы

| URI | Имя | MIME | Описание |
|---|---|---|---|
| `schedule://current-week` | current-week | `application/json` | Занятия с понедельника по воскресенье текущей недели, по датам. |

Пример содержимого `schedule://current-week` (первый день недели и поля верхнего уровня):

```json
{
  "timezone": "Europe/Moscow",
  "week_start": "2026-10-12",
  "week_end": "2026-10-18",
  "days": [
    {
      "date": "2026-10-12",
      "weekday": "понедельник",
      "lessons": [
        {
          "start": "2026-10-12T10:00:00+03:00",
          "end": "2026-10-12T11:30:00+03:00",
          "title": "Матанализ",
          "location": "ауд. 101"
        }
      ]
    },
    "…"
  ]
}
```

## Ошибки

Ошибка инструмента — результат с `isError: true`, в текстовом блоке JSON
`{"code": "...", "message": "..."}` (SDK может добавить перед ним префикс).
Коды и их смысл — в [spec.md](spec.md), раздел 3.
