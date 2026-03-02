# ref.py
import asyncio
import os
import random
import sqlite3
import time
from typing import Optional, Tuple, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ----------------- Windows Python 3.12+/3.14 event loop fix -----------------
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# ================== CONFIG ==================
# IMPORTANT: do NOT hardcode token in code when hosting
TOKEN = "7954330145:AAHjGUNYuxN52zv6O8JvQ_1c0PR6MGd5ulw"
BOT_USERNAME = "@JonibekIELTS_bot"

# initial seed admins (will be copied into DB on first run)
ADMIN_IDS = {908588571}

DEFAULT_REQUIRED_CHATS = ["@jonibeksielts9"]
DEFAULT_NEED_REFERRALS = 3

# reward invite constraints
INVITE_MEMBER_LIMIT = 1
INVITE_EXPIRE_SECONDS = 3600

DB_PATH = "referrals.db"
# ===========================================

# ---------------- DB ----------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
cur = conn.cursor()
cur.execute("PRAGMA journal_mode=WAL;")
cur.execute("PRAGMA synchronous=NORMAL;")
conn.commit()

cur.execute("""
CREATE TABLE IF NOT EXISTS admins (
  user_id INTEGER PRIMARY KEY
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS referral_events (
  invitee_id INTEGER PRIMARY KEY,
  inviter_id INTEGER NOT NULL,
  counted INTEGER NOT NULL DEFAULT 0
)
""")
conn.commit()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  invited_by INTEGER,
  referrals_count INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS rotation_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_reward_id INTEGER
)
""")
cur.execute("INSERT OR IGNORE INTO rotation_state (id, last_reward_id) VALUES (1, NULL)")

cur.execute("""
CREATE TABLE IF NOT EXISTS rewards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  link TEXT NOT NULL UNIQUE
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS issued_invites (
  user_id INTEGER PRIMARY KEY,
  invite_link TEXT NOT NULL,
  expire_ts INTEGER NOT NULL
)
""")

conn.commit()

# ---------------- settings helpers ----------------
def set_setting(key: str, value: str) -> None:
    cur.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()

def record_referral(invitee_id: int, inviter_id: int) -> None:
    if invitee_id == inviter_id:
        return
    cur.execute(
        "INSERT OR IGNORE INTO referral_events(invitee_id, inviter_id, counted) VALUES (?, ?, 0)",
        (invitee_id, inviter_id),
    )
    conn.commit()

def try_count_referral(invitee_id: int) -> bool:
    cur.execute("SELECT inviter_id, counted FROM referral_events WHERE invitee_id=?", (invitee_id,))
    row = cur.fetchone()
    if not row:
        return False
    inviter_id, counted = row
    if counted:
        return False

    ensure_user(inviter_id)
    cur.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id=?", (inviter_id,))
    cur.execute("UPDATE referral_events SET counted=1 WHERE invitee_id=?", (invitee_id,))
    conn.commit()
    return True

def get_setting(key: str, default: str) -> str:
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default

def get_required_chats() -> List[str]:
    raw = get_setting("required_chats", ",".join(DEFAULT_REQUIRED_CHATS))
    return [c.strip() for c in raw.split(",") if c.strip()]

def set_required_chats(chats: List[str]) -> None:
    set_setting("required_chats", ",".join(chats))

def get_need_referrals() -> int:
    return int(get_setting("need_referrals", str(DEFAULT_NEED_REFERRALS)))

def set_need_referrals(n: int) -> None:
    set_setting("need_referrals", str(n))

def get_reward_chat_id() -> Optional[int]:
    raw = get_setting("reward_chat_id", "").strip()
    return int(raw) if raw else None

def set_reward_chat_id(cid: int) -> None:
    set_setting("reward_chat_id", str(cid))

def get_reward_mode() -> str:
    mode = get_setting("reward_mode", "random").strip().lower()
    return mode if mode in {"random", "rotate", "all"} else "random"

def set_reward_mode(mode: str) -> None:
    m = mode.strip().lower()
    if m not in {"random", "rotate", "all"}:
        raise ValueError("reward_mode must be random|rotate|all")
    set_setting("reward_mode", m)

# ---------------- admin helpers ----------------
def seed_admins_from_config() -> None:
    for uid in ADMIN_IDS:
        cur.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (uid,))
    conn.commit()

def is_admin(uid: int) -> bool:
    cur.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,))
    return cur.fetchone() is not None

def add_admin(uid: int) -> bool:
    cur.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (uid,))
    changed = cur.rowcount > 0
    conn.commit()
    return changed

def remove_admin(uid: int) -> bool:
    cur.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    changed = cur.rowcount > 0
    conn.commit()
    return changed

def list_admins() -> List[int]:
    cur.execute("SELECT user_id FROM admins ORDER BY user_id ASC")
    return [r[0] for r in cur.fetchall()]

# ---------------- user helpers ----------------
def ensure_user(uid: int) -> None:
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    conn.commit()

def get_user(uid: int) -> Tuple[Optional[int], int]:
    cur.execute("SELECT invited_by, referrals_count FROM users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    if not row:
        return (None, 0)
    return (row[0], row[1])

def set_invited_by(uid: int, inviter: int) -> bool:
    invited_by, _ = get_user(uid)
    if invited_by is not None:
        return False
    if uid == inviter:
        return False
    cur.execute("UPDATE users SET invited_by=? WHERE user_id=?", (inviter, uid))
    conn.commit()
    return True

def inc_referrals(inviter: int) -> None:
    ensure_user(inviter)
    cur.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id=?", (inviter,))
    conn.commit()

def referral_link(uid: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={uid}"

# ---------------- optional rewards list (kept) ----------------
def add_reward(link: str) -> None:
    cur.execute("INSERT OR IGNORE INTO rewards(link) VALUES (?)", (link.strip(),))
    conn.commit()

def delete_reward_by_id(rid: int) -> bool:
    cur.execute("DELETE FROM rewards WHERE id=?", (rid,))
    ok = cur.rowcount > 0
    conn.commit()
    return ok

def list_rewards() -> List[Tuple[int, str]]:
    cur.execute("SELECT id, link FROM rewards ORDER BY id ASC")
    return cur.fetchall()

def get_next_rotating_reward() -> Optional[str]:
    rewards = list_rewards()
    if not rewards:
        return None
    cur.execute("SELECT last_reward_id FROM rotation_state WHERE id=1")
    last = cur.fetchone()[0]
    ids = [rid for rid, _ in rewards]
    if last not in ids:
        next_id = rewards[0][0]
    else:
        next_id = rewards[(ids.index(last) + 1) % len(rewards)][0]
    cur.execute("UPDATE rotation_state SET last_reward_id=? WHERE id=1", (next_id,))
    conn.commit()
    for rid, link in rewards:
        if rid == next_id:
            return link
    return rewards[0][1]

def choose_reward_links() -> List[str]:
    mode = get_reward_mode()
    links = [link for _, link in list_rewards()]
    if not links:
        return []
    if mode == "all":
        return links
    if mode == "random":
        return [random.choice(links)]
    nxt = get_next_rotating_reward()
    return [nxt] if nxt else []

# ---------------- membership check ----------------
async def joined_all(context: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    for raw_c in get_required_chats():
        ch_id = raw_c.split("|")[0] # Only use the ID/Username to check membership
        try:
            member = await context.bot.get_chat_member(chat_id=ch_id, user_id=uid)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True

# ---------------- invite cache ----------------
def get_cached_invite(uid: int) -> Optional[Tuple[str, int]]:
    cur.execute("SELECT invite_link, expire_ts FROM issued_invites WHERE user_id=?", (uid,))
    row = cur.fetchone()
    return (row[0], row[1]) if row else None

def save_cached_invite(uid: int, link: str, expire_ts: int) -> None:
    cur.execute("""
        INSERT INTO issued_invites(user_id, invite_link, expire_ts)
        VALUES(?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET invite_link=excluded.invite_link, expire_ts=excluded.expire_ts
    """, (uid, link, expire_ts))
    conn.commit()

async def get_or_create_personal_invite(context: ContextTypes.DEFAULT_TYPE, uid: int) -> str:
    now = int(time.time())
    cached = get_cached_invite(uid)
    if cached:
        link, expire_ts = cached
        if expire_ts - now > 30:
            return link

    reward_chat_id = get_reward_chat_id()
    if not reward_chat_id:
        raise RuntimeError("Reward chat not set. Admin must run /setrewardchat -100xxxxxxxxxx")

    expire_ts = now + INVITE_EXPIRE_SECONDS
    invite = await context.bot.create_chat_invite_link(
        chat_id=reward_chat_id,
        expire_date=expire_ts,
        member_limit=INVITE_MEMBER_LIMIT,
        name=f"reward-{uid}"
    )
    save_cached_invite(uid, invite.invite_link, expire_ts)
    return invite.invite_link

# ================== UI (buttons) ==================
def build_join_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for raw_c in get_required_chats():
        parts = raw_c.split("|")
        ch_id = parts[0]
        # If an invite link is provided, use it. Otherwise, fallback to standard link.
        url = parts[1] if len(parts) > 1 else (f"https://t.me/{ch_id[1:]}" if ch_id.startswith("@") else "")
        
        display_name = ch_id if ch_id.startswith("@") else "Channel"
        if url:
            rows.append([InlineKeyboardButton(f"✅ Join {display_name}", url=url)])
            
    rows.append([InlineKeyboardButton("✅ I joined, check", callback_data="check_status")])
    return InlineKeyboardMarkup(rows)

def build_referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 My invite link", callback_data="my_link")],
        [InlineKeyboardButton("✅ Check status", callback_data="check_status")],
    ])

def build_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Required channels", callback_data="admin_reqs")],
        [InlineKeyboardButton("🎁 Reward chat", callback_data="admin_rewardchat")],
        [InlineKeyboardButton("👥 Admins", callback_data="admin_admins")],
    ])

async def render_status_text(context: ContextTypes.DEFAULT_TYPE, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    _, referrals = get_user(uid)
    need = get_need_referrals()
    joined = await joined_all(context, uid)

    reqs = get_required_chats()
    # Clean up the text so the user only sees the channel name, not the link data
    display_reqs = [r.split("|")[0] for r in reqs]
    req_text = "\n".join(f"• {c}" for c in display_reqs) if display_reqs else "• (none)"

    # STEP 1: force joining first
    if not joined:
        text = (
            "🔒 Access locked\n\n"
            "Step 1: Join the required channel(s) below.\n"
            "Then press ✅ I joined, check.\n\n"
            "Required channels:\n"
            f"{req_text}"
        )
        return text, build_join_keyboard()

    # joined => count referral for inviter once (if this user came via link)
    try_count_referral(uid)

    # refresh referrals after possible counting
    _, referrals = get_user(uid)
    remaining = max(0, need - referrals)

    # STEP 2: referrals
    text = (
        "✅ Subscription confirmed\n\n"
        f"Step 2: Invite {need} friends using your link.\n\n"
        f"👥 Referrals: {referrals}/{need}\n"
        f"🔗 Your invite link:\n{referral_link(uid)}\n\n"
        "Rules:\n"
        "• Your friend must press Start from your link\n"
        "• Your friend must also join the required channel(s)\n"
    )

    if referrals >= need:
        try:
            personal_invite = await get_or_create_personal_invite(context, uid)
            text += (
                "\n🎁 Unlocked!\n"
                "Your personal access link (1 use, limited time):\n"
                f"{personal_invite}"
            )
        except Exception:
            text += "\n🎁 Unlocked, but bot can't create invite links. Make bot admin in reward chat and allow invite links."
    else:
        text += f"\n⏳ You still need {remaining} more referral(s)."

    return text, build_referral_keyboard()


# ================== COMMANDS ==================
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)

    inviter = None
    if context.args:
        try:
            inviter = int(context.args[0])
        except ValueError:
            inviter = None

    if inviter:
        ensure_user(inviter)
        record_referral(uid, inviter)


    text, kb = await render_status_text(context, uid)
    await update.message.reply_text(
        text, 
        reply_markup=kb, 
        disable_web_page_preview=True
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(uid)
    text, kb = await render_status_text(context, uid)
    await update.message.reply_text(
        text,
        reply_markup=kb,
        disable_web_page_preview=True
    )

# -------- buttons callbacks --------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    ensure_user(uid)

    if q.data == "check_status":
        text, kb = await render_status_text(context, uid)
        await q.edit_message_text(text, reply_markup=kb, disable_web_page_preview=True)
        return

    if q.data == "my_link":
        await q.message.reply_text(
            f"🔗 Your invite link:\n{referral_link(uid)}",
            disable_web_page_preview=True
        )
        return

    # admin panel buttons
    if q.data == "admin_reqs":
        if not is_admin(uid):
            return
        reqs = get_required_chats()
        await q.message.reply_text("Required channels:\n" + ("\n".join(reqs) if reqs else "(none)"))
        return

    if q.data == "admin_rewardchat":
        if not is_admin(uid):
            return
        rc = get_reward_chat_id()
        await q.message.reply_text(
            "Reward chat ID:\n"
            f"{rc if rc else '(not set)'}\n\n"
            "Set with:\n/setrewardchat -100xxxxxxxxxx"
        )
        return

    if q.data == "admin_admins":
        if not is_admin(uid):
            return
        admins = list_admins()
        await q.message.reply_text("Admins:\n" + ("\n".join(map(str, admins)) if admins else "(none)"))
        return

# -------- ADMIN COMMANDS --------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = (
        "Admin panel:\n"
        "• Use buttons below, or commands.\n\n"
        "Commands:\n"
        "/setneed <number>\n"
        "/setrewardmode random|rotate|all\n"
        "/addreq @channel [link]\n"
        "/delreq @channel\n"
        "/reqs\n"
        "/setrewardchat -100xxxxxxxxxx\n"
        "/addadmin <user_id>\n"
        "/deladmin <user_id>\n"
        "/admins\n"
        "/stats\n"
    )
    await update.message.reply_text(msg, reply_markup=build_admin_keyboard())

async def setneed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setneed 3")
        return
    try:
        n = int(context.args[0])
        if n < 1 or n > 100:
            raise ValueError
        set_need_referrals(n)
        await update.message.reply_text(f"✅ need_referrals set to {n}")
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number (1–100). Example: /setneed 3")

async def setrewardmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setrewardmode random|rotate|all")
        return
    try:
        set_reward_mode(context.args[0])
        await update.message.reply_text(f"✅ reward_mode set to {get_reward_mode()}")
    except ValueError:
        await update.message.reply_text("❌ Use: random | rotate | all")

async def setrewardchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setrewardchat -100xxxxxxxxxx")
        return
    try:
        cid = int(context.args[0])
        if not str(cid).startswith("-100"):
            raise ValueError
        set_reward_chat_id(cid)
        await update.message.reply_text(f"✅ Reward chat set to {cid}")
    except ValueError:
        await update.message.reply_text("❌ Must be numeric ID starting with -100")

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    try:
        uid = int(context.args[0])
        ok = add_admin(uid)
        await update.message.reply_text("✅ Admin added." if ok else "ℹ️ Already an admin.")
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")

async def deladmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /deladmin <user_id>")
        return
    try:
        uid = int(context.args[0])
        admins = list_admins()
        if uid in admins and len(admins) == 1:
            await update.message.reply_text("❌ Can't remove the last admin.")
            return
        ok = remove_admin(uid)
        await update.message.reply_text("✅ Admin removed." if ok else "❌ Not an admin.")
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")

async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    admins = list_admins()
    await update.message.reply_text("Admins:\n" + ("\n".join(map(str, admins)) if admins else "(none)"))

async def addreq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /addreq @channelusername [invite_link]\n"
            "Example: /addreq @jonibeksielts9 https://t.me/+AbCdEfGh"
        )
        return
    
    ch = context.args[0].strip()
    link = context.args[1].strip() if len(context.args) > 1 else ""

    if not ch.startswith("@") and not ch.startswith("-100"):
        await update.message.reply_text("❌ Channel must start with @ or -100")
        return

    entry = f"{ch}|{link}" if link else ch
    reqs = get_required_chats()
    
    # Remove old entry if updating an existing channel
    reqs = [r for r in reqs if r.split("|")[0] != ch]
    reqs.append(entry)
    
    set_required_chats(reqs)
    await update.message.reply_text("✅ Required channels updated.\n" + "\n".join(reqs))

async def delreq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /delreq @channelusername")
        return
    
    ch = context.args[0].strip()
    # Target just the ID part when deleting
    reqs = [r for r in get_required_chats() if r.split("|")[0] != ch]
    set_required_chats(reqs)
    await update.message.reply_text("✅ Required channels updated.\n" + ("\n".join(reqs) if reqs else "(none)"))

async def reqs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reqs = get_required_chats()
    await update.message.reply_text("Required channels:\n" + ("\n".join(reqs) if reqs else "(none)"))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT SUM(referrals_count) FROM users")
    total_refs = cur.fetchone()[0] or 0
    await update.message.reply_text(f"📊 Stats\nUsers: {total_users}\nTotal referrals counted: {total_refs}")

# ================== MAIN ==================
def main():
    if not TOKEN:
        raise RuntimeError("TOKEN is missing. Set environment variable TOKEN with your NEW bot token.")
    if not BOT_USERNAME:
        raise RuntimeError("BOT_USERNAME is missing (without @).")

    seed_admins_from_config()

    app = Application.builder().token(TOKEN).build()

    # user commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("myid", myid))

    # admin commands
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("setneed", setneed))
    app.add_handler(CommandHandler("setrewardmode", setrewardmode_cmd))
    app.add_handler(CommandHandler("setrewardchat", setrewardchat_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("deladmin", deladmin_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("addreq", addreq))
    app.add_handler(CommandHandler("delreq", delreq))
    app.add_handler(CommandHandler("reqs", reqs_cmd))
    app.add_handler(CommandHandler("stats", stats))

    # buttons
    app.add_handler(CallbackQueryHandler(on_button))

    print("Bot running... Ctrl+C to stop")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
