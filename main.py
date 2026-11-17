# -*- coding: utf-8 -*-
"""
JisanV2 Post Management Bot  (multi-account edition)
- Telethon controller bot + many userbot accounts
- Accounts are added from inside the bot; sessions are ENCRYPTED and
  stored in Firebase Realtime Database
- Premium inline-button panel (all old commands still work)
"""
import os
import sys
import json
import time
import random
import base64
import hashlib
import asyncio
import logging
import functools

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import InputPeerUser, MessageMediaWebPage

# The command-menu feature is optional: a Telethon version difference must
# never stop the whole bot from starting.
try:
    from telethon.tl.functions.bots import SetBotCommandsRequest
    from telethon.tl.types import BotCommand, BotCommandScopeDefault, BotCommandScopePeer
    MENU_OK = True
except ImportError:
    MENU_OK = False
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("jisanv2")


# ------------------------------------------------------------------
# Environment variables
# ------------------------------------------------------------------
def require_env(name, cast=str):
    raw = os.environ.get(name)
    if not raw:
        logger.critical("Missing environment variable: %s", name)
        sys.exit(1)
    try:
        return cast(raw)
    except ValueError:
        logger.critical("Invalid value for environment variable: %s", name)
        sys.exit(1)


API_ID = require_env("API_ID", int)
API_HASH = require_env("API_HASH")
BOT_TOKEN = require_env("BOT_TOKEN")
ADMIN_ID = require_env("ADMIN_ID", int)

# Optional: the old single account keeps working as the "Main" account
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
MAX_ACCOUNTS = int(os.environ.get("MAX_ACCOUNTS", "20") or 20)


# ------------------------------------------------------------------
# Local JSON helpers (fallback when Firebase is not configured)
# ------------------------------------------------------------------
GROUPS_FILE = "selected_groups.json"   # legacy: Main account groups
SETTINGS_FILE = "settings.json"
AUTOPOST_FILE = "autopost_ref.json"
PAYLOAD_DIR = "payload"


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to read %s: %s", path, e)
    return default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.error("Failed to save %s: %s", path, e)


# ------------------------------------------------------------------
# Firebase Realtime Database
# ------------------------------------------------------------------
class Store:
    """Thin async wrapper around firebase-admin (blocking calls run in threads)."""

    def __init__(self):
        self.ok = False
        self.db = None
        self.last_error = ""

    def init(self):
        creds_raw = (os.environ.get("FIREBASE_CREDENTIALS")
                     or os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON") or "").strip()
        db_url = (os.environ.get("FIREBASE_DB_URL")
                  or os.environ.get("FIREBASE_DATABASE_URL") or "").strip()
        if not creds_raw or not db_url:
            logger.warning("Firebase not configured (need FIREBASE_SERVICE_ACCOUNT_JSON / "
                           "FIREBASE_DATABASE_URL). Using local files; multi-account is disabled.")
            return
        try:
            import firebase_admin
            from firebase_admin import credentials, db

            cred = credentials.Certificate(json.loads(creds_raw))
            firebase_admin.initialize_app(cred, {"databaseURL": db_url})
            self.db = db
            self.ok = True
            logger.info("Firebase connected.")
        except Exception as e:
            self.last_error = type(e).__name__
            logger.error("Firebase init failed: %s", type(e).__name__)

    async def _run(self, fn):
        try:
            result = await asyncio.to_thread(fn)
            self.last_error = ""
            return result
        except Exception as e:
            self.last_error = type(e).__name__
            logger.error("Firebase error: %s", type(e).__name__)
            raise

    async def get(self, path):
        return await self._run(lambda: self.db.reference(path).get())

    async def set(self, path, value):
        return await self._run(lambda: self.db.reference(path).set(value))

    async def update(self, path, value):
        return await self._run(lambda: self.db.reference(path).update(value))

    async def delete(self, path):
        return await self._run(lambda: self.db.reference(path).delete())


store = Store()


async def kv_get(path, local_file):
    try:
        if store.ok:
            return await store.get(path)
    except Exception:
        return None
    return load_json(local_file, None)


async def kv_set(path, local_file, value):
    try:
        if store.ok:
            await store.set(path, value)
        else:
            save_json(local_file, value)
    except Exception:
        pass  # already logged by Store


# ------------------------------------------------------------------
# Encryption for session strings
# ------------------------------------------------------------------
def make_fernet():
    key = os.environ.get("ENCRYPTION_KEY", "")
    if len(key) < 16:
        return None
    dk = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), b"jisanv2-sessions", 200_000)
    return Fernet(base64.urlsafe_b64encode(dk))


fernet = make_fernet()


def accounts_ready():
    """Multi-account needs Firebase AND an encryption key."""
    return store.ok and fernet is not None


# ------------------------------------------------------------------
# State
# ------------------------------------------------------------------
START_TIME = time.time()
COOLDOWN_TIME = 3600
GROUPS_PER_PAGE = 8
GROUP_CACHE_TTL = 60

auto_post_interval = 3600
last_post_time = 0
admin_hash = None  # lets the bot reach the admin after a redeploy

waiting_users = set()
waiting_autopost_users = set()
waiting_session_users = set()
bot_users = set()

auto_delete_enabled = True
is_auto_post_running = False
auto_post_task = None
auto_payload = None          # saved Auto-Post content (text + entities + media file)
last_sent_messages = {}      # {(acc_id, group_id): [msg_ids]}
groups_cache = {}            # {acc_id: {"time": t, "items": [...]}}


class Account:
    def __init__(self, acc_id, name, session, enabled=True, groups=None,
                 is_main=False, user_id=None):
        self.id = acc_id
        self.name = name or "Account"
        self.session = session
        self.enabled = enabled
        self.groups = set(groups or [])
        self.is_main = is_main
        self.user_id = user_id
        self.client = None
        self.connected = False
        self.error = ""

    @property
    def label(self):
        return ("⭐ " if self.is_main else "") + self.name

    def icon(self):
        if not self.enabled:
            return "⏸"
        return "🟢" if self.connected else "🔴"

    def record(self, include_session=False):
        rec = {
            "name": self.name,
            "user_id": self.user_id,
            "enabled": self.enabled,
            "groups": sorted(self.groups),
        }
        if include_session and not self.is_main and self.session and fernet:
            rec["session_enc"] = fernet.encrypt(self.session.encode("utf-8")).decode("utf-8")
        return rec


accounts = {}  # acc_id -> Account

bot = TelegramClient("bot_session", API_ID, API_HASH)


def admin_peer():
    if admin_hash:
        return InputPeerUser(ADMIN_ID, admin_hash)
    return None


def total_targets():
    return sum(len(a.groups) for a in accounts.values() if a.enabled)


