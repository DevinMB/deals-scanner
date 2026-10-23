-- Telegram recipients. The owner row's chat_id is NULL: it means TELEGRAM_CHAT_ID from .env.
CREATE TABLE recipients (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    chat_id    TEXT,
    is_owner   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO recipients (name, is_owner) VALUES ('Me', 1);

CREATE TABLE watch_recipients (
    watch_id     INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL REFERENCES recipients(id) ON DELETE CASCADE,
    PRIMARY KEY (watch_id, recipient_id)
);
INSERT INTO watch_recipients (watch_id, recipient_id) SELECT id, (SELECT id FROM recipients WHERE is_owner = 1) FROM watches;

-- One row per successful delivery, so a retry only goes to the people who didn't get it.
CREATE TABLE alert_deliveries (
    alert_id     INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL REFERENCES recipients(id) ON DELETE CASCADE,
    sent_at      TEXT    NOT NULL,
    PRIMARY KEY (alert_id, recipient_id)
);
