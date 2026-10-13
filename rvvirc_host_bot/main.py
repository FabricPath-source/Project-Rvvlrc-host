import asyncio
import logging
import secrets

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

from app import App
from config import load_config
from db import DB
from handlers import admin, user as user_handlers
from integrations.panel_api import get_panel
from integrations.site_api import SiteAPI

log = logging.getLogger("rvvirc")


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя, обновляет username и отсекает заблокированных."""

    async def __call__(self, handler, event, data):
        tg = event.from_user
        if tg is None or tg.is_bot:
            return
        app: App = data["app"]
        user, created = await app.db.upsert_user(tg.id, tg.username)
        role = await app.db.get_role(user["id"])
        if not (role and role["role"] == "owner"):
            ban = await app.db.active_ban(user["id"])
            if ban:
                txt = "🚫 Ваш аккаунт заблокирован. Обратитесь в поддержку проекта."
                if isinstance(event, CallbackQuery):
                    await event.answer(txt, show_alert=True)
                else:
                    await event.answer(txt)
                return
        data["user"], data["is_new"] = user, created
        return await handler(event, data)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db = DB(cfg.db_path)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    app = App(db=db, bot=bot, site=SiteAPI(cfg.site_api_url, cfg.site_api_token), panel=get_panel(cfg),
              cfg=cfg, bot_username=me.username, bot_id=me.id)
    if not await db.one("SELECT 1 FROM roles WHERE role='owner'"):
        app.claim_code = secrets.token_urlsafe(12)
        log.warning("ВЛАДЕЛЕЦ НЕ ПРИВЯЗАН. Аккаунт @%s должен отправить боту: /claim %s", cfg.owner_username, app.claim_code)
    if not app.site.configured:
        log.warning("SITE_API_URL/SITE_API_TOKEN не заданы — регистрация и промокоды будут недоступны.")
    if not app.panel.configured:
        log.warning("Драйвер хостинг-панели не подключён — создание серверов вернёт ошибку.")

    dp = Dispatcher(storage=MemoryStorage(), app=app)
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    dp.include_routers(admin.router, user_handlers.router)   # admin раньше: его callback m:admin
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