def active_accounts():
    return [a for a in accounts.values() if a.enabled and a.connected]


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------
async def save_settings():
    await kv_set("settings", SETTINGS_FILE, {
        "interval": auto_post_interval,
        "auto_delete": auto_delete_enabled,
        "auto_running": is_auto_post_running,
        "admin_hash": admin_hash,
    })


async def persist_account(acc):
    try:
        if store.ok:
            await store.update(f"accounts/{acc.id}", acc.record())
        elif acc.is_main:
            save_json(GROUPS_FILE, sorted(acc.groups))
    except Exception:
        pass


def display_name(me):
    name = (getattr(me, "first_name", None) or getattr(me, "username", None) or "Account")
    return clean_name(name)[:30]


async def connect_account(acc):
    if not acc.session:
        acc.connected = False
        acc.error = "Session decrypt failed (ENCRYPTION_KEY?)"
        return False
    try:
        if acc.client is None:
            acc.client = TelegramClient(StringSession(acc.session), API_ID, API_HASH)
        await asyncio.wait_for(acc.client.connect(), 30)
        if not await acc.client.is_user_authorized():
            acc.connected = False
            acc.error = "Session expired / invalid"
            return False
        me = await acc.client.get_me()
        acc.user_id = me.id
        if acc.is_main or not acc.name or acc.name == "Account":
            acc.name = display_name(me)
        acc.connected = True
        acc.error = ""
        return True
    except Exception as e:
        acc.connected = False
        acc.error = type(e).__name__
        logger.error("Account %s connect failed: %s", acc.id, type(e).__name__)
        return False


async def safe_disconnect(acc):
    try:
        if acc.client:
            await acc.client.disconnect()
    except Exception:
        pass
    acc.connected = False


def new_account_id():
    base = f"a{int(time.time())}"
    acc_id, n = base, 1
    while acc_id in accounts:
        n += 1
        acc_id = f"{base}{n}"
    return acc_id


async def add_account(session_text):
    """Validate + store a new session. Returns (ok, message, account)."""
    if not accounts_ready():
        return False, "Firebase ও ENCRYPTION_KEY সেট করা নেই।", None
    if len(accounts) >= MAX_ACCOUNTS:
        return False, f"সর্বোচ্চ {MAX_ACCOUNTS}টি অ্যাকাউন্ট যোগ করা যাবে।", None
    try:
        StringSession(session_text)
    except Exception:
        return False, "এটি সঠিক Telethon session string নয়।", None

    acc = Account(new_account_id(), "", session_text)
    if not await connect_account(acc):
        reason = acc.error
        await safe_disconnect(acc)
        return False, f"Session কাজ করছে না ({reason})।", None

    if any(a.user_id == acc.user_id for a in accounts.values()):
        await safe_disconnect(acc)
        return False, "এই অ্যাকাউন্টটি আগেই যোগ করা আছে।", None

    acc.record_added = int(time.time())
    try:
        rec = acc.record(include_session=True)
        rec["added_at"] = acc.record_added
        await store.set(f"accounts/{acc.id}", rec)
    except Exception:
        await safe_disconnect(acc)
        return False, "Firebase-এ সেভ করা যায়নি।", None

    accounts[acc.id] = acc
    return True, "ok", acc


async def remove_account(acc):
    await safe_disconnect(acc)
    accounts.pop(acc.id, None)
    groups_cache.pop(acc.id, None)
    if store.ok:
        try:
            await store.delete(f"accounts/{acc.id}")
        except Exception:
            return False
    return True


async def load_state():
    """Load settings, accounts and auto-post content at startup."""
    global auto_post_interval, auto_delete_enabled, admin_hash, auto_payload

    s = await kv_get("settings", SETTINGS_FILE) or {}
    if isinstance(s.get("interval"), int) and s["interval"] >= 3600:
        auto_post_interval = s["interval"]
    if isinstance(s.get("auto_delete"), bool):
        auto_delete_enabled = s["auto_delete"]
    if isinstance(s.get("admin_hash"), int):
        admin_hash = s["admin_hash"]
    resume = bool(s.get("auto_running"))

    data = {}
    if store.ok:
        try:
            data = await store.get("accounts") or {}
        except Exception:
            data = {}

    legacy_groups = load_json(GROUPS_FILE, [])

    if SESSION_STRING:
        main = Account("main", "Main", SESSION_STRING, is_main=True)
        rec = data.get("main") or {}
        main.enabled = rec.get("enabled", True)
        if "groups" in rec:
            main.groups = set(rec.get("groups") or [])
        else:
            main.groups = set(legacy_groups)
        accounts["main"] = main
        if store.ok and "main" not in data:
            await persist_account(main)  # one-time migration of old groups

    for acc_id, rec in data.items():
        if acc_id == "main" or not isinstance(rec, dict):
            continue
        session = ""
        token = rec.get("session_enc")
        if token and fernet:
            try:
                session = fernet.decrypt(token.encode("utf-8")).decode("utf-8")
            except InvalidToken:
                logger.error("Cannot decrypt session of %s (wrong ENCRYPTION_KEY?)", acc_id)
        accounts[acc_id] = Account(
            acc_id, rec.get("name"), session,
            enabled=bool(rec.get("enabled", True)),
            groups=rec.get("groups") or [],
            user_id=rec.get("user_id"),
        )

    if accounts:
        await asyncio.gather(*(connect_account(a) for a in accounts.values()))
        logger.info("Accounts loaded: %d (connected: %d)", len(accounts), len(active_accounts()))

    # Auto-post content
    ref = await kv_get("autopost", AUTOPOST_FILE)
    peer = admin_peer()
    if ref and peer:
        try:
            msg = await bot.get_messages(peer, ids=int(ref.get("msg_id")))
            if msg:
                auto_payload = await prepare_payload(msg, os.path.join(PAYLOAD_DIR, "autopost"))
        except Exception as e:
            logger.warning("Could not restore auto-post content: %s", type(e).__name__)

    if resume and auto_payload:
        ok, _ = await start_autopost()
        logger.info("Auto-Post resumed after restart: %s", ok)


# ------------------------------------------------------------------
# Render keep-alive web server
# ------------------------------------------------------------------
async def handle(request):
    return web.Response(text="JisanV2 Engine is Alive!")


async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Dummy Web Server running on port %s", port)


# ------------------------------------------------------------------
# Design helpers
# ------------------------------------------------------------------
DIV = "━━━━━━━━━━━━━━━━━━"

DENIED_TEXT = (
    "🔒 **Access Denied**\n"
    f"{DIV}\n"
    "এই কমান্ড শুধু **অ্যাডমিন** ব্যবহার করতে পারবে।"
)

PUBLIC_TEXT = (
    "✨ **JISAN V2**\n"
    "__Post Management Panel__\n"
    f"{DIV}\n"
    "🔒 এটি একটি প্রাইভেট বট। শুধু অ্যাডমিন এটি চালাতে পারে।"
)


