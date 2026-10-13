from dataclasses import dataclass
from typing import Any

from config import Config
from db import DB
from integrations.panel_api import PanelAPI
from integrations.site_api import SiteAPI


@dataclass
class App:
    db: DB
    bot: Any
    site: SiteAPI
    panel: PanelAPI
    cfg: Config
    bot_username: str = ""
    bot_id: int = 0
    claim_code: str | None = None
