-- ЛР1: настройки пользователя и история диалога.
CREATE TABLE IF NOT EXISTS user_settings (
    chat_id     BIGINT PRIMARY KEY,
    mode        TEXT NOT NULL,
    temperature DOUBLE PRECISION NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dialog_messages (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dialog_messages_chat_id_id_idx ON dialog_messages (chat_id, id);