def clean_name(name):
    name = (name or "Admin").strip()
    for ch in ("*", "_", "`", "[", "]"):
        name = name.replace(ch, "")
    return name or "Admin"


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} দিন")
    if h:
        parts.append(f"{h} ঘণ্টা")
    if m and not d:
        parts.append(f"{m} মিনিট")
    if not parts:
        parts.append(f"{s} সেকেন্ড")
    return " ".join(parts)


def interval_text():
    return f"{auto_post_interval // 3600} ঘণ্টা"


def auto_status_text():
    return "🟢 চালু" if is_auto_post_running else "🔴 বন্ধ"


def onoff(flag):
    return "🟢 ON" if flag else "🔴 OFF"


def db_status_text():
    if not store.ok:
        return "🟡 Firebase নেই (লোকাল ফাইল)"
    if store.last_error:
        return f"🔴 সমস্যা ({store.last_error})"
    return "🟢 Firebase সংযুক্ত"


def nav_row(back=None):
    row = []
    if back:
        row.append(Button.inline("⬅️ Back", back))
    row.append(Button.inline("🏠 Home", "menu_home"))
    return row


async def show(event, text, buttons=None):
    """Edit the message for button presses, reply for commands."""
    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            pass
    else:
        await event.reply(text, buttons=buttons)


# ------------------------------------------------------------------
# Screens (each returns (text, buttons))
# ------------------------------------------------------------------
def home_screen(name):
    text = (
        "✨ **JISAN V2** ✨\n"
        "__Post Management Panel__\n"
        f"{DIV}\n"
        f"👋 স্বাগতম, **{clean_name(name)}**!\n\n"
        "🟢 বট: **Online**\n"
        f"👥 অ্যাকাউন্ট: `{len(accounts)}` (🟢 সক্রিয় `{len(active_accounts())}`)\n"
        f"🎯 টার্গেট গ্রুপ: `{total_targets()}` টি\n"
        f"🔁 Auto-Post: **{auto_status_text()}**\n"
        f"⏱ ব্যবধান: `{interval_text()}`\n"
        f"{DIV}\n"
        "নিচের মেনু থেকে বেছে নাও 👇"
    )
    buttons = [
        [Button.inline("📊 Dashboard", "menu_dash"), Button.inline("🎯 Groups", "menu_groups")],
        [Button.inline("📨 Manual Post", "menu_post"), Button.inline("🔁 Auto-Post", "menu_auto")],
        [Button.inline("👥 Accounts", "menu_accounts"), Button.inline("⚙️ Settings", "menu_settings")],
        [Button.inline("❓ Help", "menu_help")],
    ]
    return text, buttons


def dashboard_screen():
    if last_post_time:
        last_txt = f"`{fmt_duration(time.time() - last_post_time)}` আগে"
    else:
        last_txt = "`কোনো পোস্ট হয়নি`"

    remaining = COOLDOWN_TIME - (time.time() - last_post_time)
    if is_auto_post_running:
        next_txt = "🔁 Auto-Post চালু আছে"
    elif remaining > 0 and last_post_time:
        next_txt = f"⏳ `{fmt_duration(remaining)}` পরে"
    else:
        next_txt = "✅ প্রস্তুত"

    text = (
        "📊 **Dashboard**\n"
        f"{DIV}\n"
        "🤖 **বট**\n"
        "• স্ট্যাটাস: 🟢 Online\n"
        f"• আপটাইম: `{fmt_duration(time.time() - START_TIME)}`\n"
        f"• ইউজার: `{len(bot_users)}`\n\n"
        "👥 **অ্যাকাউন্ট**\n"
        f"• মোট: `{len(accounts)}` · সক্রিয়: `{len(active_accounts())}`\n"
        f"• টার্গেট গ্রুপ (সক্রিয়): `{total_targets()}` টি\n\n"
        "🔁 **Auto-Post**\n"
        f"• স্ট্যাটাস: {auto_status_text()}\n"
        f"• ব্যবধান: `{interval_text()}`\n"
        f"• কন্টেন্ট: {'✅ সেভ আছে' if auto_payload else '❌ নেই'}\n"
        f"• Auto-Delete: {onoff(auto_delete_enabled)}\n\n"
        "📨 **ম্যানুয়াল পোস্ট**\n"
        f"• সর্বশেষ পোস্ট: {last_txt}\n"
        f"• পরবর্তী: {next_txt}\n\n"
        "🗄 **ডাটাবেস**\n"
        f"• {db_status_text()}"
    )
    buttons = [
        [Button.inline("🔄 Refresh", "menu_dash")],
        nav_row(),
    ]
    return text, buttons


def auto_screen():
    text = (
        "🔁 **Auto-Post**\n"
        f"{DIV}\n"
        f"• স্ট্যাটাস: **{auto_status_text()}**\n"
        f"• কন্টেন্ট: {'✅ সেভ আছে' if auto_payload else '❌ নেই'}\n"
        f"• ব্যবধান: `{interval_text()}`\n"
        f"• সক্রিয় অ্যাকাউন্ট: `{len(active_accounts())}`\n"
        f"• টার্গেট গ্রুপ: `{total_targets()}` টি\n"
        f"{DIV}\n"
        "নিচের বাটন থেকে নিয়ন্ত্রণ করো 👇"
    )
    if is_auto_post_running:
        main_btn = Button.inline("🛑 বন্ধ করুন", "auto_stop")
    else:
        main_btn = Button.inline("🚀 চালু করুন", "auto_start")
    buttons = [
        [main_btn],
        [Button.inline("📝 কন্টেন্ট সেট", "auto_set"), Button.inline("⏱ সময় বদলান", "auto_time")],
        nav_row(),
    ]
    return text, buttons


def auto_time_screen():
    text = (
        "⏱ **Auto-Post ব্যবধান**\n"
        f"{DIV}\n"
        f"এখনকার ব্যবধান: `{interval_text()}`\n\n"
        "নতুন ব্যবধান বেছে নাও 👇\n"
        "__অন্য সময় চাইলে লিখো: `/autopost time <ঘণ্টা>`__"
    )
    buttons = [
        [Button.inline("1 ঘণ্টা", "auto_t:1"), Button.inline("2 ঘণ্টা", "auto_t:2"), Button.inline("3 ঘণ্টা", "auto_t:3")],
        [Button.inline("6 ঘণ্টা", "auto_t:6"), Button.inline("12 ঘণ্টা", "auto_t:12"), Button.inline("24 ঘণ্টা", "auto_t:24")],
        nav_row("menu_auto"),
    ]
    return text, buttons


def auto_content_prompt_screen():
    text = (
        "📝 **Auto-Post কন্টেন্ট**\n"
        f"{DIV}\n"
        "অটো-পোস্টের জন্য যে মেসেজটি (Text / Photo / Video) পাঠাতে চাও, "
        "এখন সেটি সেন্ড করো 👇"
    )
    return text, [[Button.inline("❌ বাতিল", "cancel_wait")]]


