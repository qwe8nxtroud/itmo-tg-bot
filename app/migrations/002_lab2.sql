-- ЛР2: часовой пояс, подготовленные действия, напоминания и аудит решений агента.

-- Часовой пояс пользователя — имя из базы IANA, проверяется приложением.
CREATE TABLE user_profiles (
    user_id    BIGINT PRIMARY KEY,
    timezone   TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Действие с побочным эффектом, ожидающее подтверждения пользователя.
-- Одно сообщение Telegram порождает не больше одного действия: повторная доставка
-- update натыкается на UNIQUE (chat_id, source_message_id).
CREATE TABLE pending_actions (
    id                UUID PRIMARY KEY,
    user_id           BIGINT NOT NULL,
    chat_id           BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    tool              TEXT NOT NULL,
    arguments         JSONB NOT NULL,
    timezone          TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (
        status IN ('pending', 'executing', 'done', 'cancelled', 'expired', 'failed')
    ),
    result            JSONB,
    -- Момент первого подтверждения: подтверждённое действие можно повторить после сбоя
    -- и после истечения 5 минут (на сервере оно идемпотентно по id).
    confirmed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, source_message_id)
);
CREATE INDEX pending_actions_user_status_idx ON pending_actions (user_id, status);

-- Напоминания пишет MCP-сервер. Ключ идемпотентности — id подтверждённого действия.
-- Отправка уведомлений в назначенное время появится в ЛР3 (колонка sent_at).
CREATE TABLE reminders (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    idempotency_key UUID NOT NULL UNIQUE,
    text            TEXT NOT NULL CHECK (char_length(text) BETWEEN 1 AND 500),
    remind_at       TIMESTAMPTZ NOT NULL,
    timezone        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);
CREATE INDEX reminders_owner_remind_at_idx ON reminders (owner_id, remind_at);

-- Краткий аудит решений агента: источник для /why, без текстов сообщений.
CREATE TABLE agent_events (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    request_id   TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('message', 'confirm', 'cancel')),
    action       TEXT NOT NULL,
    tools        TEXT,
    args_summary TEXT,
    validation   TEXT NOT NULL,
    execution    TEXT NOT NULL,
    reason       TEXT,
    tool_calls   INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX agent_events_user_id_idx ON agent_events (user_id, id);
