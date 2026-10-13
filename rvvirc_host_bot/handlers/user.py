import html
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from access import ROLE_NAMES, role_of
from db import now
from handlers.ui import home_kb, main_menu, show
from integrations.panel_api import PanelError
from integrations.site_api import SiteAPIError
from services import check_subs, confirm_referral, esc, fmt, safe_send, servers_quota, staff_tg_ids, uname, user_locks
from texts import get_text

router = Router(name="user")
router.message.filter(F.chat.type == "private")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class S(StatesGroup):
    email = State()
    promo = State()
    ticket = State()
    ticket_reply = State()


# ───────────── /start и меню ─────────────
@router.message(CommandStart())
async def start(m: Message, command: CommandObject, app, user, is_new, state: FSMContext):
    await state.clear()
    arg = command.args or ""
    if is_new and arg.startswith("ref_"):
        ref = await app.db.one("SELECT * FROM users WHERE referral_code=?", (arg[4:],))
        if ref and ref["id"] != user["id"]:
            await app.db.exec("UPDATE users SET referrer_id=? WHERE id=? AND referrer_id IS NULL", (ref["id"], user["id"]))
            await app.db.exec("INSERT OR IGNORE INTO referrals(referrer_id,referred_id,status,created_at) "
                              "VALUES(?,?,'pending',?)", (ref["id"], user["id"], now()))
    await m.answer(await get_text(app.db, "welcome"), reply_markup=await main_menu(app, user))


@router.message(Command("cancel"))
async def cancel(m: Message, app, user, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=await main_menu(app, user))


@router.callback_query(F.data == "m:home")
async def home(cb: CallbackQuery, app, user, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb, await get_text(app.db, "welcome"), await main_menu(app, user))


@router.callback_query(F.data == "m:rules")
async def rules(cb: CallbackQuery, app, user):
    await cb.answer()
    await show(cb, await get_text(app.db, "rules"), home_kb())


# ───────────── подписки → реферальная система → сервер ─────────────
async def gate_subs(target, app, user, back: str):
    """Проверяет подписки. True — всё выполнено; иначе показывает список ресурсов."""
    missing, errors = await check_subs(app, user["tg_id"])
    if missing:
        b = InlineKeyboardBuilder()
        lines = ["📢 <b>Для продолжения подпишитесь на обязательные ресурсы:</b>\n"]
        for s in missing:
            b.button(text=f"➕ {s['title']}", url=s["url"])
            lines.append(f"• {esc(s['title'])}" + (" ⚠️ <i>не удалось проверить — сообщите администрации</i>" if s["id"] in errors else ""))
        b.button(text="✅ Проверить подписку", callback_data=f"sub:{back}")
        b.button(text="⬅️ В меню", callback_data="m:home")
        b.adjust(1)
        await show(target, "\n".join(lines), b.as_markup())
        return False
    if user["subs_ok_at"] is None:
        await app.db.exec("UPDATE users SET subs_ok_at=? WHERE id=?", (now(), user["id"]))
    await confirm_referral(app, user)
    return True


async def ref_screen(target, app, user, header=""):
    db = app.db
    need, cnt = await db.int_setting("ref_required"), await db.ref_count(user["id"])
    link = f"https://t.me/{app.bot_username}?start=ref_{user['referral_code']}"
    bar = "🟩" * min(cnt, need) + "⬜" * max(0, need - cnt)
    hist = await db.all(
        "SELECT r.status, r.created_at, u.username, u.tg_id FROM referrals r JOIN users u ON u.id=r.referred_id "
        "WHERE r.referrer_id=? ORDER BY r.id DESC LIMIT 10", (user["id"],))
    icons = {"confirmed": "✅", "pending": "⏳", "rejected": "❌"}
    h = "\n".join(f"{icons.get(r['status'], '•')} {uname(r)} — {fmt(r['created_at'])}" for r in hist) or "пока пусто"
    text = (f"{header}👥 <b>Реферальная система</b>\n\nВаша ссылка:\n<code>{link}</code>\n\n"
            f"Приглашено: <b>{min(cnt, need)} из {need}</b>\n{bar}\n\n"
            f"<b>История приглашений:</b>\n{h}\n\n"
            "<i>Реферал засчитывается, когда новый пользователь впервые запускает бота по вашей ссылке "
            "и выполняет подписки.</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Скопировать ссылку", copy_text=CopyTextButton(text=link))],
        [InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={link}")],
        [InlineKeyboardButton(text="🎮 К серверу", callback_data="m:free")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="m:home")],
    ])
    await show(target, text, kb)