def settings_screen():
    text = (
        "⚙️ **Settings**\n"
        f"{DIV}\n"
        f"🗑 Auto-Delete: **{onoff(auto_delete_enabled)}**\n"
        "__নতুন পোস্টের সময় আগের পুরোনো পোস্ট মুছে যায় (প্রতি গ্রুপে সর্বশেষ ২টি রেখে)।__\n\n"
        f"⏱ ম্যানুয়াল কুলডাউন: `{fmt_duration(COOLDOWN_TIME)}`\n"
        f"🔁 Auto-Post ব্যবধান: `{interval_text()}`"
    )
    toggle_label = "🗑 Auto-Delete বন্ধ করুন" if auto_delete_enabled else "🗑 Auto-Delete চালু করুন"
    buttons = [
        [Button.inline(toggle_label, "del_toggle")],
        [Button.inline("⏱ Auto-Post সময়", "auto_time")],
        nav_row(),
    ]
    return text, buttons


def help_screen():
    text = (
        "❓ **Help**\n"
        f"{DIV}\n"
        "**কীভাবে ব্যবহার করবে**\n"
        "1️⃣ 👥 **Accounts** থেকে অ্যাকাউন্ট যোগ করো\n"
        "2️⃣ 🎯 **Groups** থেকে প্রতি অ্যাকাউন্টের টার্গেট গ্রুপ ✅ ON করো\n"
        "3️⃣ 🔁 **Auto-Post** থেকে কন্টেন্ট সেট করে চালু করো\n"
        "4️⃣ অথবা 📨 **Manual Post** দিয়ে একবারে পোস্ট করো\n\n"
        "**কমান্ড (বাটনের বিকল্প)**\n"
        "• `/menu` — মূল মেনু\n"
        "• `/accounts` — অ্যাকাউন্ট ম্যানেজার\n"
        "• `/post` — ম্যানুয়াল ব্রডকাস্ট\n"
        "• `/setautopost` — অটো-পোস্ট কন্টেন্ট সেভ\n"
        "• `/autopost start` / `stop` — অটো-পোস্ট চালু / বন্ধ\n"
        "• `/autopost time <ঘণ্টা>` — ব্যবধান বদল\n"
        "• `/autodelete on` / `off` — অটো-ডিলিট\n"
        "• `/groups` — টার্গেট গ্রুপ\n"
        "• `/stats` — ড্যাশবোর্ড\n"
        "• `/cancel` — চলমান ইনপুট বাতিল\n\n"
        "ℹ️ ম্যানুয়াল পোস্ট শুধু তখনই চলে যখন Auto-Post বন্ধ থাকে।"
    )
    return text, [nav_row()]


def manual_post_screen(user_id):
    """Returns (text, buttons). Adds user to waiting set only when ready."""
    if is_auto_post_running:
        text = (
            "⚠️ **ম্যানুয়াল পোস্ট বন্ধ**\n"
            f"{DIV}\n"
            "এখন **Auto-Post** চালু আছে। ম্যানুয়ালি পোস্ট করতে আগে Auto-Post বন্ধ করো।"
        )
        return text, [[Button.inline("🛑 Auto-Post বন্ধ করুন", "auto_stop")], nav_row()]

    if not active_accounts():
        text = (
            "⚠️ **কোনো সক্রিয় অ্যাকাউন্ট নেই**\n"
            f"{DIV}\n"
            "আগে 👥 **Accounts** থেকে অ্যাকাউন্ট যোগ বা চালু করো।"
        )
        return text, [[Button.inline("👥 Accounts", "menu_accounts")], nav_row()]

    if total_targets() == 0:
        text = (
            "⚠️ **কোনো টার্গেট গ্রুপ নেই**\n"
            f"{DIV}\n"
            "আগে 🎯 **Groups** থেকে অন্তত একটি গ্রুপ সিলেক্ট করো।"
        )
        return text, [[Button.inline("🎯 Groups", "menu_groups")], nav_row()]

    elapsed = time.time() - last_post_time
    if elapsed < COOLDOWN_TIME:
        remaining = int(COOLDOWN_TIME - elapsed)
        text = (
            "⏳ **কুলডাউন চলছে**\n"
            f"{DIV}\n"
            f"পরের ব্রডকাস্টের জন্য অপেক্ষা করো: **{remaining // 60}m {remaining % 60}s**"
        )
        return text, [nav_row()]

    waiting_users.add(user_id)
    text = (
        "📨 **Manual Post**\n"
        f"{DIV}\n"
        f"👥 সক্রিয় অ্যাকাউন্ট: **{len(active_accounts())}**\n"
        f"🎯 টার্গেট গ্রুপ: **{total_targets()}** টি\n\n"
        "এখন তোমার মেসেজ (Text / Photo / Video) সেন্ড বা ফরওয়ার্ড করো 👇"
    )
    return text, [[Button.inline("❌ বাতিল", "cancel_wait")]]


# ---- Accounts screens -------------------------------------------------
def accounts_screen():
    lines = [
        "👥 **Accounts**",
        DIV,
        f"• মোট: `{len(accounts)}` · সক্রিয়: `{len(active_accounts())}`",
        f"• 🗄 {db_status_text()}",
    ]
    if not accounts_ready():
        lines.append("")
        lines.append("⚠️ নতুন অ্যাকাউন্ট যোগ করতে Render-এ Firebase-এর দুটো ভেরিয়েবল "
                     "ও `ENCRYPTION_KEY` সেট করতে হবে।")
    elif not accounts:
        lines.append("")
        lines.append("এখনো কোনো অ্যাকাউন্ট নেই। ➕ চেপে যোগ করো।")
    else:
        lines.append("")
        lines.append("অ্যাকাউন্টে চাপ দিয়ে বিস্তারিত দেখো 👇")

    buttons = []
    for acc in accounts.values():
        buttons.append([Button.inline(
            f"{acc.icon()} {acc.label[:22]} · {len(acc.groups)} গ্রুপ", f"acc:{acc.id}")])
    if accounts_ready():
        buttons.append([Button.inline("➕ নতুন অ্যাকাউন্ট যোগ করুন", "acc_add")])
    buttons.append(nav_row())
    return "\n".join(lines), buttons


def account_detail_screen(acc):
    if not acc.enabled:
        status = "⏸ বন্ধ করা"
    elif acc.connected:
        status = "🟢 সংযুক্ত"
    else:
        status = f"🔴 সংযোগ নেই ({acc.error or 'unknown'})"
    text = (
        f"👤 **{acc.label}**\n"
        f"{DIV}\n"
        f"• স্ট্যাটাস: {status}\n"
        f"• টার্গেট গ্রুপ: `{len(acc.groups)}` টি\n"
        f"• ধরন: {'Main (Render env)' if acc.is_main else 'Firebase-এ সংরক্ষিত'}"
    )
    toggle = "⏸ বন্ধ করুন" if acc.enabled else "▶️ চালু করুন"
    buttons = [
        [Button.inline(toggle, f"acc_toggle:{acc.id}"), Button.inline("🎯 গ্রুপ", f"gacc:{acc.id}")],
        [Button.inline("🔄 Reconnect", f"acc_re:{acc.id}")],
    ]
    if not acc.is_main:
        buttons.append([Button.inline("❌ মুছে ফেলুন", f"acc_del:{acc.id}")])
    buttons.append(nav_row("menu_accounts"))
    return text, buttons


