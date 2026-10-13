import hmac
import ipaddress
import json
import re
import sqlite3

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from access import ALL_PERMS, ROLE_NAMES, SUPPORT_PERMS, perms_of, role_of
from db import now
from handlers.ui import home_kb, show
from integrations.site_api import SiteAPIError
from services import check_subs, esc, fmt, safe_send, staff_tg_ids, uname
from texts import DEFAULTS

router = Router(name="admin")
router.message.filter(F.chat.type == "private")
SCOPES = ("account", "free_server", "referral_rewards", "blacklist")


# ───────────── вспомогательное ─────────────
async def gate(m, app, user, perm) -> bool:
    if perm in await perms_of(app.db, user["id"]):
        return True
    await (m.answer("⛔ Недостаточно прав.") if isinstance(m, Message) else m.answer("⛔ Недостаточно прав.", show_alert=True))
    return False


def args(cmd: CommandObject, n: int = -1) -> list:
    return (cmd.args or "").split(maxsplit=n) if n >= 0 else (cmd.args or "").split()


async def target(app, m, ref):
    u = await app.db.find_user(ref)
    if not u:
        await m.answer("Пользователь не найден. Укажите Telegram ID или <code>#внутренний_ID</code>.")
    return u


async def protect(app, m, actor, tgt, need_owner_for_staff=True) -> bool:
    """Защита ролей: нельзя трогать себя и владельца; помощников/поддержку — по правилам."""
    if actor["id"] == tgt["id"]:
        await m.answer("⛔ Нельзя применить к самому себе.")
        return False
    tr, ar = await role_of(app.db, tgt["id"]), await role_of(app.db, actor["id"])
    if tr == "owner":
        await m.answer("⛔ Роль владельца неприкосновенна.")
        return False
    if need_owner_for_staff and tr in ("assistant", "support") and ar != "owner":
        await m.answer("⛔ Действия над администраторами доступны только владельцу.")
        return False
    return True


def mask_email(e):
    if not e:
        return "—"
    a, _, d = e.partition("@")
    return f"{a[:1]}***@{d}"


# ───────────── привязка владельца ─────────────
@router.message(Command("claim"))
async def claim(m: Message, command: CommandObject, app, user):
    """Первичная привязка владельца: нужен одноразовый код из консоли сервера + совпадение username.
    Роль сохраняется по Telegram ID, дальнейшая смена username ничего не даёт."""
    code = (command.args or "").strip()
    if (app.claim_code is None or not code or not hmac.compare_digest(code, app.claim_code)
            or (user["username"] or "").lower() != app.cfg.owner_username):
        return await m.answer("Неизвестная команда.")
    await app.db.exec("INSERT OR REPLACE INTO roles(user_id,role,perms,granted_by,granted_at) VALUES(?,?,?,?,?)",
                      (user["id"], "owner", "[]", None, now()))
    await app.db.audit(user["id"], "owner_claimed", f"tg:{user['tg_id']}")
    app.claim_code = None
    await m.answer(f"✅ Вы назначены владельцем. Telegram ID <code>{user['tg_id']}</code> зафиксирован. Откройте /admin")


# ───────────── главное меню админки ─────────────
SECTIONS = [  # (callback, название, право)
    ("stats", "📊 Статистика", "stats"), ("admins", "👮 Администраторы", "roles_support"),
    ("promos", "🎟 Промокоды", "promos"), ("subs", "📢 Подписки", "subs"),
    ("bans", "🚫 Безопасность", "bans"), ("tickets", "📨 Тикеты", "tickets"),
    ("log", "🧾 Журнал", "audit"), ("settings", "⚙️ Настройки и тарифы", "settings"),
    ("rewards", "🏆 Заявки и награды", "rewards"),
]


async def admin_menu(app, user):
    perms = await perms_of(app.db, user["id"])
    b = InlineKeyboardBuilder()
    for key, title, perm in SECTIONS:
        if perm in perms or (key == "admins" and "roles_assistant" in perms):
            b.button(text=title, callback_data=f"a:{key}")
    b.button(text="⬅️ В меню", callback_data="m:home")
    b.adjust(2)
    role = ROLE_NAMES[await role_of(app.db, user["id"])]
    return f"🛠 <b>Админ-панель</b> — {role}\nПоиск: <code>/user &lt;tg_id | #id&gt;</code>", b.as_markup()


