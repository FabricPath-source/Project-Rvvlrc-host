import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str
    owner_username: str
    site_api_url: str
    site_api_token: str


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    return Config(
        bot_token=token,
        db_path=os.getenv("DB_PATH", "rvvirc.db"),
        owner_username=os.getenv("OWNER_USERNAME", "gottalarp").lstrip("@").lower(),
        site_api_url=os.getenv("SITE_API_URL", "").rstrip("/"),
        site_api_token=os.getenv("SITE_API_TOKEN", ""),
    )