def account_delete_screen(acc):
    text = (
        "❌ **অ্যাকাউন্ট মুছবে?**\n"
        f"{DIV}\n"
        f"**{acc.label}** বট ও Firebase থেকে মুছে যাবে।\n\n"
        "ℹ️ Telegram-এ ওই সেশনটা চালু থেকে যায়। পুরোপুরি বন্ধ করতে ওই অ্যাকাউন্টের "
        "Settings → Devices থেকে সেশনটা Terminate করো।"
    )
    buttons = [
        [Button.inline("✅ হ্যাঁ, মুছুন", f"acc_del_yes:{acc.id}"),
         Button.inline("↩️ না", f"acc:{acc.id}")],
    ]
    return text, buttons


def account_add_prompt_screen():
    text = (
        "➕ **নতুন অ্যাকাউন্ট**\n"
        f"{DIV}\n"
        "অ্যাকাউন্টের **Telethon session string** এখন পাঠাও 👇\n\n"
        "🔒 বট যাচাই করে এনক্রিপ্ট করে সেভ করবে, আর তোমার মেসেজটা সাথে সাথে মুছে দেবে।\n"
        "⚠️ শুধু **নিজের** অ্যাকাউন্টের session দাও।"
    )
    return text, [[Button.inline("❌ বাতিল", "cancel_wait")]]


# ---- Groups screens ---------------------------------------------------
def group_accounts_screen():
    text = (
        "🎯 **কোন অ্যাকাউন্টের গ্রুপ?**\n"
        f"{DIV}\n"
        "প্রতিটা অ্যাকাউন্টের গ্রুপ আলাদা। একটা বেছে নাও 👇"
    )
    buttons = []
    for acc in accounts.values():
        buttons.append([Button.inline(
            f"{acc.icon()} {acc.label[:22]} · {len(acc.groups)}", f"gacc:{acc.id}")])
    buttons.append(nav_row())
    return text, buttons


async def get_groups(acc, force=False):
    if not acc.connected:
        raise RuntimeError(acc.error or "অ্যাকাউন্ট সংযুক্ত নেই")
    cache = groups_cache.get(acc.id)
    now = time.time()
    if force or not cache or now - cache["time"] > GROUP_CACHE_TTL:
        dialogs = await acc.client.get_dialogs()
        items = [(d.id, d.title or "Unnamed") for d in dialogs if d.is_group]
        items.sort(key=lambda x: x[1].lower())
        cache = {"time": now, "items": items}
        groups_cache[acc.id] = cache
    return cache["items"]