async def free_screen(target, app, user):
    db = app.db
    user = await db.user_by_id(user["id"])
    if await db.active_ban(user["id"], ("free_server",)):
        return await show(target, "🚫 Вам закрыт доступ к бесплатным серверам. Обратитесь в поддержку.", home_kb())
    if not await gate_subs(target, app, user, "free"):
        return
    need, cnt = await db.int_setting("ref_required"), await db.ref_count(user["id"])
    if cnt < need:
        return await ref_screen(target, app, user, f"Осталось пригласить: <b>{need - cnt}</b>\n\n")
    used, allowed = await servers_quota(app, user)
    if used >= allowed:
        return await show(target, "✅ Вы уже получили бесплатный сервер(ы). Управление — в «Мои серверы».",
                          home_kb(("🖥 Мои серверы", "m:servers")))
    if not user["email"]:
        return await show(target, "🎁 Все условия выполнены! Осталось зарегистрироваться на сайте.",
                          home_kb(("✉️ Указать почту", "free:email")))
    if not user["email_verified"]:
        return await show(target, f"📧 Почта <b>{esc(user['email'])}</b> ещё не подтверждена на сайте.\n"
                                  "Подтвердите её по письму и нажмите «Проверить».",
                          home_kb(("🔄 Проверить подтверждение", "free:verify"), ("✏️ Изменить почту", "free:email")))
    b = InlineKeyboardBuilder()
    lines = ["🎁 <b>Выберите тариф бесплатного сервера:</b>\n"]
    for p in await db.plans():
        lines.append(f"• <b>{esc(p['name'])}</b> — {p['ram_mb']} МБ ОЗУ, {p['disk_mb']} МБ диск, CPU {p['cpu']}%")
        b.button(text=p["name"], callback_data=f"plan:{p['id']}")
    b.button(text="⬅️ В меню", callback_data="m:home")
    b.adjust(1)
    await show(target, "\n".join(lines) if len(lines) > 1 else "Тарифы пока не настроены владельцем.", b.as_markup())


@router.callback_query(F.data == "m:free")
async def m_free(cb: CallbackQuery, app, user):
    await cb.answer()
    await free_screen(cb, app, user)


@router.callback_query(F.data == "m:ref")
async def m_ref(cb: CallbackQuery, app, user):
    await cb.answer()
    user = await app.db.user_by_id(user["id"])
    if await gate_subs(cb, app, user, "ref"):
        await ref_screen(cb, app, user)


@router.callback_query(F.data.startswith("sub:"))
async def sub_check(cb: CallbackQuery, app, user):
    await cb.answer()
    user = await app.db.user_by_id(user["id"])
    if cb.data == "sub:ref":
        if await gate_subs(cb, app, user, "ref"):
            await ref_screen(cb, app, user)
    else:
        await free_screen(cb, app, user)


