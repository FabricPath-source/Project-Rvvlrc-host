import asyncio
import json
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id INTEGER UNIQUE NOT NULL,
  username TEXT,
  referral_code TEXT UNIQUE NOT NULL,
  referrer_id INTEGER REFERENCES users(id),
  created_at INTEGER NOT NULL,
  subs_ok_at INTEGER,
  email TEXT,
  email_verified INTEGER NOT NULL DEFAULT 0,
  site_account_id TEXT,
  bonus_servers INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users(email) WHERE email IS NOT NULL;
CREATE TABLE IF NOT EXISTS roles(
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  role TEXT NOT NULL CHECK(role IN ('owner','assistant','support')),
  perms TEXT NOT NULL DEFAULT '[]',
  granted_by INTEGER, granted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, chat_id TEXT NOT NULL, url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS referrals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  referrer_id INTEGER NOT NULL REFERENCES users(id),
  referred_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL, confirmed_at INTEGER
);
CREATE TABLE IF NOT EXISTS ref_adjustments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL,
  admin_id INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL, percent INTEGER NOT NULL,
  expires_at INTEGER, max_uses INTEGER NOT NULL DEFAULT 0, per_user INTEGER NOT NULL DEFAULT 1,
  plans TEXT NOT NULL DEFAULT '*', min_amount REAL NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_uses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  promo_id INTEGER NOT NULL, user_id INTEGER NOT NULL, used_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL, scope TEXT NOT NULL, reason TEXT NOT NULL,
  starts_at INTEGER NOT NULL, ends_at INTEGER, active INTEGER NOT NULL DEFAULT 1,
  admin_id INTEGER NOT NULL, lifted_by INTEGER, lifted_at INTEGER
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id INTEGER, action TEXT NOT NULL, target TEXT, details TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS servers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL, plan_id TEXT NOT NULL, panel_server_id TEXT,
  status TEXT NOT NULL, address TEXT, version TEXT, error TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL, staff INTEGER NOT NULL, author_id INTEGER NOT NULL,
  text TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

DEFAULT_PLANS = [
    {"id": "free-1", "name": "Free-1GB", "ram_mb": 1024, "disk_mb": 5120, "cpu": 100,
     "versions": ["1.20.4", "1.21.1"]},
]
DEFAULT_SETTINGS = {
    "ref_required": "3",
    "max_free_servers": "1",
    "support_username": "",
    "support_url": "",
    "plans": json.dumps(DEFAULT_PLANS, ensure_ascii=False),
}


def now() -> int:
    return int(time.time())


class DB:
    """SQLite с одним соединением; вызовы уходят в поток, чтобы не блокировать event loop."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    def _run(self, sql, params, mode):
        with self._lock:
            cur = self.conn.execute(sql, params)
            if mode == "one":
                return cur.fetchone()
            if mode == "all":
                return cur.fetchall()
            self.conn.commit()
            return cur.lastrowid if mode == "insert" else cur.rowcount

    async def one(self, sql, params=()):
        return await asyncio.to_thread(self._run, sql, params, "one")

    async def all(self, sql, params=()):
        return await asyncio.to_thread(self._run, sql, params, "all")

    async def exec(self, sql, params=()):
        """Возвращает число затронутых строк."""
        return await asyncio.to_thread(self._run, sql, params, "exec")

    async def insert(self, sql, params=()):
        return await asyncio.to_thread(self._run, sql, params, "insert")

    # ---- настройки ----
    async def setting(self, key: str) -> str:
        row = await self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    async def set_setting(self, key: str, value: str):
        await self.exec("INSERT INTO settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    async def int_setting(self, key: str) -> int:
        return int(await self.setting(key) or 0)

    async def plans(self) -> list:
        return json.loads(await self.setting("plans") or "[]")

    # ---- пользователи ----
    async def user_by_id(self, uid):
        return await self.one("SELECT * FROM users WHERE id=?", (uid,))

    async def user_by_tg(self, tg_id):
        return await self.one("SELECT * FROM users WHERE tg_id=?", (tg_id,))

    async def find_user(self, ref: str):
        """'#12' — внутренний ID, число — Telegram ID. Username намеренно не поддерживается."""
        ref = ref.strip()
        if ref.startswith("#") and ref[1:].isdigit():
            return await self.user_by_id(int(ref[1:]))
        if ref.isdigit():
            return await self.user_by_tg(int(ref))
        return None

    async def upsert_user(self, tg_id: int, username):
        """Возвращает (user, created)."""
        row = await self.user_by_tg(tg_id)
        if row:
            if row["username"] != username:
                await self.exec("UPDATE users SET username=? WHERE id=?", (username, row["id"]))
                row = await self.user_by_tg(tg_id)
            return row, False
        try:
            await self.insert(
                "INSERT INTO users(tg_id,username,referral_code,created_at) VALUES(?,?,?,?)",
                (tg_id, username, secrets.token_hex(5), now()))
            return await self.user_by_tg(tg_id), True
        except sqlite3.IntegrityError:
            return await self.user_by_tg(tg_id), False

    # ---- роли / баны ----
    async def get_role(self, uid):
        return await self.one("SELECT * FROM roles WHERE user_id=?", (uid,))

    async def active_ban(self, uid, scopes=("account", "blacklist")):
        q = ",".join("?" * len(scopes))
        return await self.one(
            f"SELECT * FROM bans WHERE user_id=? AND active=1 AND scope IN ({q}) "
            "AND (ends_at IS NULL OR ends_at>?) ORDER BY id DESC LIMIT 1",
            (uid, *scopes, now()))

    async def audit(self, admin_id, action, target="", details=""):
        await self.insert("INSERT INTO audit_log(admin_id,action,target,details,created_at) VALUES(?,?,?,?,?)",
                          (admin_id, action, target, details, now()))

    # ---- рефералы ----
    async def ref_count(self, uid) -> int:
        a = await self.one("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='confirmed'", (uid,))
        b = await self.one("SELECT COALESCE(SUM(delta),0) s FROM ref_adjustments WHERE user_id=?", (uid,))
        return max(0, a["c"] + b["s"])