async def groups_screen(acc_id, page=0, force=False):
    acc = accounts.get(acc_id)
    if not acc:
        return accounts_screen()

    try:
        items = await get_groups(acc, force)
    except Exception as e:
        text = (
            "🎯 **টার্গেট গ্রুপ**\n"
            f"{DIV}\n"
            f"❌ **{acc.label}** থেকে গ্রুপ লোড করা যায়নি: `{str(e)[:80]}`"
        )
        return text, [[Button.inline("🔄 Reconnect", f"acc_re:{acc.id}")], nav_row("menu_groups")]

    if not items:
        text = (
            "🎯 **টার্গেট গ্রুপ**\n"
            f"{DIV}\n"
            f"❌ **{acc.label}** অ্যাকাউন্টে কোনো গ্রুপ পাওয়া যায়নি।"
        )
        return text, [nav_row("menu_groups")]

    total_pages = max(1, (len(items) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = min(max(page, 0), total_pages - 1)
    chunk = items[page * GROUPS_PER_PAGE:(page + 1) * GROUPS_PER_PAGE]

    text = (
        f"🎯 **টার্গেট গ্রুপ** · {acc.label}\n"
        f"{DIV}\n"
        f"✅ সিলেক্টেড: `{len(acc.groups)}` / {len(items)}\n"
        f"📄 পেজ: `{page + 1}/{total_pages}`\n\n"
        "বাটনে চাপ দিয়ে চালু/বন্ধ করো:\n"
        "• ✅ = এখানে পোস্ট যাবে\n"
        "• ❌ = এখানে পোস্ট যাবে না"
    )

    buttons = []
    for gid, title in chunk:
        symbol = "✅" if gid in acc.groups else "❌"
        buttons.append([Button.inline(f"{symbol} {title[:28]}", f"tg:{acc.id}:{gid}:{page}")])

    pager = []
    if page > 0:
        pager.append(Button.inline("◀️ আগে", f"grp_page:{acc.id}:{page - 1}"))
    if page < total_pages - 1:
        pager.append(Button.inline("পরে ▶️", f"grp_page:{acc.id}:{page + 1}"))
    if pager:
        buttons.append(pager)

    buttons.append([Button.inline("💾 Save & Close", "close_groups")])
    buttons.append(nav_row("menu_groups"))
    return text, buttons


def groups_saved_screen():
    text = (
        "✅ **টার্গেট গ্রুপ সেভ হয়েছে!**\n"
        f"{DIV}\n"
        f"🎯 মোট সিলেক্টেড (সক্রিয় অ্যাকাউন্ট): `{total_targets()}` টি\n\n"
        "এখন পোস্ট করতে পারো 👇"
    )
    buttons = [
        [Button.inline("📨 Manual Post", "menu_post"), Button.inline("🔁 Auto-Post", "menu_auto")],
        nav_row(),
    ]
    return text, buttons


# ------------------------------------------------------------------
# Posting engine
# ------------------------------------------------------------------
async def prepare_payload(message, dest):
    """Turn a bot-chat message into something every account can send."""
    media_path = None
    if message.media and not isinstance(message.media, MessageMediaWebPage):
        os.makedirs(PAYLOAD_DIR, exist_ok=True)
        media_path = await message.download_media(file=dest)
    return {
        "text": message.message or "",
        "entities": message.entities,
        "media": media_path,
    }


def cleanup_payload(payload, keep=None):
    try:
        path = payload and payload.get("media")
        if path and path != keep and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


async def _send(client, group_id, payload):
    if payload["media"]:
        return await client.send_file(
            group_id, payload["media"],
            caption=payload["text"], formatting_entities=payload["entities"])
    return await client.send_message(
        group_id, payload["text"], formatting_entities=payload["entities"])


async def send_to_group(acc, group_id, payload):
    """Delete old post (if enabled) and send the new one from this account."""
    key = (acc.id, group_id)
    sent = last_sent_messages.setdefault(key, [])

    if auto_delete_enabled and len(sent) >= 2:
        old_id = sent.pop(0)
        try:
            await acc.client.delete_messages(group_id, old_id)
        except Exception as e:
            logger.warning("Delete failed (%s / %s): %s", acc.id, group_id, type(e).__name__)

    try:
        msg = await _send(acc.client, group_id, payload)
    except FloodWaitError as e:
        if e.seconds > 300:
            raise
        logger.warning("FloodWait %ss (%s / %s), waiting...", e.seconds, acc.id, group_id)
        await asyncio.sleep(e.seconds + 1)
        msg = await _send(acc.client, group_id, payload)

    sent.append(msg.id)


async def broadcast(payload):
    """Send the payload from every active account to its own groups.
    Returns a list of (account, ok, failed, note)."""
    results = []
    for acc in list(accounts.values()):
        if not acc.enabled or not acc.groups:
            continue
        if not acc.connected:
            await connect_account(acc)  # one reconnect attempt
        if not acc.connected:
            results.append((acc, 0, len(acc.groups), acc.error or "সংযোগ নেই"))
            continue

        ok = failed = 0
        for group_id in list(acc.groups):
            try:
                await send_to_group(acc, group_id, payload)
                ok += 1
                await asyncio.sleep(random.randint(2, 5))
            except Exception as e:
                failed += 1
                logger.error("Post failed (%s / %s): %s", acc.id, group_id, type(e).__name__)
                await asyncio.sleep(1)
        results.append((acc, ok, failed, ""))
    return results


async def auto_post_loop():
    global last_post_time
    while is_auto_post_running:
        await asyncio.sleep(auto_post_interval)
        if not is_auto_post_running:
            break
        if not (auto_payload and total_targets()):
            continue

        logger.info("⚡ Executing Scheduled Auto-Post...")
        try:
            await broadcast(auto_payload)
            last_post_time = time.time()
        except Exception as e:
            # The loop must never die because of one bad cycle
            logger.error("Auto-post cycle failed: %s", type(e).__name__)


async def start_autopost():
    global is_auto_post_running, auto_post_task
    if not auto_payload:
        return False, "❌ আগে কন্টেন্ট সেভ করো!"
    if is_auto_post_running:
        return False, "⚠️ Auto-Post আগে থেকেই চালু আছে!"
    is_auto_post_running = True
    auto_post_task = asyncio.create_task(auto_post_loop())
    await save_settings()
    return True, "🚀 Auto-Post চালু হয়েছে!"


async def stop_autopost():
    global is_auto_post_running, auto_post_task
    if not is_auto_post_running:
        return False, "⚠️ Auto-Post আগে থেকেই বন্ধ আছে।"
    is_auto_post_running = False
    if auto_post_task and not auto_post_task.done():
        auto_post_task.cancel()
    await save_settings()
    return True, "🛑 Auto-Post বন্ধ হয়েছে!"


# ------------------------------------------------------------------
# Telegram "Menu" button (command list)
# ------------------------------------------------------------------
if MENU_OK:
    PUBLIC_COMMANDS = [
        BotCommand(command="start", description="🏠 মূল মেনু খুলুন"),
        BotCommand(command="help", description="❓ কমান্ড ও সাহায্য দেখুন"),
    ]

    ADMIN_COMMANDS = [
        BotCommand(command="start", description="🏠 মূল মেনু খুলুন"),
        BotCommand(command="stats", description="📊 ড্যাশবোর্ড ও স্ট্যাটাস"),
        BotCommand(command="accounts", description="👥 অ্যাকাউন্ট ম্যানেজার"),
        BotCommand(command="groups", description="🎯 টার্গেট গ্রুপ বাছুন"),
        BotCommand(command="post", description="📨 ম্যানুয়াল ব্রডকাস্ট"),
        BotCommand(command="setautopost", description="📝 অটো-পোস্টের কন্টেন্ট সেট"),
        BotCommand(command="autopost", description="🔁 অটো-পোস্ট নিয়ন্ত্রণ"),
        BotCommand(command="autodelete", description="🗑 অটো-ডিলিট সেটিং"),
        BotCommand(command="cancel", description="❌ চলমান ইনপুট বাতিল"),
        BotCommand(command="help", description="❓ কমান্ড ও সাহায্য দেখুন"),
    ]
else:
    PUBLIC_COMMANDS = []
    ADMIN_COMMANDS = []


async def set_public_menu():
    if not MENU_OK:
        return
    try:
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="", commands=PUBLIC_COMMANDS))
    except Exception as e:
        logger.warning("Could not set public command menu: %s", e)


async def set_admin_menu(peer=None):
    """Full command menu, visible only in the admin's chat."""
    if not MENU_OK:
        return
    try:
        peer = peer or admin_peer()
        if peer is None:
            return
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopePeer(peer=peer), lang_code="", commands=ADMIN_COMMANDS))
        logger.info("Admin command menu set.")
    except Exception as e:
        logger.warning("Admin command menu not set yet: %s", e)


async def remember_admin(event):
    """Store the admin's access hash so the bot can reach them after redeploys."""
    global admin_hash
    try:
        peer = await event.get_input_sender()
        h = getattr(peer, "access_hash", None)
        if h and h != admin_hash:
            admin_hash = h
            await save_settings()
        return peer
    except Exception as e:
        logger.debug("remember_admin failed: %s", e)
        return None


# ------------------------------------------------------------------
# Admin guard + helpers for text commands
# ------------------------------------------------------------------
def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(event):
        if event.sender_id != ADMIN_ID:
            await event.reply(DENIED_TEXT)
            return
        await handler(event)

    return wrapper


async def sender_name(event):
    try:
        sender = await event.get_sender()
        return getattr(sender, "first_name", None) or "Admin"
    except Exception:
        return "Admin"


# ------------------------------------------------------------------
# Text commands
# ------------------------------------------------------------------
@bot.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event):
    bot_users.add(event.sender_id)
    if event.sender_id != ADMIN_ID:
        await event.reply(PUBLIC_TEXT)
        return
    await show(event, *home_screen(await sender_name(event)))
    peer = await remember_admin(event)
    await set_admin_menu(peer)


@bot.on(events.NewMessage(pattern=r"^/menu$"))
@admin_only
async def menu_handler(event):
    await show(event, *home_screen(await sender_name(event)))


@bot.on(events.NewMessage(pattern=r"^/help"))
async def help_handler(event):
    bot_users.add(event.sender_id)
    if event.sender_id != ADMIN_ID:
        await event.reply(PUBLIC_TEXT)
        return
    await show(event, *help_screen())


@bot.on(events.NewMessage(pattern=r"^/stats"))
@admin_only
async def stats_handler(event):
    await show(event, *dashboard_screen())


