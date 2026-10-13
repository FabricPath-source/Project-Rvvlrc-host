"""Тексты и подписи кнопок.
Приоритет: настройка в БД (/settext) -> файл texts.json -> значения по умолчанию ниже."""
import json
import os

DEFAULTS = {
    "welcome": (
        "Добро пожаловать в RVVIRC HOST!\n\n"
        "Создавай свой Minecraft-сервер, приглашай друзей и получай доступ к бесплатному хостингу.\n\n"
        "Выполняй задания, собирай приглашения и управляй своими серверами прямо через наш сервис."
    ),
    "btn_free": "🎮 Бесплатный сервер",
    "btn_ref": "👥 Реферальная система",
    "btn_profile": "👤 Мой профиль",
    "btn_servers": "🖥 Мои серверы",
    "btn_promo": "🎟 Промокод",
    "btn_rules": "📋 Правила проекта",
    "btn_support": "🆘 Техническая поддержка",
    "btn_admin": "🛠 Админ-панель",
    "rules": (
        "📋 <b>Правила проекта</b>\n\n"
        "1. Один человек — один бесплатный сервер.\n"
        "2. Накрутка рефералов, мультиаккаунты и обход ограничений запрещены.\n"
        "3. Администрация вправе ограничить доступ при нарушении правил.\n\n"
        "<i>Текст редактируется владельцем: /settext rules ...</i>"
    ),
}

_FILE = {}
_path = os.getenv("TEXTS_PATH", "texts.json")
if os.path.exists(_path):
    with open(_path, encoding="utf-8") as f:
        _FILE = json.load(f)


async def get_text(db, key: str) -> str:
    row = await db.one("SELECT value FROM settings WHERE key=?", (f"text:{key}",))
    if row:
        return row["value"]
    return _FILE.get(key) or DEFAULTS.get(key, key)