# ───────────── регистрация на сайте ─────────────
@router.callback_query(F.data == "free:email")
async def ask_email(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(S.email)
    await show(cb, "Введите адрес электронной почты, который будет использоваться для регистрации на RVVIRC HOST.\n\n"
                   "⚠️ Пароль в Telegram отправлять не нужно. /cancel — отмена.")


@router.message(S.email)
async def got_email(m: Message, app, user, state: FSMContext):
    email = (m.text or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        return await m.answer("Некорректный формат почты. Попробуйте ещё раз или /cancel.")
    db = app.db
    user = await db.user_by_id(user["id"])
    need = await db.int_setting("ref_required")
    if (await db.active_ban(user["id"], ("free_server",)) or user["subs_ok_at"] is None
            or await db.ref_count(user["id"]) < need):
        await state.clear()
        return await m.answer("Условия получения сервера не выполнены.", reply_markup=await main_menu(app, user))
    other = await db.one("SELECT id FROM users WHERE email=? AND id!=?", (email, user["id"]))
    if other:
        return await m.answer("Эта почта уже привязана к другому аккаунту. Укажите другую.")
    try:
        res = await app.site.register_account(email, user["tg_id"], user["id"])
    except SiteAPIError as e:
        await state.clear()
        return await m.answer(f"❌ Не удалось связаться с сайтом: {esc(str(e))}\nПопробуйте позже или напишите в поддержку.",
                              reply_markup=await main_menu(app, user))
    await state.clear()
    if res.status == "rejected":
        return await m.answer(f"❌ Сайт отклонил регистрацию: {esc(res.message or 'без пояснения')}",
                              reply_markup=await main_menu(app, user))
    await db.exec("UPDATE users SET email=?, email_verified=?, site_account_id=? WHERE id=?",
                  (email, int(res.email_verified), res.account_id, user["id"]))
    if res.email_verified:
        await m.answer("✅ Аккаунт на сайте готов.", reply_markup=home_kb(("🎁 Выбрать тариф", "m:free")))
    else:
        await m.answer(f"📧 Письмо с подтверждением отправлено на <b>{esc(email)}</b>.\n"
                       "Подтвердите почту и нажмите «Проверить».",
                       reply_markup=home_kb(("🔄 Проверить подтверждение", "free:verify")))


@router.callback_query(F.data == "free:verify")
async def verify(cb: CallbackQuery, app, user):
    user = await app.db.user_by_id(user["id"])
    if not user["email"]:
        return await cb.answer("Сначала укажите почту.", show_alert=True)
    try:
        st = await app.site.account_status(user["email"], user["tg_id"])
    except SiteAPIError as e:
        return await cb.answer(f"Сайт недоступен: {e}", show_alert=True)
    if not st.get("email_verified"):
        return await cb.answer("Почта пока не подтверждена.", show_alert=True)
    await app.db.exec("UPDATE users SET email_verified=1, site_account_id=COALESCE(?,site_account_id) WHERE id=?",
                      (st.get("account_id"), user["id"]))
    await cb.answer("Почта подтверждена ✅")
    await free_screen(cb, app, user)


# ───────────── создание сервера ─────────────
@router.callback_query(F.data.startswith("plan:"))
async def pick_plan(cb: CallbackQuery, app, user):
    await cb.answer()
    plan = next((p for p in await app.db.plans() if p["id"] == cb.data[5:]), None)
    if not plan:
        return await show(cb, "Тариф не найден.", home_kb())
    if len(plan["versions"]) == 1:
        return await create_server(cb, app, user, plan, plan["versions"][0])
    b = InlineKeyboardBuilder()
    for v in plan["versions"]:
        b.button(text=f"Minecraft {v}", callback_data=f"mk:{plan['id']}:{v}")
    b.button(text="⬅️ Назад", callback_data="m:free")
    b.adjust(2)
    await show(cb, "Выберите версию Minecraft:", b.as_markup())


@router.callback_query(F.data.startswith("mk:"))
async def pick_version(cb: CallbackQuery, app, user):
    await cb.answer()
    _, pid, ver = cb.data.split(":", 2)
    plan = next((p for p in await app.db.plans() if p["id"] == pid), None)
    if not plan or ver not in plan["versions"]:
        return await show(cb, "Тариф или версия не найдены.", home_kb())
    await create_server(cb, app, user, plan, ver)


async def create_server(cb, app, user, plan, version):
    db = app.db
    async with user_locks[user["id"]]:                       # защита от двойного нажатия
        user = await db.user_by_id(user["id"])
        need = await db.int_setting("ref_required")
        missing, _ = await check_subs(app, user["tg_id"])
        used, allowed = await servers_quota(app, user)
        if (missing or await db.active_ban(user["id"], ("free_server",)) or await db.ref_count(user["id"]) < need
                or not user["email_verified"] or not user["site_account_id"] or used >= allowed):
            return await show(cb, "Условия получения сервера не выполнены или лимит исчерпан.", home_kb())
        sid = await db.insert("INSERT INTO servers(user_id,plan_id,status,version,created_at) VALUES(?,?,?,?,?)",
                              (user["id"], plan["id"], "creating", version, now()))
        try:
            info = await app.panel.create_server(user["site_account_id"], plan, version)
        except PanelError as e:
            await db.exec("UPDATE servers SET status='failed', error=? WHERE id=?", (str(e), sid))
            await db.audit(None, "server_create_failed", f"#{user['id']}", str(e))
            for tg in await staff_tg_ids(app, "servers_manage"):
                await safe_send(app.bot, tg, f"⚠️ Ошибка создания сервера для #{user['id']}: {esc(str(e))}")
            return await show(cb, f"❌ Не удалось создать сервер: {esc(str(e))}\nОшибка передана администрации.",
                              home_kb(("🆘 Поддержка", "m:support")))
        await db.exec("UPDATE servers SET panel_server_id=?, status=?, address=?, version=COALESCE(?,version) WHERE id=?",
                      (info.panel_id, info.status, info.address, info.version, sid))
    await show(cb, f"✅ Сервер создан!\nАдрес: <code>{esc(info.address or 'уточняется')}</code>",
               home_kb(("🖥 Мои серверы", "m:servers")))


@router.callback_query(F.data == "m:servers")
async def my_servers(cb: CallbackQuery, app, user):
    await cb.answer()
    rows = await app.db.all("SELECT * FROM servers WHERE user_id=? AND status!='failed' ORDER BY id", (user["id"],))
    if not rows:
        return await show(cb, "У вас пока нет серверов.", home_kb(("🎮 Получить бесплатный", "m:free")))
    plans = {p["id"]: p for p in await app.db.plans()}
    b, lines = InlineKeyboardBuilder(), ["🖥 <b>Мои серверы</b>\n"]
    for s in rows:
        if s["panel_server_id"] and app.panel.configured:
            try:
                info = await app.panel.get_server(s["panel_server_id"])
                await app.db.exec("UPDATE servers SET status=?, address=COALESCE(?,address) WHERE id=?",
                                  (info.status, info.address, s["id"]))
                s = await app.db.one("SELECT * FROM servers WHERE id=?", (s["id"],))
            except PanelError:
                pass
        p = plans.get(s["plan_id"])
        res = f"{p['ram_mb']} МБ ОЗУ / {p['disk_mb']} МБ диск / CPU {p['cpu']}%" if p else s["plan_id"]
        lines.append(f"<b>#{s['id']}</b> — {esc(s['status'])}\nАдрес: <code>{esc(s['address'] or '—')}</code>\n"
                     f"Версия: {esc(s['version'] or '—')}\nРесурсы: {esc(res)}\n")
        if s["panel_server_id"]:
            for act, label in (("start", "▶️"), ("stop", "⏹"), ("restart", "🔄")):
                b.button(text=f"{label} #{s['id']}", callback_data=f"srv:{act}:{s['id']}")
    b.button(text="🔃 Обновить", callback_data="m:servers")
    b.button(text="⬅️ В меню", callback_data="m:home")
    b.adjust(3, 1)
    await show(cb, "\n".join(lines), b.as_markup())


@router.callback_query(F.data.startswith("srv:"))
async def srv_power(cb: CallbackQuery, app, user):
    _, act, sid = cb.data.split(":")
    s = await app.db.one("SELECT * FROM servers WHERE id=? AND user_id=?", (int(sid), user["id"]))  # только свои
    if not s or not s["panel_server_id"] or act not in ("start", "stop", "restart"):
        return await cb.answer("Сервер недоступен.", show_alert=True)
    try:
        await app.panel.power(s["panel_server_id"], act)
    except PanelError as e:
        return await cb.answer(f"Не удалось: {e}", show_alert=True)
    await cb.answer("Команда отправлена ✅")


# ───────────── профиль ─────────────
@router.callback_query(F.data == "m:profile")
async def profile(cb: CallbackQuery, app, user):
    await cb.answer()
    db = app.db
    user = await db.user_by_id(user["id"])
    need, cnt = await db.int_setting("ref_required"), await db.ref_count(user["id"])
    used, allowed = await servers_quota(app, user)
    limited = await db.active_ban(user["id"], ("free_server", "referral_rewards"))
    email = "не привязана" if not user["email"] else f"{esc(user['email'])} ({'✅ подтверждена' if user['email_verified'] else '⏳ не подтверждена'})"
    promos = await db.all("SELECT p.code, pu.used_at FROM promo_uses pu JOIN promos p ON p.id=pu.promo_id "
                          "WHERE pu.user_id=? ORDER BY pu.id DESC LIMIT 10", (user["id"],))
    ptxt = "\n".join(f"• {esc(p['code'])} — {fmt(p['used_at'])}" for p in promos) or "нет"
    sup = await db.setting("support_url") or (f"@{await db.setting('support_username')}" if await db.setting("support_username") else "через «Техническая поддержка»")
    await show(cb, (
        "👤 <b>Мой профиль</b>\n\n"
        f"Telegram ID: <code>{user['tg_id']}</code>\n"
        f"Username: {('@' + esc(user['username'])) if user['username'] else '—'}\n"
        f"ID аккаунта RVVIRC HOST: <b>#{user['id']}</b>\n"
        f"Регистрация: {fmt(user['created_at'])}\n"
        f"Статус: {'⚠️ есть ограничения' if limited else '✅ активен'}\n"
        f"Почта: {email}\n"
        f"Приглашено: {min(cnt, need)} из {need}\n"
        f"Доступно наград: {max(0, allowed - used) if cnt >= need else 0}\n"
        f"Создано серверов: {used}\n"
        f"Уровень доступа: {ROLE_NAMES[await role_of(db, user['id'])]}\n\n"
        f"<b>Промокоды:</b>\n{ptxt}\n\nПоддержка: {esc(sup)}"), home_kb())


# ───────────── промокод ─────────────
@router.callback_query(F.data == "m:promo")
async def promo_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(S.promo)
    await show(cb, "🎟 Отправьте промокод одним сообщением. /cancel — отмена.")


@router.message(S.promo)
async def promo_got(m: Message, app, user, state: FSMContext):
    await state.clear()
    db, code = app.db, (m.text or "").strip().upper()
    p = await db.one("SELECT * FROM promos WHERE code=? AND deleted=0", (code,))
    user = await db.user_by_id(user["id"])
    kb = await main_menu(app, user)
    if not p or not p["active"] or (p["expires_at"] and p["expires_at"] < now()):
        return await m.answer("❌ Промокод недействителен или истёк.", reply_markup=kb)
    total = (await db.one("SELECT COUNT(*) c FROM promo_uses WHERE promo_id=?", (p["id"],)))["c"]
    mine = (await db.one("SELECT COUNT(*) c FROM promo_uses WHERE promo_id=? AND user_id=?", (p["id"], user["id"])))["c"]
    if (p["max_uses"] and total >= p["max_uses"]) or mine >= p["per_user"]:
        return await m.answer("❌ Лимит активаций этого промокода исчерпан.", reply_markup=kb)
    if not user["site_account_id"] or not user["email_verified"]:
        return await m.answer("Сначала зарегистрируйтесь на сайте и подтвердите почту («Бесплатный сервер»).", reply_markup=kb)
    async with user_locks[user["id"]]:
        try:
            applied, msg = await app.site.apply_promo(user["site_account_id"], p["code"], p["percent"])
        except SiteAPIError as e:
            return await m.answer(f"❌ Сайт не подтвердил применение: {esc(str(e))}", reply_markup=kb)
        if not applied:
            return await m.answer(f"❌ Сайт отклонил промокод: {esc(msg or 'без пояснения')}", reply_markup=kb)
        await db.insert("INSERT INTO promo_uses(promo_id,user_id,used_at) VALUES(?,?,?)", (p["id"], user["id"], now()))
    await m.answer(f"✅ Промокод <b>{esc(p['code'])}</b> применён на сайте: скидка {p['percent']}%.", reply_markup=kb)


# ───────────── поддержка ─────────────
@router.callback_query(F.data == "m:support")
async def support(cb: CallbackQuery, app, user):
    await cb.answer()
    db = app.db
    url, uname_ = await db.setting("support_url"), await db.setting("support_username")
    b = InlineKeyboardBuilder()
    if url or uname_:
        b.button(text="💬 Написать в поддержку", url=url or f"https://t.me/{uname_}")
    b.button(text="✉️ Создать обращение", callback_data="tk:new")
    b.button(text="📨 Мои обращения", callback_data="tk:list")
    b.button(text="⬅️ В меню", callback_data="m:home")
    b.adjust(1)
    await show(cb, "🆘 <b>Техническая поддержка</b>\nОпишите проблему — ответ придёт сюда, в бота.", b.as_markup())


@router.callback_query(F.data == "tk:new")
async def tk_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(S.ticket)
    await show(cb, "Опишите проблему одним сообщением. /cancel — отмена.")


@router.message(S.ticket)
async def tk_got(m: Message, app, user, state: FSMContext):
    text = (m.text or "").strip()[:3000]
    if not text:
        return await m.answer("Отправьте текстовое сообщение.")
    await state.clear()
    open_n = (await app.db.one("SELECT COUNT(*) c FROM tickets WHERE user_id=? AND status='open'", (user["id"],)))["c"]
    if open_n >= 3:
        return await m.answer("У вас уже 3 открытых обращения. Дождитесь ответа.", reply_markup=await main_menu(app, user))
    tid = await app.db.insert("INSERT INTO tickets(user_id,subject,created_at) VALUES(?,?,?)", (user["id"], text[:60], now()))
    await app.db.insert("INSERT INTO ticket_messages(ticket_id,staff,author_id,text,created_at) VALUES(?,?,?,?,?)",
                        (tid, 0, user["id"], text, now()))
    for tg in await staff_tg_ids(app, "tickets"):
        await safe_send(app.bot, tg, f"📩 Новое обращение <b>#{tid}</b> от #{user['id']}\n/ticket {tid}")
    await m.answer(f"✅ Обращение #{tid} создано. Мы ответим здесь.", reply_markup=await main_menu(app, user))


@router.callback_query(F.data == "tk:list")
async def tk_list(cb: CallbackQuery, app, user):
    await cb.answer()
    rows = await app.db.all("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],))
    txt = "\n".join(f"#{t['id']} — {'🟢 открыто' if t['status'] == 'open' else '⚪️ закрыто'} — {esc(t['subject'])}" for t in rows)
    await show(cb, "📨 <b>Мои обращения</b>\n\n" + (txt or "пока нет"), home_kb())


@router.callback_query(F.data.startswith("tk:reply:"))
async def tk_reply_btn(cb: CallbackQuery, app, user, state: FSMContext):
    tid = int(cb.data.split(":")[2])
    t = await app.db.one("SELECT * FROM tickets WHERE id=? AND user_id=? AND status='open'", (tid, user["id"]))
    if not t:
        return await cb.answer("Обращение закрыто.", show_alert=True)
    await cb.answer()
    await state.set_state(S.ticket_reply)
    await state.update_data(tid=tid)
    await cb.message.answer(f"Напишите ответ по обращению #{tid}. /cancel — отмена.")


@router.message(S.ticket_reply)
async def tk_reply_got(m: Message, app, user, state: FSMContext):
    tid = (await state.get_data()).get("tid")
    await state.clear()
    t = await app.db.one("SELECT * FROM tickets WHERE id=? AND user_id=? AND status='open'", (tid, user["id"]))
    if not t or not (m.text or "").strip():
        return await m.answer("Не удалось добавить сообщение.")
    await app.db.insert("INSERT INTO ticket_messages(ticket_id,staff,author_id,text,created_at) VALUES(?,?,?,?,?)",
                        (tid, 0, user["id"], m.text.strip()[:3000], now()))
    for tg in await staff_tg_ids(app, "tickets"):
        await safe_send(app.bot, tg, f"💬 Новое сообщение в обращении <b>#{tid}</b>\n/ticket {tid}")
    await m.answer("✅ Отправлено.")