@router.message(Command("admin"))
async def admin_cmd(m: Message, app, user):
    if not await app.db.get_role(user["id"]):
        return await m.answer("Неизвестная команда.")
    text, kb = await admin_menu(app, user)
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data == "m:admin")
async def admin_cb(cb: CallbackQuery, app, user):
    if not await app.db.get_role(user["id"]):
        return await cb.answer("⛔", show_alert=True)
    await cb.answer()
    text, kb = await admin_menu(app, user)
    await show(cb, text, kb)


async def render(app, key):
    db = app.db
    if key == "stats":
        c = lambda q, p=(): db.one(q, p)
        total, day = await c("SELECT COUNT(*) n FROM users"), await c("SELECT COUNT(*) n FROM users WHERE created_at>?", (now() - 86400,))
        refs, bans = await c("SELECT COUNT(*) n FROM referrals WHERE status='confirmed'"), await c("SELECT COUNT(*) n FROM bans WHERE active=1")
        srv = await db.all("SELECT status, COUNT(*) n FROM servers GROUP BY status")
        tk, pu = await c("SELECT COUNT(*) n FROM tickets WHERE status='open'"), await c("SELECT COUNT(*) n FROM promo_uses")
        return (f"📊 <b>Статистика</b>\nПользователей: {total['n']} (за 24ч: {day['n']})\nПодтверждённых рефералов: {refs['n']}\n"
                f"Серверы: {', '.join(f'{r['status']}={r['n']}' for r in srv) or '—'}\nОткрытых тикетов: {tk['n']}\n"
                f"Активаций промокодов: {pu['n']}\nАктивных блокировок: {bans['n']}")
    if key == "admins":
        rows = await db.all("SELECT r.*, u.tg_id, u.username, g.tg_id gtg FROM roles r JOIN users u ON u.id=r.user_id "
                            "LEFT JOIN users g ON g.id=r.granted_by ORDER BY r.granted_at")
        t = "\n".join(f"#{r['user_id']} <code>{r['tg_id']}</code> {uname(r)} — {ROLE_NAMES[r['role']]}"
                      f"{' ' + str(json.loads(r['perms'])) if r['role'] == 'assistant' else ''}\n   с {fmt(r['granted_at'])}, выдал: {r['gtg'] or '—'}" for r in rows)
        return ("👮 <b>Администраторы</b>\n" + t + "\n\n/grant_support &lt;ref&gt;\n/grant_assistant &lt;ref&gt;\n/revoke &lt;ref&gt;\n"
                "/perm &lt;ref&gt; &lt;право&gt; on|off (владелец)\nПрава: " + ", ".join(ALL_PERMS))
    if key == "promos":
        rows = await db.all("SELECT p.*, (SELECT COUNT(*) FROM promo_uses WHERE promo_id=p.id) n FROM promos p WHERE deleted=0 ORDER BY id DESC LIMIT 20")
        t = "\n".join(f"<b>{esc(p['code'])}</b> −{p['percent']}% до {fmt(p['expires_at']) if p['expires_at'] else '∞'}, "
                      f"{p['n']}/{p['max_uses'] or '∞'}, на чел. {p['per_user']}, {'🟢' if p['active'] else '⚪️'}" for p in rows)
        return ("🎟 <b>Промокоды</b>\n" + (t or "пока нет") + "\n\n/promo_add КОД ПРОЦЕНТ ДНЕЙ(0=∞) МАКС(0=∞) НА_ЧЕЛ [тарифы|*] [мин_сумма]\n"
                "/promo_edit КОД percent|days|max|per_user|plans|min ЗНАЧЕНИЕ\n/promo_toggle КОД\n/promo_del КОД\n/promo_info КОД")
    if key == "subs":
        rows = await db.all("SELECT * FROM subscriptions ORDER BY id")
        t = "\n".join(f"#{s['id']} {'🟢' if s['enabled'] else '⚪️'} {esc(s['title'])} — <code>{esc(s['chat_id'])}</code>" for s in rows)
        return ("📢 <b>Обязательные подписки</b>\n" + (t or "нет") + "\n\n/sub_add &lt;chat_id|@канал&gt; &lt;ссылка&gt; &lt;название&gt;\n"
                "/sub_toggle ID\n/sub_del ID\nБот должен быть админом в канале/чате.")
    if key == "bans":
        rows = await db.all("SELECT b.*, u.tg_id FROM bans b JOIN users u ON u.id=b.user_id ORDER BY b.id DESC LIMIT 15")
        t = "\n".join(f"{'🔴' if b['active'] else '⚪️'} #{b['user_id']} ({b['tg_id']}) {b['scope']} до {fmt(b['ends_at']) if b['ends_at'] else '∞'} — {esc(b['reason'])}" for b in rows)
        return ("🚫 <b>Безопасность</b> (последние)\n" + (t or "нет") + "\n\n/ban &lt;ref&gt; account|free_server|referral_rewards|blacklist ДНЕЙ(0=∞) причина\n"
                "/unban &lt;ref&gt; [scope]\n/bans &lt;ref&gt; — история\n/ipblock CIDR ЧАСЫ причина, /ipunblock CIDR (через API сайта)")
    if key == "tickets":
        rows = await db.all("SELECT * FROM tickets WHERE status='open' ORDER BY id LIMIT 20")
        return ("📨 <b>Открытые тикеты</b>\n" + ("\n".join(f"#{t['id']} от #{t['user_id']}: {esc(t['subject'])}" for t in rows) or "нет")
                + "\n\n/ticket ID, /reply ID текст, /close ID, /escalate ID")
    if key == "log":
        rows = await db.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 20")
        return "🧾 <b>Журнал</b>\n" + "\n".join(f"{fmt(r['created_at'])} #{r['admin_id'] or '—'} {esc(r['action'])} {esc(r['target'] or '')} {esc((r['details'] or '')[:80])}" for r in rows)
    if key == "settings":
        pl = await db.plans()
        return (f"⚙️ <b>Настройки</b>\nref_required={await db.setting('ref_required')}, max_free_servers={await db.setting('max_free_servers')}\n"
                f"support_username={esc(await db.setting('support_username')) or '—'}, support_url={esc(await db.setting('support_url')) or '—'}\n"
                "/set ключ значение\n\n<b>Тарифы</b>\n" + "\n".join(f"{esc(p['id'])}: {esc(p['name'])} {p['ram_mb']}МБ/{p['disk_mb']}МБ/{p['cpu']}% {','.join(p['versions'])}" for p in pl)
                + "\n/plan_set ID Название ОЗУ_МБ ДИСК_МБ CPU% версии,через,запятую\n/plan_del ID\n\n"
                "<b>Тексты</b>: /texts, /settext ключ текст")
    if key == "rewards":
        rows = await db.all("SELECT * FROM servers WHERE status IN ('failed','creating') ORDER BY id DESC LIMIT 15")
        return ("🏆 <b>Заявки на серверы (проблемные)</b>\n" + ("\n".join(f"#{s['id']} user #{s['user_id']} {s['status']} {esc((s['error'] or '')[:80])}" for s in rows) or "нет")
                + "\n\n/ref_adjust &lt;ref&gt; ±N причина — корректировка приглашений\n/reward &lt;ref&gt; ±N причина — доп. бесплатные серверы")
    return "?"