@bot.on(events.NewMessage(pattern=r"^/accounts$"))
@admin_only
async def accounts_handler(event):
    await show(event, *accounts_screen())


@bot.on(events.NewMessage(pattern=r"^/cancel$"))
@admin_only
async def cancel_handler(event):
    waiting_users.discard(event.sender_id)
    waiting_autopost_users.discard(event.sender_id)
    waiting_session_users.discard(event.sender_id)
    await event.reply("❌ বাতিল করা হয়েছে।", buttons=[nav_row()])


@bot.on(events.NewMessage(pattern=r"^/groups"))
@admin_only
async def groups_manager(event):
    if not accounts:
        await show(event, *accounts_screen())
        return
    if len(accounts) == 1:
        acc_id = next(iter(accounts))
        msg = await event.reply("⏳ **গ্রুপ লোড হচ্ছে...**")
        text, buttons = await groups_screen(acc_id, 0, force=True)
        await msg.edit(text, buttons=buttons)
    else:
        await show(event, *group_accounts_screen())


@bot.on(events.NewMessage(pattern=r"^/autodelete\s+(on|off)$"))
@admin_only
async def autodelete_toggle(event):
    global auto_delete_enabled
    auto_delete_enabled = event.pattern_match.group(1).lower() == "on"
    await save_settings()
    await show(event, *settings_screen())


@bot.on(events.NewMessage(pattern=r"^/setautopost$"))
@admin_only
async def set_autopost_prompt(event):
    waiting_users.discard(event.sender_id)
    waiting_session_users.discard(event.sender_id)
    waiting_autopost_users.add(event.sender_id)
    await show(event, *auto_content_prompt_screen())


@bot.on(events.NewMessage(pattern=r"^/autopost(?:\s+(start|stop|time(?:\s+\d+)?))?$"))
@admin_only
async def autopost_control(event):
    global auto_post_interval
    args = event.text.split()
    action = args[1].lower() if len(args) > 1 else ""

    if action == "start":
        ok, msg = await start_autopost()
        await event.reply(msg)
        await show(event, *auto_screen())
    elif action == "stop":
        ok, msg = await stop_autopost()
        await event.reply(msg)
        await show(event, *auto_screen())
    elif action == "time":
        if len(args) > 2 and args[2].isdigit():
            hours = int(args[2])
            if hours < 1 or hours > 168:
                await event.reply("❌ সময় ১ থেকে ১৬৮ ঘণ্টার মধ্যে হতে হবে!")
                return
            auto_post_interval = hours * 3600
            await save_settings()
            await event.reply(f"⏱ ব্যবধান আপডেট হয়েছে: প্রতি `{hours}` ঘণ্টা।")
            await show(event, *auto_screen())
        else:
            await event.reply("⚠️ নিয়ম: `/autopost time <ঘণ্টা>` (যেমন: `/autopost time 2`)")
    else:
        await show(event, *auto_screen())


@bot.on(events.NewMessage(pattern=r"^/post$"))
@admin_only
async def post_handler(event):
    bot_users.add(event.sender_id)
    waiting_autopost_users.discard(event.sender_id)
    waiting_session_users.discard(event.sender_id)
    await show(event, *manual_post_screen(event.sender_id))


