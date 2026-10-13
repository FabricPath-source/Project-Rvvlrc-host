from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from texts import get_text


async def show(target, text: str, kb=None):
    """Редактирует сообщение (для callback) или отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def main_menu(app, user):
    b = InlineKeyboardBuilder()
    for key, cb in [("btn_free", "m:free"), ("btn_ref", "m:ref"), ("btn_profile", "m:profile"),
                    ("btn_servers", "m:servers"), ("btn_promo", "m:promo"), ("btn_rules", "m:rules"),
                    ("btn_support", "m:support")]:
        b.button(text=await get_text(app.db, key), callback_data=cb)
    if await app.db.get_role(user["id"]):
        b.button(text=await get_text(app.db, "btn_admin"), callback_data="m:admin")
    b.adjust(1, 1, 2, 2, 1, 1)
    return b.as_markup()


def home_kb(*rows):
    """rows: списки (text, callback_data); внизу всегда «В меню»."""
    b = InlineKeyboardBuilder()
    for text, cb in rows:
        b.button(text=text, callback_data=cb)
    b.button(text="⬅️ В меню", callback_data="m:home")
    b.adjust(1)
    return b.as_markup()