@router.callback_query(F.data.startswith("a:"))
async def section(cb: CallbackQuery, app, user):
    key = cb.data[2:]
    need = {k: p for k, _, p in SECTIONS}.get(key)
    perms = await perms_of(app.db, user["id"])
    if not need or not (need in perms or (key == "admins" and "roles_assistant" in perms)):
        return await cb.answer("⛔ Недостаточно прав.", show_alert=True)
    await cb.answer()
    await show(cb, await render(app, key), home_kb(("🛠 Админ-панель", "m:admin")))


# ───────────── пользователи ─────────────
@router.message(Command("user"))
async def user_cmd(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "users_view"):
        return
    a = args(command)
    if not a:
        return await m.answer("Использование: /user &lt;tg_id | #id&gt;")
    u = await target(app, m, a[0])
    if not u:
        return
    db = app.db
    role, cnt, need = await db.get_role(u["id"]), await db.ref_count(u["id"]), await db.int_setting("ref_required")
    missing, errs = await check_subs(app, u["tg_id"])
    srv = await db.all("SELECT id, status, address FROM servers WHERE user_id=?", (u["id"],))
    bans = await db.all("SELECT scope, reason, ends_at FROM bans WHERE user_id=? AND active=1 AND (ends_at IS NULL OR ends_at>?)", (u["id"], now()))
    full = "users_manage" in await perms_of(db, user["id"])
    await m.answer(
        f"👤 <b>#{u['id']}</b> · <code>{u['tg_id']}</code> · {('@' + esc(u['username'])) if u['username'] else '—'}\n"
        f"Роль: {ROLE_NAMES[role['role'] if role else None]}\nРегистрация: {fmt(u['created_at'])}\n"
        f"Почта: {esc(u['email'] if full else mask_email(u['email']))} ({'✅' if u['email_verified'] else '—'}), аккаунт сайта: {esc(u['site_account_id'] or '—')}\n"
        f"Подписки: {'✅ все' if not missing else '❌ не хватает: ' + ', '.join(esc(s['title']) for s in missing)}"
        f"{' (ошибок проверки: ' + str(len(errs)) + ')' if errs else ''}\n"
        f"Рефералы: {cnt}/{need}\nСерверы: {', '.join(f'#{s['id']} {s['status']} {esc(s['address'] or '')}' for s in srv) or '—'}\n"
        f"Блокировки: {', '.join(f'{b['scope']} ({esc(b['reason'])})' for b in bans) or 'нет'}\nЛимит доп. серверов: +{u['bonus_servers']}")


