"""Интерфейс хостинг-панели.

Реальная панель не указана в ТЗ, поэтому драйвер НЕ выдумывается: базовый класс честно
сообщает, что панель не подключена. Чтобы подключить панель — реализуйте подкласс PanelAPI
по её документированному API и верните его из get_panel().
"""
from dataclasses import dataclass


class PanelError(Exception):
    pass


@dataclass
class ServerInfo:
    panel_id: str
    status: str                 # running | stopped | starting | error ...
    address: str | None = None
    version: str | None = None


class PanelAPI:
    configured = False

    async def create_server(self, account_id: str, plan: dict, version: str) -> ServerInfo:
        raise PanelError("Хостинг-панель не подключена")

    async def get_server(self, panel_id: str) -> ServerInfo:
        raise PanelError("Хостинг-панель не подключена")

    async def power(self, panel_id: str, action: str) -> None:
        """action: start | stop | restart"""
        raise PanelError("Хостинг-панель не подключена")


def get_panel(cfg) -> PanelAPI:
    # TODO: выбрать драйвер по настройкам, например: if cfg.panel_driver == "xxx": return XxxPanel(...)
    return PanelAPI()