# ------------------------------------------------------------------
# Button router
# ------------------------------------------------------------------
@bot.on(events.CallbackQuery)
async def callback_router(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("🔒 শুধু অ্যাডমিনের জন্য!", alert=True)
        return

    global auto_post_interval, auto_delete_enabled
    data = event.data.decode("utf-8", "ignore")
    parts = data.split(":")
    cmd = parts[0]
    uid = event.sender_id
    toast = ""
    alert = False
    answered = False

    def clear_waiting():
        waiting_users.discard(uid)
        waiting_autopost_users.discard(uid)
        waiting_session_users.discard(uid)

    try:
        if cmd == "menu_home":
            clear_waiting()
            await show(event, *home_screen(await sender_name(event)))

        elif cmd == "menu_dash":
            await show(event, *dashboard_screen())

        elif cmd == "menu_help":
            await show(event, *help_screen())

        elif cmd == "menu_settings":
            await show(event, *settings_screen())

        elif cmd == "menu_accounts":
            clear_waiting()
            await show(event, *accounts_screen())

        elif cmd == "acc":
            acc = accounts.get(parts[1])
            if acc:
                await show(event, *account_detail_screen(acc))
            else:
                await show(event, *accounts_screen())

        elif cmd == "acc_add":
            if not accounts_ready():
                toast, alert = "⚠️ Firebase ও ENCRYPTION_KEY সেট করা নেই", True
            else:
                clear_waiting()
                waiting_session_users.add(uid)
                await show(event, *account_add_prompt_screen())

        elif cmd == "acc_toggle":
            acc = accounts.get(parts[1])
            if acc:
                acc.enabled = not acc.enabled
                await persist_account(acc)
                toast = "▶️ চালু করা হয়েছে" if acc.enabled else "⏸ বন্ধ করা হয়েছে"
                await show(event, *account_detail_screen(acc))

        elif cmd == "acc_re":
            acc = accounts.get(parts[1])
            if acc:
                await event.answer("🔄 সংযোগ করা হচ্ছে...")
                answered = True
                await safe_disconnect(acc)
                acc.client = None
                ok = await connect_account(acc)
                await show(event, *account_detail_screen(acc))

        elif cmd == "acc_del":
            acc = accounts.get(parts[1])
            if acc and not acc.is_main:
                await show(event, *account_delete_screen(acc))

        elif cmd == "acc_del_yes":
            acc = accounts.get(parts[1])
            if acc and not acc.is_main:
                ok = await remove_account(acc)
                toast = "✅ মুছে ফেলা হয়েছে" if ok else "⚠️ Firebase থেকে মুছতে সমস্যা হয়েছে"
                alert = not ok
            await show(event, *accounts_screen())

        elif cmd == "menu_groups":
            if not accounts:
                await show(event, *accounts_screen())
            elif len(accounts) == 1:
                await event.answer("⏳ লোড হচ্ছে...")
                answered = True
                await show(event, *await groups_screen(next(iter(accounts)), 0, force=True))
            else:
                await show(event, *group_accounts_screen())

        elif cmd == "gacc":
            await event.answer("⏳ লোড হচ্ছে...")
            answered = True
            await show(event, *await groups_screen(parts[1], 0, force=True))

        elif cmd == "grp_page":
            await show(event, *await groups_screen(parts[1], int(parts[2])))

        elif cmd == "tg":
            acc = accounts.get(parts[1])
            gid = int(parts[2])
            if acc:
                if gid in acc.groups:
                    acc.groups.discard(gid)
                    toast = "❌ বন্ধ করা হয়েছে"
                else:
                    acc.groups.add(gid)
                    toast = "✅ চালু করা হয়েছে"
                await persist_account(acc)
                await show(event, *await groups_screen(acc.id, int(parts[3])))

        elif cmd == "close_groups":
            await show(event, *groups_saved_screen())

        elif cmd == "menu_post":
            waiting_autopost_users.discard(uid)
            waiting_session_users.discard(uid)
            await show(event, *manual_post_screen(uid))

        elif cmd == "menu_auto":
            await show(event, *auto_screen())

        elif cmd == "auto_start":
            ok, toast = await start_autopost()
            alert = not ok
            await show(event, *auto_screen())

        elif cmd == "auto_stop":
            ok, toast = await stop_autopost()
            alert = not ok
            await show(event, *auto_screen())

        elif cmd == "auto_set":
            clear_waiting()
            waiting_autopost_users.add(uid)
            await show(event, *auto_content_prompt_screen())

        elif cmd == "auto_time":
            await show(event, *auto_time_screen())

        elif cmd == "auto_t":
            hours = int(parts[1])
            if 1 <= hours <= 168:
                auto_post_interval = hours * 3600
                await save_settings()
                toast = f"⏱ ব্যবধান: {hours} ঘণ্টা"
            await show(event, *auto_screen())

        elif cmd == "del_toggle":
            auto_delete_enabled = not auto_delete_enabled
            await save_settings()
            toast = f"Auto-Delete {'ON' if auto_delete_enabled else 'OFF'}"
            await show(event, *settings_screen())

        elif cmd == "cancel_wait":
            clear_waiting()
            toast = "বাতিল করা হয়েছে"
            await show(event, *home_screen(await sender_name(event)))

    except Exception as e:
        logger.error("Callback error (%s): %s", cmd, type(e).__name__)
        toast = "❌ সমস্যা হয়েছে, আবার চেষ্টা করো"
        alert = True

    if not answered:
        try:
            await event.answer(toast, alert=alert)
        except Exception:
            pass


# ------------------------------------------------------------------
# Incoming content (session / auto-post content / manual post)
# ------------------------------------------------------------------
@bot.on(events.NewMessage)
async def content_handler(event):
    global last_post_time, auto_payload
    user_id = event.sender_id

    if user_id != ADMIN_ID:
        return

    # 0) New account session string
    if user_id in waiting_session_users:
        if event.text and event.text.startswith("/"):
            return
        waiting_session_users.discard(user_id)
        session_text = (event.text or "").strip()
        try:
            await event.delete()  # never leave the session string in the chat
        except Exception:
            pass
        status = await bot.send_message(event.chat_id, "⏳ **যাচাই করা হচ্ছে...**")
        ok, msg, acc = await add_account(session_text)
        if ok:
            await status.edit(
                "✅ **অ্যাকাউন্ট যোগ হয়েছে!**\n"
                f"{DIV}\n"
                f"👤 **{acc.label}**\n\n"
                "এখন এর টার্গেট গ্রুপ বেছে নাও 👇",
                buttons=[[Button.inline("🎯 গ্রুপ বাছুন", f"gacc:{acc.id}")],
                         nav_row("menu_accounts")],
            )
        else:
            await status.edit(
                f"❌ **যোগ করা যায়নি**\n{DIV}\n{msg}",
                buttons=[[Button.inline("🔁 আবার চেষ্টা", "acc_add")],
                         nav_row("menu_accounts")],
            )
        return

    # 1) Save auto-post content
    if user_id in waiting_autopost_users:
        if event.text and event.text.startswith("/"):
            return
        waiting_autopost_users.discard(user_id)
        old = auto_payload
        try:
            auto_payload = await prepare_payload(
                event.message, os.path.join(PAYLOAD_DIR, f"autopost_{int(time.time())}"))
        except Exception as e:
            logger.error("Could not prepare auto-post content: %s", type(e).__name__)
            await event.reply("❌ কন্টেন্ট সেভ করা যায়নি, আবার চেষ্টা করো।", buttons=[nav_row()])
            return
        cleanup_payload(old, keep=auto_payload.get("media"))
        await kv_set("autopost", AUTOPOST_FILE, {"msg_id": event.message.id})
        text = (
            "✅ **Auto-Post কন্টেন্ট সেভ হয়েছে!**\n"
            f"{DIV}\n"
            "এখন চালু করতে পারো 👇"
        )
        buttons = [[Button.inline("🚀 চালু করুন", "auto_start")], nav_row("menu_auto")]
        await event.reply(text, buttons=buttons)
        return

    # 2) Manual broadcast
    if user_id in waiting_users:
        if event.text and event.text.startswith("/"):
            return
        waiting_users.discard(user_id)

        processing = await event.reply(
            "⏳ **প্রসেসিং...**\n"
            f"{DIV}\n"
            f"👥 `{len(active_accounts())}` অ্যাকাউন্ট থেকে `{total_targets()}` টি গ্রুপে পোস্ট পাঠানো হচ্ছে।"
        )

        payload = None
        try:
            payload = await prepare_payload(
                event.message, os.path.join(PAYLOAD_DIR, f"manual_{int(time.time())}"))
            results = await broadcast(payload)
        except Exception as e:
            logger.error("Manual broadcast failed: %s", type(e).__name__)
            await processing.edit("❌ **ব্রডকাস্ট ব্যর্থ হয়েছে।** আবার চেষ্টা করো।", buttons=[nav_row()])
            return
        finally:
            cleanup_payload(payload, keep=auto_payload.get("media") if auto_payload else None)

        last_post_time = time.time()
        total_ok = sum(r[1] for r in results)
        total_fail = sum(r[2] for r in results)

        lines = [
            "✅ **ব্রডকাস্ট সম্পন্ন**",
            DIV,
            f"• সফল: `{total_ok}` টি গ্রুপ ✅",
            f"• ব্যর্থ: `{total_fail}` টি গ্রুপ ❌",
            "",
            "👥 **অ্যাকাউন্ট অনুযায়ী**",
        ]
        for acc, ok_n, fail_n, note in results:
            suffix = f" — {note}" if note else ""
            lines.append(f"• {acc.label[:20]}: ✅ {ok_n} · ❌ {fail_n}{suffix}")
        lines.append("")
        lines.append("⏱ পরবর্তী ম্যানুয়াল পোস্ট ১ ঘণ্টা পর।")

        await processing.edit("\n".join(lines), buttons=[nav_row()])


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
async def main():
    logger.info("Starting JisanV2 Engine (multi-account)...")
    store.init()
    if store.ok and fernet is None:
        logger.error("ENCRYPTION_KEY missing or shorter than 16 characters: "
                     "adding/loading Firebase accounts is disabled.")
    await start_dummy_server()
    await bot.start(bot_token=BOT_TOKEN)
    await set_public_menu()
    await load_state()
    await set_admin_menu()
    logger.info("🚀 JisanV2 Engine is Online!")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
