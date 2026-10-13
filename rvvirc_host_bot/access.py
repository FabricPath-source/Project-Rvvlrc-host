import json

ALL_PERMS = {
    "users_view": "просмотр пользователей", "tickets": "тикеты поддержки",
    "servers_view": "просмотр серверов", "users_manage": "управление пользователями",
    "roles_support": "выдача роли техподдержки", "roles_assistant": "назначение помощников",
    "promos": "промокоды", "subs": "обязательные подписки", "servers_manage": "управление серверами",
    "bans": "блокировки и чёрный список", "audit": "журнал действий", "settings": "настройки и тарифы",
    "stats": "статистика", "rewards": "заявки и награды", "ip": "блокировка IP (через API сайта)",
}
SUPPORT_PERMS = {"users_view", "tickets", "servers_view"}
ROLE_NAMES = {"owner": "Владелец", "assistant": "Помощник владельца", "support": "Техподдержка", None: "Пользователь"}


async def perms_of(db, uid: int) -> set:
    r = await db.get_role(uid)
    if not r:
        return set()
    if r["role"] == "owner":
        return set(ALL_PERMS)
    if r["role"] == "support":
        return set(SUPPORT_PERMS)
    return (set(json.loads(r["perms"] or "[]")) & set(ALL_PERMS)) | SUPPORT_PERMS


async def role_of(db, uid: int):
    r = await db.get_role(uid)
    return r["role"] if r else None