@router.message(Command("ban"))
async def ban(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "bans"):
        return
    a = args(command, 3)
    if len(a) < 4 or a[1] not in SCOPES or not a[2].isdigit():
        return await m.answer("Использование: /ban &lt;ref&gt; account|free_server|referral_rewards|blacklist ДНЕЙ(0=∞) причина")
    u = await app.db.find_user(a[0])
    if not u and a[0].isdigit():                        # блокировка по Telegram ID ещё не пользовавшегося
        u, _ = await app.db.upsert_user(int(a[0]), None)
    if not u:
        return await m.answer("Пользователь не найден.")
    if not await protect(app, m, user, u):
        return
    days = int(a[2])
    await app.db.insert("INSERT INTO bans(user_id,scope,reason,starts_at,ends_at,admin_id) VALUES(?,?,?,?,?,?)",
                        (u["id"], a[1], a[3], now(), now() + days * 86400 if days else None, user["id"]))
    await app.db.audit(user["id"], "ban", f"#{u['id']}", f"{a[1]} {days}d {a[3]}")
    await m.answer(f"🚫 #{u['id']}: {a[1]} на {days or '∞'} дн.")


@router.message(Command("unban"))
async def unban(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "bans"):
        return
    a = args(command)
    u = await target(app, m, a[0]) if a else None
    if not u:
        return
    q, p = "UPDATE bans SET active=0, lifted_by=?, lifted_at=? WHERE user_id=? AND active=1", [user["id"], now(), u["id"]]
    if len(a) > 1:
        q, p = q + " AND scope=?", p + [a[1]]
    n = await app.db.exec(q, tuple(p))
    await app.db.audit(user["id"], "unban", f"#{u['id']}", " ".join(a[1:]))
    await m.answer(f"✅ Снято блокировок: {n}")


