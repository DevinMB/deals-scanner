"""Runtime configuration from environment variables. Secrets live only here, never in the DB."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    ui_password: str | None
    base_url: str | None
    dry_run: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dealwatch.db"


def load() -> Config:
    def env(name: str) -> str | None:
        return os.environ.get(name) or None

    return Config(
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=env("TELEGRAM_CHAT_ID"),
        ui_password=env("UI_PASSWORD"),
        base_url=env("BASE_URL"),
        dry_run=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
    )
