"""Клиент API сайта RVVIRC HOST.

Сайт ДОЛЖЕН реализовать контракт, описанный в README (раздел «Контракт API сайта»).
Пока SITE_API_URL/SITE_API_TOKEN не заданы, любые вызовы завершаются ошибкой SiteAPIError —
бот в этом случае честно сообщает, что регистрация недоступна, и ничего не «изображает».
"""
import asyncio
from dataclasses import dataclass

import aiohttp


class SiteAPIError(Exception):
    pass


@dataclass
class RegResult:
    status: str                 # created | verification_sent | rejected
    account_id: str | None = None
    email_verified: bool = False
    message: str = ""


class SiteAPI:
    def __init__(self, base_url: str, token: str):
        self.base_url, self.token = base_url, token

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    async def _post(self, path: str, payload: dict, idem: str | None = None) -> dict:
        if not self.configured:
            raise SiteAPIError("Интеграция с сайтом не настроена")
        headers = {"Authorization": f"Bearer {self.token}"}
        if idem:
            headers["Idempotency-Key"] = idem
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(self.base_url + path, json=payload, headers=headers) as r:
                    data = await r.json(content_type=None)
                    if not isinstance(data, dict):
                        raise SiteAPIError("Некорректный ответ сайта")
                    if r.status >= 400:
                        raise SiteAPIError(str(data.get("error") or f"HTTP {r.status}"))
                    return data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise SiteAPIError(f"Сайт недоступен: {e}") from e

    async def register_account(self, email: str, tg_id: int, bot_user_id: int) -> RegResult:
        d = await self._post("/bot/v1/accounts/register",
                             {"email": email, "telegram_id": tg_id, "bot_user_id": bot_user_id},
                             idem=f"reg:{tg_id}:{email}")
        if d.get("status") not in ("created", "verification_sent", "rejected"):
            raise SiteAPIError("Сайт вернул неизвестный статус регистрации")
        return RegResult(d["status"], d.get("account_id"), bool(d.get("email_verified")), d.get("message", ""))

    async def account_status(self, email: str, tg_id: int) -> dict:
        """Ответ: {"account_id": str|None, "email_verified": bool}"""
        return await self._post("/bot/v1/accounts/status", {"email": email, "telegram_id": tg_id})

    async def apply_promo(self, account_id: str, code: str, percent: int) -> tuple[bool, str]:
        d = await self._post("/bot/v1/promo/apply",
                             {"account_id": account_id, "code": code, "percent": percent},
                             idem=f"promo:{account_id}:{code}")
        return bool(d.get("applied")), d.get("message", "")

    async def block_ip(self, cidr: str, hours: int, reason: str, admin_ref: str):
        await self._post("/bot/v1/ip/block", {"cidr": cidr, "hours": hours, "reason": reason, "admin": admin_ref})

    async def unblock_ip(self, cidr: str, admin_ref: str):
        await self._post("/bot/v1/ip/unblock", {"cidr": cidr, "admin": admin_ref})
