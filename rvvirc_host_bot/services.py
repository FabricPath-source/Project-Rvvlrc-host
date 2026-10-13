import asyncio
import html
import time
from collections import defaultdict
from datetime import datetime, timezone

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

from access import perms_of
from db import now

esc = html.escape
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_OK = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


def fmt(ts) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "—"


def uname(u) -> str:
    return f"@{esc(u['username'])}" if u["username"] else f"ID …{str(u['tg_id'])[-4:]}"


async def safe_send(bot, tg_id: int, text: str, **kw) -> bool:
    try:
        await bot.send_message(tg_id, text, **kw)
        return True
    except TelegramAPIError:
        return False


async def staff_tg_ids(app, perm: str) -> list[int]:
    out = []
    for r in await app.db.all("SELECT u.id, u.tg_id FROM roles r JOIN users u ON u.id=r.user_id"):
        if perm in await perms_of(app.db, r["id"]):
            out.append(r["tg_id"])
    return out


async def check_subs(app, tg_id: int):
    """Реальная проверка через Bot API. Возвращает (не выполненные, {id: ошибка})."""
    missing, errors = [], {}
    for s in await app.db.all("SELECT * FROM subscriptions WHERE enabled=1 ORDER BY id"):
        cid = s["chat_id"]
        chat = int(cid) if cid.lstrip("-").isdigit() else cid
        try:
            m = await app.bot.get_chat_member(chat, tg_id)
            ok = m.status in _OK or (m.status == ChatMemberStatus.RESTRICTED and getattr(m, "is_member", False))
        except TelegramAPIError as e:
            ok, errors[s["id"]] = False, str(e)
        if not ok:
            missing.append(s)
    return missing, errors


async def confirm_referral(app, user):
    """Подтверждает реферала после выполнения условий подписки."""
    db = app.db
    r = await db.one("SELECT * FROM referrals WHERE referred_id=? AND status='pending'", (user["id"],))
    if not r:
        return
    referrer = await db.user_by_id(r["referrer_id"])
    if (await db.active_ban(referrer["id"], ("account", "blacklist", "referral_rewards"))
            or await db.active_ban(user["id"], ("account", "blacklist"))):
        await db.exec("UPDATE referrals SET status='rejected' WHERE id=? AND status='pending'", (r["id"],))
        return
    if await db.exec("UPDATE referrals SET status='confirmed', confirmed_at=? WHERE id=? AND status='pending'",
                     (now(), r["id"])) == 0:
        return
    cnt, need = await db.ref_count(referrer["id"]), await db.int_setting("ref_required")
    msg = f"🎉 Новый реферал подтверждён! Прогресс: {min(cnt, need)} из {need}."
    if cnt >= need:
        msg += "\nУсловие выполнено — откройте «Бесплатный сервер»."
    await safe_send(app.bot, referrer["tg_id"], msg)


async def servers_quota(app, user) -> tuple[int, int]:
    """(использовано, разрешено). Неудачные попытки не считаются."""
    used = (await app.db.one("SELECT COUNT(*) c FROM servers WHERE user_id=? AND status!='failed'", (user["id"],)))["c"]
    allowed = await app.db.int_setting("max_free_servers") + user["bonus_servers"]
    return used, allowed