@router.message(Command("bans"))
async def bans_hist(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "bans"):
        return
    a = args(command)
    u = await target(app, m, a[0]) if a else None
    if not u:
        return
    rows = await app.db.all("SELECT * FROM bans WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["id"],))
    await m.answer("\n".join(f"{'🔴' if r['active'] else '⚪️'} {r['scope']} {fmt(r['starts_at'])} → {fmt(r['ends_at']) if r['ends_at'] else '∞'} "
                             f"адм.#{r['admin_id']} {esc(r['reason'])}" + (f" (снял #{r['lifted_by']} {fmt(r['lifted_at'])})" if r['lifted_by'] else "") for r in rows) or "История пуста.")


@router.message(Command("ref_adjust"))
async def ref_adjust(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "rewards"):
        return
    a = args(command, 2)
    if len(a) < 3 or not re.fullmatch(r"[+-]?\d+", a[1]):
        return await m.answer("Использование: /ref_adjust &lt;ref&gt; ±N причина")
    u = await target(app, m, a[0])
    if not u:
        return
    await app.db.insert("INSERT INTO ref_adjustments(user_id,delta,reason,admin_id,created_at) VALUES(?,?,?,?,?)", (u["id"], int(a[1]), a[2], user["id"], now()))
    await app.db.audit(user["id"], "ref_adjust", f"#{u['id']}", f"{a[1]}: {a[2]}")
    await m.answer(f"✅ Теперь у #{u['id']} приглашений: {await app.db.ref_count(u['id'])}")


@router.message(Command("reward"))
async def reward(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "rewards"):
        return
    a = args(command, 2)
    if len(a) < 3 or not re.fullmatch(r"[+-]?\d+", a[1]):
        return await m.answer("Использование: /reward &lt;ref&gt; ±N причина")
    u = await target(app, m, a[0])
    if not u:
        return
    await app.db.exec("UPDATE users SET bonus_servers=MAX(0,bonus_servers+?) WHERE id=?", (int(a[1]), u["id"]))
    await app.db.audit(user["id"], "reward", f"#{u['id']}", f"{a[1]}: {a[2]}")
    await m.answer("✅ Готово.")


# ───────────── администраторы ─────────────
async def set_role(m, command, app, user, role, perm):
    if not await gate(m, app, user, perm):
        return
    a = args(command)
    u = await target(app, m, a[0]) if a else None
    if not u or not await protect(app, m, user, u):
        return
    await app.db.exec("INSERT OR REPLACE INTO roles(user_id,role,perms,granted_by,granted_at) VALUES(?,?,?,?,?)",
                      (u["id"], role, "[]", user["id"], now()))
    await app.db.audit(user["id"], f"grant_{role}", f"#{u['id']}")
    await safe_send(app.bot, u["tg_id"], f"Вам назначена роль: {ROLE_NAMES[role]}. Откройте /admin")
    await m.answer(f"✅ #{u['id']} → {ROLE_NAMES[role]}")


@router.message(Command("grant_support"))
async def grant_support(m: Message, command: CommandObject, app, user):
    await set_role(m, command, app, user, "support", "roles_support")


@router.message(Command("grant_assistant"))
async def grant_assistant(m: Message, command: CommandObject, app, user):
    await set_role(m, command, app, user, "assistant", "roles_assistant")


@router.message(Command("revoke"))
async def revoke(m: Message, command: CommandObject, app, user):
    a = args(command)
    u = await app.db.find_user(a[0]) if a else None
    if not u:
        return await m.answer("Использование: /revoke &lt;ref&gt;")
    r = await app.db.get_role(u["id"])
    if not r or not await gate(m, app, user, "roles_assistant" if r["role"] == "assistant" else "roles_support"):
        return
    if not await protect(app, m, user, u, need_owner_for_staff=False):
        return
    await app.db.exec("DELETE FROM roles WHERE user_id=?", (u["id"],))
    await app.db.audit(user["id"], "revoke_role", f"#{u['id']}", r["role"])
    await m.answer("✅ Роль снята.")


@router.message(Command("perm"))
async def perm_cmd(m: Message, command: CommandObject, app, user):
    if await role_of(app.db, user["id"]) != "owner":
        return await m.answer("⛔ Только владелец.")
    a = args(command)
    if len(a) != 3 or a[1] not in ALL_PERMS or a[1] in SUPPORT_PERMS or a[2] not in ("on", "off"):
        return await m.answer("Использование: /perm &lt;ref&gt; &lt;право&gt; on|off\nПрава: " + ", ".join(p for p in ALL_PERMS if p not in SUPPORT_PERMS))
    u = await target(app, m, a[0])
    r = await app.db.get_role(u["id"]) if u else None
    if not r or r["role"] != "assistant":
        return await m.answer("Права настраиваются только у помощников владельца.")
    ps = set(json.loads(r["perms"]))
    ps.add(a[1]) if a[2] == "on" else ps.discard(a[1])
    await app.db.exec("UPDATE roles SET perms=? WHERE user_id=?", (json.dumps(sorted(ps)), u["id"]))
    await app.db.audit(user["id"], "perm", f"#{u['id']}", f"{a[1]} {a[2]}")
    await m.answer(f"✅ Права #{u['id']}: {sorted(ps)}")


@router.message(Command("admins"))
async def admins(m: Message, app, user):
    if not await gate(m, app, user, "roles_support"):
        return
    await m.answer(await render(app, "admins"))


# ───────────── промокоды ─────────────
@router.message(Command("promo_add"))
async def promo_add(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "promos"):
        return
    a = args(command)
    try:
        code, pct, days, mx, per = a[0].upper(), int(a[1]), int(a[2]), int(a[3]), int(a[4])
        plans = a[5] if len(a) > 5 else "*"
        mn = float(a[6]) if len(a) > 6 else 0.0
        assert re.fullmatch(r"[A-Z0-9_-]{3,32}", code) and 1 <= pct <= 100 and days >= 0 and mx >= 0 and per >= 1 and mn >= 0
    except (IndexError, ValueError, AssertionError):
        return await m.answer("Использование: /promo_add КОД ПРОЦЕНТ(1-100) ДНЕЙ(0=∞) МАКС(0=∞) НА_ЧЕЛ [тарифы|*] [мин_сумма]")
    try:
        await app.db.insert("INSERT INTO promos(code,percent,expires_at,max_uses,per_user,plans,min_amount,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (code, pct, now() + days * 86400 if days else None, mx, per, plans, mn, user["id"], now()))
    except sqlite3.IntegrityError:
        return await m.answer("Такой промокод уже существует.")
    await app.db.audit(user["id"], "promo_add", code, f"{pct}%")
    await m.answer(f"✅ Промокод <b>{esc(code)}</b> −{pct}% создан.")


@router.message(Command("promo_edit"))
async def promo_edit(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "promos"):
        return
    a = args(command)
    cols = {"percent": "percent", "max": "max_uses", "per_user": "per_user", "plans": "plans", "min": "min_amount"}
    try:
        code, field, val = a[0].upper(), a[1], a[2]
        if field == "days":
            await app.db.exec("UPDATE promos SET expires_at=? WHERE code=? AND deleted=0", (now() + int(val) * 86400 if int(val) else None, code))
        else:
            v = val if field == "plans" else float(val) if field == "min" else int(val)
            if field == "percent":
                assert 1 <= v <= 100
            await app.db.exec(f"UPDATE promos SET {cols[field]}=? WHERE code=? AND deleted=0", (v, code))
    except (IndexError, KeyError, ValueError, AssertionError):
        return await m.answer("Использование: /promo_edit КОД percent|days|max|per_user|plans|min ЗНАЧЕНИЕ")
    await app.db.audit(user["id"], "promo_edit", code, f"{field}={val}")
    await m.answer("✅ Обновлено.")


@router.message(Command("promo_toggle"))
async def promo_toggle(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "promos"):
        return
    code = (args(command) or [""])[0].upper()
    n = await app.db.exec("UPDATE promos SET active=1-active WHERE code=? AND deleted=0", (code,))
    await app.db.audit(user["id"], "promo_toggle", code)
    await m.answer("✅ Статус изменён." if n else "Не найден.")


@router.message(Command("promo_del"))
async def promo_del(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "promos"):
        return
    code = (args(command) or [""])[0].upper()
    n = await app.db.exec("UPDATE promos SET deleted=1, active=0 WHERE code=?", (code,))   # история активаций сохраняется
    await app.db.audit(user["id"], "promo_del", code)
    await m.answer("✅ Удалён (история сохранена)." if n else "Не найден.")


@router.message(Command("promo_info"))
async def promo_info(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "promos"):
        return
    p = await app.db.one("SELECT * FROM promos WHERE code=?", ((args(command) or [""])[0].upper(),))
    if not p:
        return await m.answer("Не найден.")
    uses = await app.db.all("SELECT u.id, u.tg_id, pu.used_at FROM promo_uses pu JOIN users u ON u.id=pu.user_id WHERE pu.promo_id=? ORDER BY pu.id DESC LIMIT 30", (p["id"],))
    await m.answer(f"<b>{esc(p['code'])}</b> −{p['percent']}% | до {fmt(p['expires_at']) if p['expires_at'] else '∞'} | макс {p['max_uses'] or '∞'}, на чел. {p['per_user']}\n"
                   f"Тарифы: {esc(p['plans'])}, мин. сумма {p['min_amount']}, {'🟢' if p['active'] and not p['deleted'] else '⚪️'}{' (удалён)' if p['deleted'] else ''}\n"
                   f"Активаций: {len(uses)}\n" + "\n".join(f"#{u['id']} ({u['tg_id']}) {fmt(u['used_at'])}" for u in uses))


# ───────────── подписки ─────────────
@router.message(Command("sub_add"))
async def sub_add(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "subs"):
        return
    a = args(command, 2)
    if len(a) < 3 or not a[1].startswith("http"):
        return await m.answer("Использование: /sub_add &lt;chat_id|@канал&gt; &lt;https-ссылка&gt; &lt;название&gt;")
    chat = int(a[0]) if a[0].lstrip("-").isdigit() else a[0]
    try:
        me = await app.bot.get_chat_member(chat, app.bot_id)
        if me.status not in ("administrator", "creator"):
            raise ValueError("бот не администратор")
    except (TelegramAPIError, ValueError) as e:
        return await m.answer(f"❌ Бот не может проверять участников этого чата: {esc(str(e))}\nДобавьте бота администратором и повторите.")
    sid = await app.db.insert("INSERT INTO subscriptions(title,chat_id,url) VALUES(?,?,?)", (a[2], str(a[0]), a[1]))
    await app.db.audit(user["id"], "sub_add", str(sid), a[2])
    await m.answer(f"✅ Подписка #{sid} добавлена.")


@router.message(Command("sub_toggle"))
async def sub_toggle(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "subs"):
        return
    a = args(command)
    n = await app.db.exec("UPDATE subscriptions SET enabled=1-enabled WHERE id=?", (int(a[0]),)) if a and a[0].isdigit() else 0
    await app.db.audit(user["id"], "sub_toggle", " ".join(a))
    await m.answer("✅ Готово." if n else "Не найдено.")


@router.message(Command("sub_del"))
async def sub_del(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "subs"):
        return
    a = args(command)
    n = await app.db.exec("DELETE FROM subscriptions WHERE id=?", (int(a[0]),)) if a and a[0].isdigit() else 0
    await app.db.audit(user["id"], "sub_del", " ".join(a))
    await m.answer("✅ Удалено." if n else "Не найдено.")


# ───────────── настройки, тексты, тарифы ─────────────
SETTINGS = {"ref_required": lambda v: int(v) >= 1, "max_free_servers": lambda v: int(v) >= 0,
            "support_username": lambda v: re.fullmatch(r"@?\w{4,32}", v) is not None,
            "support_url": lambda v: v == "-" or v.startswith("https://")}


@router.message(Command("set"))
async def set_cmd(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "settings"):
        return
    a = args(command, 1)
    try:
        assert len(a) == 2 and SETTINGS[a[0]](a[1])
    except (AssertionError, KeyError, ValueError):
        return await m.answer("Использование: /set ключ значение\nКлючи: " + ", ".join(SETTINGS) + "\n(для support_url «-» очищает)")
    v = "" if a[1] == "-" else a[1].lstrip("@") if a[0] == "support_username" else a[1]
    await app.db.set_setting(a[0], v)
    await app.db.audit(user["id"], "set", a[0], v)
    await m.answer("✅ Сохранено.")


@router.message(Command("texts"))
async def texts_cmd(m: Message, app, user):
    if await gate(m, app, user, "settings"):
        await m.answer("Ключи текстов: " + ", ".join(DEFAULTS) + "\n/settext ключ текст (HTML разрешён)")


@router.message(Command("settext"))
async def settext(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "settings"):
        return
    a = args(command, 1)
    if len(a) < 2 or a[0] not in DEFAULTS:
        return await m.answer("Использование: /settext ключ текст. Ключи: " + ", ".join(DEFAULTS))
    await app.db.set_setting(f"text:{a[0]}", a[1])
    await app.db.audit(user["id"], "settext", a[0])
    await m.answer("✅ Сохранено.")


@router.message(Command("plan_set"))
async def plan_set(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "settings"):
        return
    a = args(command)
    try:
        assert len(a) == 6 and re.fullmatch(r"[\w-]{1,20}", a[0])
        plan = {"id": a[0], "name": a[1], "ram_mb": int(a[2]), "disk_mb": int(a[3]), "cpu": int(a[4]),
                "versions": [v for v in a[5].split(",") if re.fullmatch(r"[\w.\-]{1,16}", v)]}
        assert plan["versions"] and min(plan["ram_mb"], plan["disk_mb"], plan["cpu"]) > 0
    except (AssertionError, ValueError):
        return await m.answer("Использование: /plan_set ID Название ОЗУ_МБ ДИСК_МБ CPU% версии,через,запятую")
    plans = [p for p in await app.db.plans() if p["id"] != plan["id"]] + [plan]
    await app.db.set_setting("plans", json.dumps(plans, ensure_ascii=False))
    await app.db.audit(user["id"], "plan_set", plan["id"])
    await m.answer("✅ Тариф сохранён.")


@router.message(Command("plan_del"))
async def plan_del(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "settings"):
        return
    pid = (args(command) or [""])[0]
    await app.db.set_setting("plans", json.dumps([p for p in await app.db.plans() if p["id"] != pid], ensure_ascii=False))
    await app.db.audit(user["id"], "plan_del", pid)
    await m.answer("✅ Готово.")


# ───────────── IP (только через API сайта) ─────────────
@router.message(Command("ipblock"))
async def ipblock(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "ip"):
        return
    a = args(command, 2)
    try:
        net = str(ipaddress.ip_network(a[0], strict=False))
        hours = int(a[1])
        assert 1 <= hours <= 24 * 365 and net.split("/")[1] != "0" and (ipaddress.ip_network(net).prefixlen >= (16 if ":" not in net else 48))
    except (IndexError, ValueError, AssertionError):
        return await m.answer("Использование: /ipblock CIDR ЧАСЫ(1..8760) причина. Слишком широкие подсети запрещены (IPv4 ≥ /16, IPv6 ≥ /48). "
                              "Срок обязателен — блокировка пересматривается по истечении.")
    try:
        await app.site.block_ip(net, hours, a[2] if len(a) > 2 else "", f"#{user['id']}")
    except SiteAPIError as e:
        return await m.answer(f"❌ Сайт не выполнил блокировку: {esc(str(e))}")
    await app.db.audit(user["id"], "ip_block", net, f"{hours}h")
    await m.answer(f"✅ Блокировка {net} на {hours} ч. передана сайту.")


@router.message(Command("ipunblock"))
async def ipunblock(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "ip"):
        return
    try:
        net = str(ipaddress.ip_network((args(command) or [""])[0], strict=False))
        await app.site.unblock_ip(net, f"#{user['id']}")
    except ValueError:
        return await m.answer("Использование: /ipunblock CIDR")
    except SiteAPIError as e:
        return await m.answer(f"❌ {esc(str(e))}")
    await app.db.audit(user["id"], "ip_unblock", net)
    await m.answer("✅ Готово.")


# ───────────── тикеты ─────────────
@router.message(Command("tickets"))
async def tickets(m: Message, app, user):
    if await gate(m, app, user, "tickets"):
        await m.answer(await render(app, "tickets"))


@router.message(Command("ticket"))
async def ticket(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "tickets"):
        return
    a = args(command)
    t = await app.db.one("SELECT * FROM tickets WHERE id=?", (int(a[0]),)) if a and a[0].isdigit() else None
    if not t:
        return await m.answer("Использование: /ticket ID")
    msgs = await app.db.all("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id", (t["id"],))
    await m.answer(f"📨 <b>#{t['id']}</b> от #{t['user_id']} ({t['status']})\n\n" + "\n".join(
        f"{'🛠' if x['staff'] else '👤'} {fmt(x['created_at'])}: {esc(x['text'])}" for x in msgs) + f"\n\n/reply {t['id']} текст")


@router.message(Command("reply"))
async def reply(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "tickets"):
        return
    a = args(command, 1)
    t = await app.db.one("SELECT * FROM tickets WHERE id=? AND status='open'", (int(a[0]),)) if len(a) == 2 and a[0].isdigit() else None
    if not t:
        return await m.answer("Использование: /reply ID текст (тикет должен быть открыт)")
    await app.db.insert("INSERT INTO ticket_messages(ticket_id,staff,author_id,text,created_at) VALUES(?,?,?,?,?)", (t["id"], 1, user["id"], a[1], now()))
    owner = await app.db.user_by_id(t["user_id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Ответить", callback_data=f"tk:reply:{t['id']}")
    ok = await safe_send(app.bot, owner["tg_id"], f"🛠 Ответ поддержки по обращению <b>#{t['id']}</b>:\n\n{esc(a[1])}", reply_markup=kb.as_markup())
    await app.db.audit(user["id"], "ticket_reply", str(t["id"]))
    await m.answer("✅ Отправлено." if ok else "Сохранено, но пользователь недоступен для бота.")


@router.message(Command("close"))
async def close(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "tickets"):
        return
    a = args(command)
    n = await app.db.exec("UPDATE tickets SET status='closed' WHERE id=?", (int(a[0]),)) if a and a[0].isdigit() else 0
    await app.db.audit(user["id"], "ticket_close", " ".join(a))
    await m.answer("✅ Закрыто." if n else "Не найдено.")


@router.message(Command("escalate"))
async def escalate(m: Message, command: CommandObject, app, user):
    if not await gate(m, app, user, "tickets"):
        return
    a = args(command)
    if not a or not a[0].isdigit():
        return await m.answer("Использование: /escalate ID")
    owners = await app.db.all("SELECT u.tg_id FROM roles r JOIN users u ON u.id=r.user_id WHERE r.role='owner'")
    for o in owners:
        await safe_send(app.bot, o["tg_id"], f"🔺 Тикет <b>#{a[0]}</b> передан вам сотрудником #{user['id']}\n/ticket {a[0]}")
    await app.db.audit(user["id"], "ticket_escalate", a[0])
    await m.answer("✅ Передано владельцу.")


@router.message(Command("stats"))
async def stats(m: Message, app, user):
    if await gate(m, app, user, "stats"):
        await m.answer(await render(app, "stats"))


@router.message(Command("log"))
async def log_cmd(m: Message, app, user):
    if await gate(m, app, user, "audit"):
        await m.answer(await render(app, "log"))
