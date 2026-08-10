import os
import re
from dataclasses import dataclass
from pathlib import Path

from webapp.telegram_service import TelegramSettings


@dataclass(frozen=True)
class AppSettings:
    username: str
    password: str
    telegram: TelegramSettings

    @classmethod
    def from_env(cls):
        username = os.environ.get("APP_USERNAME", "").strip()
        password = os.environ.get("APP_PASSWORD", "")
        api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
        api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
        missing = [
            name
            for name, value in (
                ("APP_USERNAME", username),
                ("APP_PASSWORD", password),
                ("TELEGRAM_API_ID", api_id),
                ("TELEGRAM_API_HASH", api_hash),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required environment: {', '.join(missing)}")
        try:
            numeric_api_id = int(api_id)
        except ValueError as exc:
            raise RuntimeError("TELEGRAM_API_ID must be an integer.") from exc

        raw_proxies = os.environ.get("TELEGRAM_PROXIES", "")
        proxies = tuple(item.strip() for item in re.split(r"[;\n]+", raw_proxies) if item.strip())
        poll_seconds = float(os.environ.get("POLL_SECONDS", "5"))
        if poll_seconds <= 0:
            raise RuntimeError("POLL_SECONDS must be greater than zero.")

        return cls(
            username=username,
            password=password,
            telegram=TelegramSettings(
                data_dir=Path(os.environ.get("APP_DATA_DIR", "/data")),
                api_id=numeric_api_id,
                api_hash=api_hash,
                proxies=proxies,
                poll_seconds=poll_seconds,
            ),
        )
