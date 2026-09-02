import os
import re
import json
import time
import html
import sqlite3
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from collections import Counter

import requests
import telebot
from telebot import types
from flask import Flask, request, redirect, render_template_string, jsonify

# ============================================================
# CẤU HÌNH QUA BIẾN MÔI TRƯỜNG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().lstrip("-").isdigit()}
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")
BOT_NAME = os.getenv("BOT_NAME", "MD5 Tài Xỉu Pro")

# Không đặt thông tin ngân hàng thật trong mã nguồn. Có thể sửa tại /admin.
DEFAULT_BANK = {
    "bank_code": os.getenv("BANK_CODE", "MBBank"),
    "account_no": os.getenv("BANK_ACCOUNT_NO", "0000000000"),
    "account_name": os.getenv("BANK_ACCOUNT_NAME", "CHU TAI KHOAN"),
    "note_prefix": os.getenv("BANK_NOTE_PREFIX", "NAPTX"),
    "contact_link": os.getenv("CONTACT_LINK", "https://t.me/"),
}
DEFAULT_PACKAGES = {
    "Gói 1 ngày": {"price": 10000, "days": 1},
    "Gói 7 ngày": {"price": 50000, "days": 7},
    "Gói 30 ngày": {"price": 150000, "days": 30},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("md5tx")

# Token có thể nhập sau tại /admin. Placeholder chỉ để TeleBot đăng ký handler.
# Khi chưa cấu hình, Render vẫn giữ web admin hoạt động thay vì crash.
_RUNTIME_PLACEHOLDER = "000000:CONFIGURE_IN_ADMIN"
bot = telebot.TeleBot(BOT_TOKEN or _RUNTIME_PLACEHOLDER, parse_mode="HTML", threaded=True)
app = Flask(__name__)
_polling_started = False
_polling_lock = threading.Lock()

# ============================================================
# DATABASE: SQLite để đáp ứng yêu cầu chỉ 2 file
# ============================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            created_at TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY, package_name TEXT NOT NULL, days INTEGER NOT NULL,
            used_by INTEGER, created_at TEXT NOT NULL, used_at TEXT, expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, decided_at TEXT, decided_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, sent_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS giftcodes (
            code TEXT PRIMARY KEY, amount INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS giftcode_uses (
            code TEXT NOT NULL, telegram_id INTEGER NOT NULL, used_at TEXT NOT NULL,
            PRIMARY KEY(code, telegram_id)
        );
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "balance" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0")
        if c.execute("SELECT 1 FROM settings WHERE key='bank'").fetchone() is None:
            c.execute("INSERT INTO settings(key,value) VALUES('bank',?)", (json.dumps(DEFAULT_BANK),))
        if c.execute("SELECT 1 FROM settings WHERE key='packages'").fetchone() is None:
            c.execute("INSERT INTO settings(key,value) VALUES('packages',?)", (json.dumps(DEFAULT_PACKAGES),))
        if c.execute("SELECT 1 FROM settings WHERE key='bot_token'").fetchone() is None:
            c.execute("INSERT INTO settings(key,value) VALUES('bot_token',?)", (json.dumps(BOT_TOKEN),))
        if c.execute("SELECT 1 FROM settings WHERE key='admin_ids'").fetchone() is None:
            c.execute("INSERT INTO settings(key,value) VALUES('admin_ids',?)", (json.dumps(sorted(x for x in ADMIN_IDS if x != 0)),))


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def get_setting(key, default):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))


def is_admin(uid):
    return uid in ADMIN_IDS and uid != 0


def start_bot_if_configured():
    """Nạp cấu hình từ DB và bật polling một lần; không có token thì chỉ chạy web admin."""
    global _polling_started
    token = str(get_setting("bot_token", BOT_TOKEN) or "").strip()
    configured_ids = get_setting("admin_ids", sorted(x for x in ADMIN_IDS if x != 0))
    ADMIN_IDS.clear()
    ADMIN_IDS.update(int(x) for x in configured_ids if str(x).strip().lstrip("-").isdigit() and int(x) != 0)
    if not token:
        return False
    with _polling_lock:
        if _polling_started:
            return True
        bot.token = token
        _polling_started = True
        threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30), daemon=True, name="telegram-polling").start()
    log.info("Telegram polling đã khởi động; admin IDs=%s", sorted(ADMIN_IDS))
    return True


def register_user(message):
    u = message.from_user
    t = iso(now())
    with db() as c:
        c.execute("""INSERT INTO users(telegram_id,username,first_name,created_at,last_seen)
                     VALUES(?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET
                     username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen""",
                  (u.id, u.username or "", u.first_name or "", t, t))


def fmt_money(n):
    return f"{int(n):,}".replace(",", ".") + "đ"


def user_key(uid):
    with db() as c:
        return c.execute("""SELECT * FROM keys WHERE used_by=? AND expires_at IS NOT NULL
                          AND expires_at>? ORDER BY expires_at DESC LIMIT 1""", (uid, iso(now()))).fetchone()

# ============================================================
# BỘ PHÂN TÍCH HASH DETERMINISTIC
# Trích ý tưởng từ file mẫu: nibble, entropy, bit-run, Markov 2 bước,
# phổ tần số và cellular rule 30. Không random, không math.random.
# Lưu ý: hash ngẫu nhiên không chứa thông tin bảo đảm kết quả trò chơi.
# ============================================================
class HashAnalyzer:
    @staticmethod
    def _bits(data):
        return [(b >> (7 - i)) & 1 for b in data for i in range(8)]

    @staticmethod
    def _entropy(values):
        if not values:
            return 0.0
        cnt = Counter(values)
        n = len(values)
        return -sum((v / n) * __import__('math').log2(v / n) for v in cnt.values())

    @staticmethod
    def _spectral_score(bits):
        # DFT thủ công, tránh thêm dependency và giữ tính tái lập.
        import math
        n = len(bits)
        if n < 16:
            return 0.0
        score = 0.0
        for k in range(1, min(n // 2, 32)):
            re_part = sum(bits[t] * math.cos(2 * math.pi * k * t / n) for t in range(n))
            im_part = sum(bits[t] * math.sin(2 * math.pi * k * t / n) for t in range(n))
            power = re_part * re_part + im_part * im_part
            score += power * (1 if k % 2 else -1)
        return score / (n * n)

    def analyze(self, value):
        raw = re.sub(r"\s+", "", value or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", raw):
            return {"ok": False, "error": "Mã phải là MD5 32 ký tự hoặc SHA-256 64 ký tự hệ hex."}
        data = bytes.fromhex(raw)
        bits = self._bits(data)
        score_tai = 50.0
        score_xiu = 50.0
        details = []

        # 1. Nibble/high-low score, lấy trực tiếp tinh thần ultimate_md5_core_v4.
        nibbles = [(b >> 4) & 15 for b in data] + [b & 15 for b in data]
        high = sum(1 for n in nibbles if n >= 8)
        low = len(nibbles) - high
        if high > low * 1.15:
            score_tai += 8; details.append("high-nibble→Tài")
        elif low > high * 1.15:
            score_xiu += 8; details.append("low-nibble→Xỉu")

        odd = sum(n % 2 for n in nibbles)
        even = len(nibbles) - odd
        if odd > even * 1.20:
            score_tai += 5
        elif even > odd * 1.20:
            score_xiu += 5

        # 2. Shannon entropy và phân bố byte.
        ent = self._entropy(data)
        ratio = ent / 8.0
        if ratio > 0.90:
            (score_tai if data[-1] >= 128 else score_xiu)
            if data[-1] >= 128: score_tai += 6
            else: score_xiu += 6
            details.append("entropy-cao")
        elif ratio < 0.70:
            if sum(data) / len(data) >= 128: score_xiu += 6
            else: score_tai += 6
            details.append("entropy-thap")

        # 3. Bit ratio và độ dài run.
        ones = sum(bits); zeros = len(bits) - ones
        if zeros > ones + 10: score_tai += 6
        elif ones > zeros + 10: score_xiu += 6
        runs = []
        run = 1
        for i in range(1, len(bits)):
            if bits[i] == bits[i - 1]: run += 1
            else: runs.append(run); run = 1
        runs.append(run)
        avg_run = sum(runs) / max(1, len(runs))
        if avg_run > 2.5: score_tai += 5
        elif avg_run < 1.8: score_xiu += 5

        # 4. Markov 2-step trên chuỗi nibble cao/thấp trong chính hash.
        trans = Counter()
        highbits = [int(n >= 8) for n in nibbles]
        for i in range(len(highbits) - 2):
            trans[(highbits[i], highbits[i + 1], highbits[i + 2])] += 1
        last = tuple(highbits[-2:])
        t = trans[(last[0], last[1], 1)]
        x = trans[(last[0], last[1], 0)]
        if t > x: score_tai += 7
        elif x > t: score_xiu += 7

        # 5. Fourier-like deterministic spectral score.
        spectral = self._spectral_score(bits)
        if spectral > 0.002: score_tai += 5; details.append("spectral→Tài")
        elif spectral < -0.002: score_xiu += 5; details.append("spectral→Xỉu")

        # 6. Cellular automaton Rule 30, cùng ý tưởng trong file mẫu.
        state = bits[:]
        for _ in range(8):
            state = [state[(i - 1) % len(state)] ^ (state[i] | state[(i + 1) % len(state)]) for i in range(len(state))]
        density = sum(state) / len(state)
        if density > 0.52: score_tai += 5
        elif density < 0.48: score_xiu += 5

        total = score_tai + score_xiu
        tai = round(score_tai / total * 100, 1)
        xiu = round(score_xiu / total * 100, 1)
        result = "Tài" if tai >= xiu else "Xỉu"
        confidence = round(max(tai, xiu), 1)
        return {"ok": True, "hash": raw, "result": result, "tai": tai, "xiu": xiu,
                "confidence": confidence, "detail": ", ".join(details) or "tổng hợp deterministic"}

analyzer = HashAnalyzer()

# ============================================================
# GIAO DIỆN TELEGRAM — chuyển trang bằng edit_message_text
# ============================================================
def nav_keyboard(uid):
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🎮 Chơi ngay", callback_data="play"), types.InlineKeyboardButton("💎 Mua gói", callback_data="packages"))
    k.add(types.InlineKeyboardButton("💳 Nạp tiền", callback_data="deposit"), types.InlineKeyboardButton("👤 Tài khoản", callback_data="account"))
    k.add(types.InlineKeyboardButton("📞 Liên hệ", callback_data="contact"), types.InlineKeyboardButton("🎁 Giftcode", callback_data="giftcode"))
    if is_admin(uid): k.add(types.InlineKeyboardButton("🛠 Quản trị", callback_data="admin_menu"))
    return k


def page_text(uid):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
    name = (u["username"] if u and u["username"] else (u["first_name"] if u and u["first_name"] else "bạn"))
    balance = fmt_money(u["balance"] if u and "balance" in u.keys() else 0)
    active = user_key(uid)
    package = html.escape(active["package_name"]) if active else ""
    expires = html.escape(active["expires_at"]) if active else ""
    remaining = ""
    if active:
        try: remaining = str(max(0, (datetime.fromisoformat(active["expires_at"]) - now()).days)) + " ngày"
        except Exception: remaining = ""
    return f"👑 <b>Md5 Bot Auza</b>\n✨ <i>Hệ thống phân tích MD5 Tài/Xỉu cao cấp</i>\n\n👋 Xin chào, <b>{html.escape(name)}</b>\n🆔 ID: <code>{uid}</code>\n💰 Số dư: <b>{balance}</b>\n📦 Gói: <b>{package}</b>\n⏳ Hạn dùng: <code>{expires}</code>\n⌛ Còn lại: <b>{remaining}</b>\n\n⚠️ Không có gói miễn phí. Mua gói để phân tích MD5 Tài/Xỉu."


def edit_page(call, text, markup):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=markup)
        except Exception: pass


def welcome(chat_id):
    bot.send_message(chat_id, page_text(chat_id), reply_markup=nav_keyboard(chat_id))


@bot.message_handler(commands=["start", "menu", "help"])
def start(message):
    # /start luôn là nút thoát: hủy bước nhập hash/key/giftcode/đơn đang chờ.
    try: bot.clear_step_handler_by_chat_id(message.chat.id)
    except Exception: pass
    register_user(message); welcome(message.chat.id)


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    uid, cid = call.from_user.id, call.message.chat.id
    try:
        if call.data == "home": edit_page(call, page_text(uid), nav_keyboard(uid))
        elif call.data == "packages": show_packages(cid, call)
        elif call.data == "deposit": ask_deposit(cid, call)
        elif call.data == "play": play(cid, call)
        elif call.data == "enter_key":
            edit_page(call, "🔐 <b>Nhập key</b>\n\nHãy gửi key của bạn trong tin nhắn tiếp theo.", types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Quay lại", callback_data="home")))
            bot.register_next_step_handler_by_chat_id(cid, activate_key)
        elif call.data.startswith("confirm_deposit:"):
            confirm_deposit(uid, int(call.data.split(":", 1)[1]), call)
        elif call.data == "account": show_account(cid, call)
        elif call.data == "contact": show_contact(cid, call)
        elif call.data == "giftcode":
            edit_page(call, "🎁 <b>NHẬP GIFTCODE</b>\n\nGửi mã giftcode ở tin nhắn kế tiếp.", types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home")))
            bot.register_next_step_handler_by_chat_id(cid, redeem_giftcode)
        elif call.data.startswith("confirm_buy:"): purchase_package(uid, call.data.split(":", 1)[1], call)
        elif call.data == "admin_menu": admin_menu(cid, call) if is_admin(uid) else None
        elif call.data.startswith("buy:"): buy_package(cid, call.data.split(":", 1)[1], call)
        elif call.data.startswith("approve:"): request_approve(uid, int(call.data.split(":")[1]), call)
        elif call.data.startswith("approve_confirm:"): decide_deposit(uid, int(call.data.split(":")[1]), True)
        elif call.data.startswith("reject:"): request_reject(uid, int(call.data.split(":")[1]), call)
        elif call.data.startswith("reject_confirm:"): decide_deposit(uid, int(call.data.split(":")[1]), False)
        elif call.data == "admin_key": edit_page(call, "🔑 <b>Tạo key</b>\n\nDùng lệnh <code>/taokey Tên_gói</code> để tạo key.", types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Admin", callback_data="admin_menu")))
        elif call.data == "admin_stats": send_stats(cid, call)
        elif call.data == "admin_broadcast":
            edit_page(call, "📢 <b>THÔNG BÁO TOÀN BỘ</b>\n\nHãy nhập nội dung thông báo ở tin nhắn kế tiếp.", types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Admin", callback_data="admin_menu")))
            bot.register_next_step_handler_by_chat_id(cid, broadcast_next_step)
        bot.answer_callback_query(call.id)
    except Exception:
        log.exception("callback error")
        bot.answer_callback_query(call.id, "Có lỗi, hãy thử lại.", show_alert=True)


def show_packages(cid, call=None):
    packages = get_setting("packages", DEFAULT_PACKAGES)
    lines = ["💎 <b>CÁC GÓI KEY</b>", "", "Chọn gói phù hợp để sử dụng hệ thống:"]
    k = types.InlineKeyboardMarkup(row_width=1)
    for name, p in packages.items():
        lines.append(f"🔹 <b>{html.escape(name)}</b>  •  {fmt_money(p['price'])}  •  {p['days']} ngày")
        k.add(types.InlineKeyboardButton(f"🛒 Mua {name}", callback_data="buy:" + name[:50]))
    k.add(types.InlineKeyboardButton("💳 Nạp tiền", callback_data="deposit"), types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, "\n".join(lines), k)
    else: bot.send_message(cid, "\n".join(lines), reply_markup=k)


def ask_deposit(cid, call=None):
    text = "💳 <b>NẠP TIỀN</b>\n\nNhập số tiền muốn nạp, chỉ nhập số.\nVí dụ: <code>50000</code>"
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Quay lại", callback_data="home"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)
    bot.register_next_step_handler_by_chat_id(cid, create_deposit)


def create_deposit(message):
    if (message.text or "").strip().lower() in ("/start", "/menu"):
        try: bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception: pass
        register_user(message); welcome(message.chat.id); return
    register_user(message); uid = message.chat.id
    try: amount = int(re.sub(r"[^0-9]", "", message.text or ""))
    except ValueError: amount = 0
    if amount <= 0: bot.send_message(uid, "❌ Số tiền không hợp lệ."); return
    with db() as c:
        pending = c.execute("SELECT COUNT(*) n FROM deposits WHERE telegram_id=? AND status='pending'", (uid,)).fetchone()["n"]
        last = c.execute("SELECT created_at FROM deposits WHERE telegram_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if pending >= 3:
        bot.send_message(uid, "⚠️ Bạn đang có tối đa 3 đơn chờ. Vui lòng chờ xử lý."); return
    if pending == 0 and last:
        try:
            if now() - datetime.fromisoformat(last["created_at"]) < timedelta(minutes=5):
                bot.send_message(uid, "⏳ Vui lòng chờ 5 phút giữa các lần tạo đơn nạp."); return
        except ValueError: pass
    bank = get_setting("bank", DEFAULT_BANK); content = f"{bank['note_prefix']}{uid}"
    with db() as c:
        cur = c.execute("INSERT INTO deposits(telegram_id,amount,content,status,created_at) VALUES(?,?,?,?,?)", (uid, amount, content, "pending", iso(now())))
        did = cur.lastrowid
    qr = f"https://img.vietqr.io/image/{bank['bank_code']}-{bank['account_no']}-compact2.png?amount={amount}&addInfo={content}&accountName={requests.utils.quote(bank['account_name'])}"
    caption = f"💳 <b>ĐƠN NẠP #{did}</b>\n\n🏦 Ngân hàng: <code>{html.escape(bank['bank_code'])}</code>\n🔢 STK: <code>{html.escape(bank['account_no'])}</code>\n👤 Tên: <code>{html.escape(bank['account_name'])}</code>\n💰 Số tiền: <b>{fmt_money(amount)}</b>\n📝 Nội dung: <code>{content}</code>\n\nSau khi chuyển khoản, hãy bấm nút bên dưới."
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Tôi đã nạp tiền", callback_data=f"confirm_deposit:{did}"), types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    bot.send_photo(uid, qr, caption=caption, reply_markup=k)


def buy_package(cid, name, call=None):
    packages = get_setting("packages", DEFAULT_PACKAGES)
    p = packages.get(name)
    if not p:
        text = "❌ Gói này không còn tồn tại."
        k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Gói key", callback_data="packages"))
    else:
        with db() as c: u = c.execute("SELECT COALESCE(balance,0) balance FROM users WHERE telegram_id=?", (cid,)).fetchone()
        balance = int(u["balance"] if u else 0)
        text = f"💎 <b>{html.escape(name)}</b>\n\n💰 Giá: <b>{fmt_money(p['price'])}</b>\n⏱ Thời hạn: <b>{p['days']} ngày</b>\n💳 Số dư của bạn: <b>{fmt_money(balance)}</b>\n\nBấm xác nhận để hệ thống trừ tiền và giao key tự động."
        k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ Xác nhận mua key", callback_data="confirm_buy:" + name[:50]), types.InlineKeyboardButton("💳 Nạp tiền", callback_data="deposit"), types.InlineKeyboardButton("↩️ Gói key", callback_data="packages"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)


def purchase_package(uid, name, call=None):
    packages = get_setting("packages", DEFAULT_PACKAGES); p = packages.get(name)
    if not p:
        text = "❌ Gói không tồn tại."
    else:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            u = c.execute("SELECT COALESCE(balance,0) balance FROM users WHERE telegram_id=?", (uid,)).fetchone()
            balance = int(u["balance"] if u else 0)
            price = int(p["price"]); days = int(p["days"])
            if balance < price:
                text = f"❌ Số dư không đủ. Bạn có <b>{fmt_money(balance)}</b>, cần <b>{fmt_money(price)}</b>."
            else:
                # Tự sinh key mới trong cùng transaction; không cần kho key tạo trước.
                changed = c.execute("UPDATE users SET balance=balance-? WHERE telegram_id=? AND balance>=?", (price, uid, price)).rowcount
                if changed != 1:
                    text = "⚠️ Giao dịch đã được xử lý hoặc số dư thay đổi. Vui lòng mở lại Mua gói."
                else:
                    created = iso(now()); exp = iso(now() + timedelta(days=days))
                    count = c.execute("SELECT COUNT(*) n FROM keys").fetchone()["n"] + 1
                    seed = f"{BOT_TOKEN}:{uid}:{name}:{count}:{time.time_ns()}".encode()
                    key_value = "TX-" + hashlib.sha256(seed).hexdigest()[:20].upper()
                    c.execute("INSERT INTO keys(key,package_name,days,used_by,created_at,used_at,expires_at) VALUES(?,?,?,?,?,?,?)", (key_value, name, days, uid, created, created, exp))
                    text = f"✅ <b>MUA KEY THÀNH CÔNG</b>\n\n💎 Gói: <b>{html.escape(name)}</b>\n🔑 Key mới của bạn: <code>{key_value}</code>\n⏳ Hạn dùng: <code>{exp}</code>\n💳 Số dư còn lại: <b>{fmt_money(balance-price)}</b>"
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🎮 Chơi ngay", callback_data="play"), types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, text, k)
    else: bot.send_message(uid, text, reply_markup=k)


def show_contact(cid, call=None):
    bank = get_setting("bank", DEFAULT_BANK); link = bank.get("contact_link", "https://t.me/")
    text = f"📞 <b>LIÊN HỆ HỖ TRỢ</b>\n\nNếu cần hỗ trợ nạp tiền, mua key hoặc đối soát đơn, hãy liên hệ admin."
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("💬 Mở liên hệ", url=link), types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)


def play(cid, call=None):
    row = user_key(cid)
    if not row:
        text = "🔒 <b>KHU VỰC CHƠI</b>\n\nBạn chưa có key còn hạn. Hãy nhập key hoặc mua gói để tiếp tục."
        k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔐 Nhập key", callback_data="enter_key"), types.InlineKeyboardButton("💎 Mua gói", callback_data="packages"))
    else:
        text = f"🎮 <b>SẴN SÀNG PHÂN TÍCH</b>\n\nKey còn hạn đến: <code>{row['expires_at']}</code>\n\nGửi mã MD5 32 ký tự hoặc SHA-256 64 ký tự."
        k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)
    if row: bot.register_next_step_handler_by_chat_id(cid, analyze_message)


def analyze_message(message):
    if (message.text or "").strip().lower() in ("/start", "/menu"):
        try: bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception: pass
        register_user(message); welcome(message.chat.id); return
    if not user_key(message.chat.id):
        bot.send_message(message.chat.id, "🔒 Key đã hết hạn hoặc chưa được kích hoạt.")
        return
    out = analyzer.analyze(message.text)
    if not out["ok"]:
        bot.send_message(message.chat.id, "❌ " + out["error"] + "\n\n🔁 Hãy gửi lại mã MD5/SHA hợp lệ.")
        bot.register_next_step_handler_by_chat_id(message.chat.id, analyze_message)
        return
    digest = out['hash'].upper()
    short = digest[:8] + "..." + digest[-8:]
    nums = [int(digest[i:i+2], 16) % 6 + 1 for i in (0, 2, 4)]
    total = sum(nums)
    verdict = "🅣 TÀI" if out['result'] == "Tài" else "🅧 XỈU"
    text = (f"🔮 <b>PHÂN TÍCH MD5 TÀI/XỈU</b>\n\n"
            f"📦 Phiên bản: <b>Mới Nhất</b>\n"
            f"📝 MD5 hiện tại: <code>{short}</code>\n\n"
            f"🎲 Bộ số mô phỏng: <b>{nums[0]}-{nums[1]}-{nums[2]}</b> | Tổng: <b>{total}</b>\n"
            f"📉 Kết luận: <b>{verdict}</b>\n"
            f"🎯 Tài/Xỉu %: <b>T {out['tai']}% · X {out['xiu']}%</b>")
    bot.send_message(message.chat.id, text)
    # Giữ phiên chơi mở: user có thể gửi mã tiếp theo ngay lập tức.
    bot.register_next_step_handler_by_chat_id(message.chat.id, analyze_message)


def show_account(cid, call=None):
    row = user_key(cid)
    if row: text = f"👤 <b>TÀI KHOẢN</b>\n\n🔑 Gói: <b>{html.escape(row['package_name'])}</b>\n⏳ Hết hạn: <code>{row['expires_at']}</code>"
    else: text = "👤 <b>TÀI KHOẢN</b>\n\nBạn chưa có key còn hạn."
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("💎 Mua gói", callback_data="packages"), types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)

@bot.message_handler(commands=["nhapkey"])
def enter_key_cmd(message):
    bot.send_message(message.chat.id, "🔐 Gửi key cần kích hoạt."); bot.register_next_step_handler_by_chat_id(message.chat.id, activate_key)

@bot.callback_query_handler(func=lambda call: call.data == "enter_key_legacy")
def enter_key_legacy(call):
    bot.send_message(call.message.chat.id, "🔐 Gửi key cần kích hoạt."); bot.register_next_step_handler_by_chat_id(call.message.chat.id, activate_key)


def activate_key(message):
    if (message.text or "").strip().lower() in ("/start", "/menu"):
        try: bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception: pass
        register_user(message); welcome(message.chat.id); return
    key = (message.text or "").strip().upper()
    with db() as c:
        row = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
        if not row or row["used_by"] is not None: bot.send_message(message.chat.id, "❌ Key không đúng hoặc đã được sử dụng."); return
        exp = iso(now() + timedelta(days=int(row["days"])))
        c.execute("UPDATE keys SET used_by=?,used_at=?,expires_at=? WHERE key=?", (message.chat.id, iso(now()), exp, key))
    bot.send_message(message.chat.id, f"✅ <b>Kích hoạt key thành công!</b>\n\n💎 Gói: <b>{html.escape(row['package_name'])}</b>\n⏳ Hạn đến: <code>{exp}</code>")

def redeem_giftcode(message):
    if (message.text or "").strip().lower() in ("/start", "/menu"):
        try: bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception: pass
        register_user(message); welcome(message.chat.id); return
    register_user(message)
    uid = message.chat.id; code = (message.text or "").strip().upper()
    if not code:
        bot.send_message(uid, "❌ Mã giftcode không hợp lệ."); return
    with db() as c:
        row = c.execute("SELECT * FROM giftcodes WHERE code=?", (code,)).fetchone()
        used = c.execute("SELECT 1 FROM giftcode_uses WHERE code=? AND telegram_id=?", (code, uid)).fetchone()
        if not row or row["used_count"] >= row["max_uses"] or used:
            bot.send_message(uid, "❌ Giftcode không tồn tại, đã hết lượt hoặc bạn đã dùng mã này."); return
        c.execute("INSERT OR IGNORE INTO giftcode_uses(code,telegram_id,used_at) VALUES(?,?,?)", (code, uid, iso(now())))
        c.execute("UPDATE giftcodes SET used_count=used_count+1 WHERE code=? AND used_count<max_uses", (code,))
        c.execute("UPDATE users SET balance=COALESCE(balance,0)+? WHERE telegram_id=?", (row["amount"], uid))
        balance = c.execute("SELECT balance FROM users WHERE telegram_id=?", (uid,)).fetchone()["balance"]
    bot.send_message(uid, f"🎉 <b>ĐỔI GIFTCODE THÀNH CÔNG</b>\n\n💰 Nhận được: <b>{fmt_money(row['amount'])}</b>\n💳 Số dư hiện tại: <b>{fmt_money(balance)}</b>")


# ============================================================
# ADMIN TELEGRAM
# ============================================================
def admin_menu(cid, call=None):
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🔑 Tạo key", callback_data="admin_key"), types.InlineKeyboardButton("📊 Thống kê", callback_data="admin_stats"))
    k.add(types.InlineKeyboardButton("📢 Thông báo toàn bộ", callback_data="admin_broadcast"))
    k.add(types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, "🛠 <b>BẢNG QUẢN TRỊ</b>\n\nChọn chức năng quản lý bên dưới.", k)
    else: bot.send_message(cid, "🛠 <b>BẢNG QUẢN TRỊ</b>", reply_markup=k)

@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    if is_admin(message.from_user.id): admin_menu(message.chat.id)

@bot.message_handler(commands=["taokey"])
def create_key_cmd(message):
    if not is_admin(message.from_user.id): return
    name = " ".join((message.text or "").split()[1:]).replace("_", " ").strip()
    packages = get_setting("packages", DEFAULT_PACKAGES)
    if name not in packages:
        bot.send_message(message.chat.id, "Tên gói không đúng. Gói hiện có: " + ", ".join(packages)); return
    p = packages[name]
    with db() as c:
        count = c.execute("SELECT COUNT(*) n FROM keys").fetchone()["n"] + 1
    seed = f"{BOT_TOKEN}:{message.from_user.id}:{name}:{count}:{time.time_ns()}".encode()
    key = "TX-" + hashlib.sha256(seed).hexdigest()[:20].upper()
    with db() as c: c.execute("INSERT INTO keys(key,package_name,days,created_at) VALUES(?,?,?,?)", (key, name, p["days"], iso(now())))
    bot.send_message(message.chat.id, f"Đã tạo key cho <b>{html.escape(name)}</b>:\n<code>{key}</code>")

@bot.message_handler(commands=["taogift"])
def create_gift_cmd(message):
    if not is_admin(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Dùng /taogift TENGIFT [soluot]. Mỗi mã nhận ngẫu nhiên từ 1.000đ đến 10.000đ."); return
    code = parts[1].upper()
    try: max_uses = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError: max_uses = 1
    # Chỉ giftcode dùng ngẫu nhiên; bộ phân tích hash vẫn deterministic.
    import secrets
    amount = secrets.randbelow(9001) + 1000
    with db() as c: c.execute("INSERT OR REPLACE INTO giftcodes(code,amount,max_uses,used_count,created_at) VALUES(?,?,?,?,?)", (code, amount, max_uses, 0, iso(now())))
    bot.send_message(message.chat.id, f"🎁 Đã tạo giftcode <code>{code}</code>\n💰 Giá trị: <b>{fmt_money(amount)}</b>\n👥 Số lượt: <b>{max_uses}</b>")


def broadcast_text(admin_chat_id, text):
    with db() as c: users = [r["telegram_id"] for r in c.execute("SELECT telegram_id FROM users")]
    sent = 0
    for target in users:
        try: bot.send_message(target, "📢 <b>Thông báo từ admin</b>\n\n" + html.escape(text)); sent += 1
        except Exception: pass
    with db() as c: c.execute("INSERT INTO broadcasts(text,sent_at) VALUES(?,?)", (text, iso(now())))
    bot.send_message(admin_chat_id, f"✅ Đã gửi thông báo tới {sent}/{len(users)} người dùng.")


def broadcast_next_step(message):
    if is_admin(message.from_user.id) and (message.text or "").strip():
        broadcast_text(message.chat.id, message.text.strip())


@bot.message_handler(commands=["thongbao"])
def broadcast_cmd(message):
    if not is_admin(message.from_user.id): return
    text = (message.text or "").partition(" ")[2].strip()
    if text: broadcast_text(message.chat.id, text)
    else:
        bot.send_message(message.chat.id, "📢 Hãy nhập nội dung thông báo ở tin nhắn kế tiếp.")
        bot.register_next_step_handler_by_chat_id(message.chat.id, broadcast_next_step)


def send_stats(cid, call=None):
    with db() as c:
        u = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        k = c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NULL").fetchone()["n"]
        d = c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"]
        total = c.execute("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status='approved'").fetchone()["s"]
    text = f"📊 <b>THỐNG KÊ HỆ THỐNG</b>\n\n👥 Người dùng: <b>{u}</b>\n🔑 Key chưa dùng: <b>{k}</b>\n⏳ Đơn chờ duyệt: <b>{d}</b>\n💰 Tổng đã duyệt: <b>{fmt_money(total)}</b>"
    kbd = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Quản trị", callback_data="admin_menu"))
    if call: edit_page(call, text, kbd)
    else: bot.send_message(cid, text, reply_markup=kbd)


def notify_admin_deposit(did):
    with db() as c:
        r = c.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
    if not r: return
    k = types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("✅ Duyệt đơn", callback_data=f"approve:{did}"), types.InlineKeyboardButton("❌ Từ chối", callback_data=f"reject:{did}"))
    text = f"🔔 <b>YÊU CẦU XÁC NHẬN ĐƠN NẠP #{did}</b>\n\n👤 User: <code>{r['telegram_id']}</code>\n💰 Số tiền: <b>{fmt_money(r['amount'])}</b>\n📝 Nội dung: <code>{r['content']}</code>\n\nVui lòng kiểm tra giao dịch thực tế trước khi duyệt."
    for aid in ADMIN_IDS:
        try: bot.send_message(aid, text, reply_markup=k)
        except Exception: pass


def request_approve(admin_uid, did, call=None):
    if not is_admin(admin_uid): return
    with db() as c: r = c.execute("SELECT * FROM deposits WHERE id=? AND status='pending'", (did,)).fetchone()
    if not r: return
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("✅ XÁC NHẬN DUYỆT & CỘNG TIỀN", callback_data=f"approve_confirm:{did}"), types.InlineKeyboardButton("↩️ Hủy", callback_data="admin_menu"))
    edit_page(call, f"⚠️ <b>XÁC NHẬN DUYỆT ĐƠN #{did}</b>\n\n💰 Số tiền: <b>{fmt_money(r['amount'])}</b>\n👤 User: <code>{r['telegram_id']}</code>\n\nSau khi xác nhận, hệ thống sẽ cộng tiền vào số dư user. Hãy chắc chắn đã kiểm tra giao dịch.", k)


def request_reject(admin_uid, did, call=None):
    if not is_admin(admin_uid): return
    with db() as c: r = c.execute("SELECT * FROM deposits WHERE id=? AND status='pending'", (did,)).fetchone()
    if not r: return
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ XÁC NHẬN TỪ CHỐI", callback_data=f"reject_confirm:{did}"), types.InlineKeyboardButton("↩️ Hủy", callback_data="admin_menu"))
    edit_page(call, f"⚠️ <b>XÁC NHẬN TỪ CHỐI ĐƠN #{did}</b>\n\nUser: <code>{r['telegram_id']}</code>\nSố tiền: <b>{fmt_money(r['amount'])}</b>", k)


def confirm_deposit(uid, did, call=None):
    with db() as c: r = c.execute("SELECT * FROM deposits WHERE id=? AND telegram_id=?", (did, uid)).fetchone()
    if not r or r["status"] != "pending":
        bot.send_message(uid, "❌ Đơn không tồn tại hoặc đã xử lý."); return
    bot.send_message(uid, f"✅ Đã gửi xác nhận đơn nạp <b>#{did}</b> tới admin. Vui lòng chờ duyệt.")
    notify_admin_deposit(did)


def decide_deposit(uid, did, approved):
    if not is_admin(uid): return
    with db() as c:
        r = c.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if not r or r["status"] != "pending": return
        status = "approved" if approved else "rejected"
        c.execute("UPDATE deposits SET status=?,decided_at=?,decided_by=? WHERE id=?", (status, iso(now()), uid, did))
        if approved:
            c.execute("INSERT INTO users(telegram_id,username,first_name,balance,created_at,last_seen) VALUES(?,?,?,?,?,?) ON CONFLICT(telegram_id) DO NOTHING", (r["telegram_id"], "", "", 0, iso(now()), iso(now())))
            c.execute("UPDATE users SET balance=COALESCE(balance,0)+? WHERE telegram_id=?", (int(r["amount"]), r["telegram_id"]))
    if approved:
        with db() as c: bal = c.execute("SELECT balance FROM users WHERE telegram_id=?", (r["telegram_id"],)).fetchone()["balance"]
        bot.send_message(r["telegram_id"], f"✅ <b>NẠP TIỀN THÀNH CÔNG</b>\n\nĐơn <b>#{did}</b> đã được admin xác nhận.\n💰 Đã cộng: <b>{fmt_money(r['amount'])}</b>\n💳 Số dư hiện tại: <b>{fmt_money(bal)}</b>\n\nBạn có thể vào Mua gói để nhận key tự động.")
        bot.send_message(uid, f"✅ Đã duyệt đơn <b>#{did}</b> và cộng <b>{fmt_money(r['amount'])}</b> vào tài khoản user.")
    else:
        bot.send_message(r["telegram_id"], f"❌ Đơn nạp #{did} đã bị từ chối. Vui lòng liên hệ admin để đối soát.")
        bot.send_message(uid, f"❌ Đã xác nhận từ chối đơn <b>#{did}</b>.")


# ============================================================
# ADMIN WEB KHÔNG ĐĂNG NHẬP — CHỈ DÙNG MÔI TRƯỜNG RIÊNG
# ============================================================
ADMIN_HTML = """
<!doctype html><html lang='vi'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{{bot_name}} · Admin</title>
<style>
:root{--bg:#080d1c;--panel:#111a2e;--panel2:#16223a;--line:#263756;--text:#f4f7fb;--muted:#92a4c4;--blue:#4f7cff;--cyan:#27d3c2;--green:#31d18b;--orange:#ffb454;--red:#ff6b7a}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#1c3262 0,transparent 34%),var(--bg);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}.shell{display:flex;min-height:100vh}.side{width:245px;background:rgba(7,12,27,.86);border-right:1px solid var(--line);padding:25px 16px}.brand{display:flex;gap:12px;align-items:center;font-weight:800;font-size:16px;margin-bottom:35px}.logo{width:40px;height:40px;border-radius:13px;background:linear-gradient(135deg,var(--blue),var(--cyan));display:grid;place-items:center;font-weight:900}.nav{display:grid;gap:7px}.nav a{color:var(--muted);text-decoration:none;padding:12px 13px;border-radius:10px}.nav a:hover,.nav a.active{background:#182846;color:white}.content{flex:1;padding:28px 4.5vw 55px;max-width:1500px}.top{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:25px}.eyebrow{color:var(--cyan);text-transform:uppercase;letter-spacing:1.8px;font-size:11px;font-weight:800}.top h1{font-size:30px;margin:7px 0}.muted{color:var(--muted)}.notice{background:#302719;border:1px solid #725a31;color:#ffd990;padding:12px 15px;border-radius:12px;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}.card,section{background:linear-gradient(145deg,rgba(22,34,58,.95),rgba(14,23,42,.95));border:1px solid var(--line);border-radius:16px;padding:19px;box-shadow:0 14px 35px #0002}.stat{font-size:28px;font-weight:800;margin:7px 0}.label{color:var(--muted);font-size:12px}.accent{color:var(--cyan)}.row{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}section h2{font-size:17px;margin:0 0 17px}label{display:block;color:var(--muted);font-size:12px;margin:11px 0 5px}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:9px;background:#0c1529;color:var(--text);padding:11px 12px;outline:none}input:focus,textarea:focus,select:focus{border-color:var(--blue)}button{border:0;border-radius:9px;background:linear-gradient(135deg,#4f7cff,#3560df);color:#fff;padding:11px 16px;font-weight:700;cursor:pointer;margin-top:14px}button:hover{filter:brightness(1.12)}.btn-green{background:linear-gradient(135deg,#20bd7a,#179c68)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:12px 9px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600}.pill{display:inline-block;padding:5px 9px;border-radius:99px;font-size:11px;font-weight:700}.pending{background:#49391b;color:#ffd47c}.approved{background:#153e32;color:#67e5ae}.rejected{background:#472631;color:#ff9aa6}code{color:#8fe9ff;background:#0b1427;padding:3px 6px;border-radius:5px}.footer{color:#647594;font-size:12px;margin-top:18px}@media(max-width:900px){.side{width:190px}.grid{grid-template-columns:repeat(2,1fr)}.row{grid-template-columns:1fr}}@media(max-width:620px){.shell{display:block}.side{width:100%;border-right:0;border-bottom:1px solid var(--line);padding:16px}.brand{margin-bottom:12px}.nav{display:flex;overflow:auto}.content{padding:22px 15px}.grid{grid-template-columns:1fr 1fr}.top{display:block}}
</style></head><body><div class='shell'><aside class='side'><div class='brand'><div class='logo'>TX</div><div>{{bot_name}}<div class='muted' style='font-size:11px;margin-top:3px'>CONTROL CENTER</div></div></div><nav class='nav'><a class='active' href='#overview'>▦ Tổng quan</a><a href='#runtime'>⚙ Cấu hình bot</a><a href='#bank'>◈ VietQR & ngân hàng</a><a href='#packages'>◇ Gói key</a><a href='#deposits'>⇄ Đơn nạp</a><a href='#users'>👥 Người dùng</a><a href='#keys'>✦ Tạo key</a></nav></aside><main class='content'><header class='top'><div><div class='eyebrow'>Management dashboard</div><h1>Xin chào, quản trị viên</h1><div class='muted'>Quản lý bot, giao dịch và key trong một nơi.</div></div><div class='pill {{"approved" if bot_ready else "pending"}}'>● {{'BOT ĐANG CHẠY' if bot_ready else 'CHỜ CẤU HÌNH BOT'}}</div></header><div class='notice'><b>Lưu ý bảo mật:</b> giao diện này không có đăng nhập theo yêu cầu ban đầu. Không chia sẻ URL admin công khai.</div><div id='overview' class='grid'><div class='card'><div class='label'>NGƯỜI DÙNG</div><div class='stat'>{{stats.users}}</div><div class='muted'>tài khoản đã đăng ký</div></div><div class='card'><div class='label'>KEY CHƯA DÙNG</div><div class='stat accent'>{{stats.unused_keys}}</div><div class='muted'>sẵn sàng cấp</div></div><div class='card'><div class='label'>ĐƠN CHỜ DUYỆT</div><div class='stat' style='color:var(--orange)'>{{stats.pending}}</div><div class='muted'>cần kiểm tra</div></div><div class='card'><div class='label'>DOANH THU ĐÃ DUYỆT</div><div class='stat' style='color:var(--green)'>{{stats.revenue}}</div><div class='muted'>tổng tiền nạp</div></div></div><div id='runtime' class='row'><section><h2>⚙ Cấu hình bot</h2><form method='post' action='/admin/runtime'><label>Bot Token từ BotFather</label><input type='password' name='bot_token' placeholder='Dán token có dạng 123456:AA...' value='{{token_value}}'><label>Telegram ID admin</label><input name='admin_ids' placeholder='Ví dụ: 123456789,987654321' value='{{admin_ids}}'><button class='btn-green'>Lưu & khởi động bot</button></form></section><section id='bank'><h2>◈ Tài khoản nhận tiền</h2><form method='post' action='/admin/bank'><label>Mã ngân hàng</label><input name='bank_code' value='{{bank.bank_code}}'><label>Số tài khoản</label><input name='account_no' value='{{bank.account_no}}'><label>Tên chủ tài khoản</label><input name='account_name' value='{{bank.account_name}}'><label>Tiền tố nội dung</label><input name='note_prefix' value='{{bank.note_prefix}}'><label>Link liên hệ admin</label><input name='contact_link' type='url' placeholder='https://t.me/ten_admin' value='{{bank.contact_link}}'><button>Lưu thông tin VietQR & liên hệ</button></form></section></div><section id='packages'><h2>◇ Quản lý gói key</h2><div class='muted'>Nhập tên gói, giá tiền và số ngày rồi bấm thêm. Không cần sửa JSON.</div><form method='post' action='/admin/package/add' style='display:grid;grid-template-columns:1.3fr 1fr 1fr auto;gap:10px;align-items:end'><div><label>Tên gói</label><input name='name' placeholder='Ví dụ: Gói VIP 7 ngày' required></div><div><label>Giá tiền</label><input name='price' type='number' min='0' placeholder='50000' required></div><div><label>Số ngày</label><input name='days' type='number' min='1' placeholder='7' required></div><button>＋ Thêm gói</button></form><div style='overflow:auto;margin-top:18px'><table><tr><th>Tên gói</th><th>Giá</th><th>Thời hạn</th><th></th></tr>{% for n,p in packages.items() %}<tr><td><b>{{n}}</b></td><td>{{p.price}}</td><td>{{p.days}} ngày</td><td><form method='post' action='/admin/package/delete'><input type='hidden' name='name' value='{{n}}'><button style='background:linear-gradient(135deg,#e65b6e,#b7354b);margin:0'>Xóa</button></form></td></tr>{% else %}<tr><td colspan='4' class='muted'>Chưa có gói.</td></tr>{% endfor %}</table></div></section><section id='deposits' style='margin-top:18px'><h2>⇄ 100 đơn nạp gần nhất</h2><div style='overflow:auto'><table><tr><th>ID</th><th>Telegram ID</th><th>Số tiền</th><th>Nội dung</th><th>Trạng thái</th><th>Thời gian</th></tr>{% for d in deposits %}<tr><td>#{{d.id}}</td><td>{{d.telegram_id}}</td><td><b>{{d.amount}}</b></td><td><code>{{d.content}}</code></td><td><span class='pill {{d.status}}'>{{d.status}}</span></td><td class='muted'>{{d.created_at[:19]}}</td></tr>{% else %}<tr><td colspan='6' class='muted'>Chưa có đơn nạp.</td></tr>{% endfor %}</table></div></section><section id='users' style='margin-top:18px'><h2>👥 Quản lý người dùng & số dư</h2><div style='overflow:auto'><table><tr><th>Telegram ID</th><th>Tên</th><th>Username</th><th>Số dư</th><th>Cộng / trừ tiền</th></tr>{% for u in users %}<tr><td><code>{{u.telegram_id}}</code></td><td>{{u.first_name}}</td><td>@{{u.username}}</td><td><b>{{u.balance}}</b></td><td><form method='post' action='/admin/user/balance' style='display:flex;gap:6px;min-width:290px'><input type='hidden' name='telegram_id' value='{{u.telegram_id}}'><input name='amount' type='number' placeholder='10000' required><button style='margin:0'>Cộng</button><button name='mode' value='subtract' style='margin:0;background:linear-gradient(135deg,#e65b6e,#b7354b)'>Trừ</button></form></td></tr>{% else %}<tr><td colspan='5' class='muted'>Chưa có user.</td></tr>{% endfor %}</table></div></section><div id='keys' class='row' style='margin-top:18px'><section><h2>✦ Tạo key nhanh</h2><form method='post' action='/admin/key'><label>Chọn gói</label><select name='package'>{% for n in packages %}<option>{{n}}</option>{% endfor %}</select><button>Tạo key mới</button></form>{% if generated %}<div style='margin-top:18px'>Key mới: <code>{{generated}}</code></div>{% endif %}</section><section><h2>Trạng thái hệ thống</h2><div class='muted'>Health endpoint</div><p><span class='pill approved'>● ONLINE</span> <code>/</code></p><div class='muted'>Database</div><p><span class='pill approved'>● READY</span> SQLite</p></section></div><div class='footer'>{{bot_name}} · Admin Control Center · Dữ liệu được lưu trong SQLite</div></main></div></body></html>
"""

@app.get("/")
def health(): return jsonify(ok=True, service=BOT_NAME, time=iso(now()))

@app.get("/admin")
def admin_page():
    with db() as c:
        deposits = c.execute("SELECT * FROM deposits ORDER BY id DESC LIMIT 100").fetchall()
        users = c.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 200").fetchall()
        stats = {
            "users": c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
            "unused_keys": c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NULL").fetchone()["n"],
            "pending": c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"],
            "revenue": fmt_money(c.execute("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status='approved'").fetchone()["s"]),
        }
    packages = get_setting("packages", DEFAULT_PACKAGES); bank = get_setting("bank", DEFAULT_BANK)
    token = str(get_setting("bot_token", BOT_TOKEN) or "")
    ids = get_setting("admin_ids", sorted(x for x in ADMIN_IDS if x != 0))
    return render_template_string(ADMIN_HTML, bot_name=BOT_NAME, bot_ready=bool(token), deposits=deposits, users=users, packages=packages, bank=bank, stats=stats, admin_ids=','.join(map(str, ids)), token_value="", packages_json=json.dumps(packages, ensure_ascii=False, indent=2), generated=request.args.get("generated", ""))

@app.post("/admin/runtime")
def admin_runtime():
    token = request.form.get("bot_token", "").strip()
    old_token = str(get_setting("bot_token", BOT_TOKEN) or "")
    if token:
        if ":" not in token:
            return "BOT_TOKEN không hợp lệ: token phải có dấu hai chấm (:).", 400
        set_setting("bot_token", token)
    elif not old_token:
        return "Hãy nhập BOT_TOKEN từ BotFather.", 400
    raw_ids = request.form.get("admin_ids", "")
    ids = [int(x.strip()) for x in raw_ids.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit() and int(x.strip()) != 0]
    if not ids:
        return "Hãy nhập ít nhất một Telegram ID admin.", 400
    set_setting("admin_ids", ids)
    start_bot_if_configured()
    return redirect("/admin")

@app.post("/admin/bank")
def admin_bank():
    set_setting("bank", {k: request.form.get(k, "").strip() for k in DEFAULT_BANK})
    return redirect("/admin")

@app.post("/admin/package/add")
def admin_package_add():
    name = request.form.get("name", "").strip()
    try:
        price, days = int(request.form.get("price", "0")), int(request.form.get("days", "0"))
    except ValueError:
        return "Giá hoặc số ngày không hợp lệ", 400
    if not name or price < 0 or days < 1: return "Tên, giá và số ngày không hợp lệ", 400
    packages = get_setting("packages", DEFAULT_PACKAGES); packages[name] = {"price": price, "days": days}; set_setting("packages", packages)
    return redirect("/admin#packages")

@app.post("/admin/package/delete")
def admin_package_delete():
    packages = get_setting("packages", DEFAULT_PACKAGES); packages.pop(request.form.get("name", ""), None); set_setting("packages", packages)
    return redirect("/admin#packages")

@app.post("/admin/user/balance")
def admin_user_balance():
    try: uid, amount = int(request.form.get("telegram_id", "0")), int(request.form.get("amount", "0"))
    except ValueError: return "Dữ liệu không hợp lệ", 400
    if amount <= 0: return "Số tiền phải lớn hơn 0", 400
    delta = -amount if request.form.get("mode") == "subtract" else amount
    with db() as c: c.execute("UPDATE users SET balance=COALESCE(balance,0)+? WHERE telegram_id=?", (delta, uid))
    return redirect("/admin#users")

@app.post("/admin/packages")
def admin_packages():
    try:
        value = json.loads(request.form.get("packages", "{}"))
        if not isinstance(value, dict): raise ValueError()
        set_setting("packages", value)
    except Exception: return "JSON gói không hợp lệ", 400
    return redirect("/admin")

@app.post("/admin/key")
def admin_key():
    name = request.form.get("package", "")
    p = get_setting("packages", DEFAULT_PACKAGES).get(name)
    if not p: return "Gói không tồn tại", 400
    with db() as c: count = c.execute("SELECT COUNT(*) n FROM keys").fetchone()["n"] + 1
    key = "TX-" + hashlib.sha256(f"{BOT_TOKEN}:{name}:{count}:{time.time_ns()}".encode()).hexdigest()[:20].upper()
    with db() as c: c.execute("INSERT INTO keys(key,package_name,days,created_at) VALUES(?,?,?,?)", (key, name, int(p["days"]), iso(now())))
    return redirect("/admin?generated=" + key)

@app.get("/admin/stats")
def admin_stats_api():
    with db() as c:
        return jsonify(users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"], pending_deposits=c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"], unused_keys=c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NULL").fetchone()["n"])


def run_web():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    init_db()
    # Luôn mở web admin trước. Token có thể nhập từ /admin sau khi Render chạy.
    start_bot_if_configured()
    threading.Thread(target=run_web, daemon=True, name="flask-admin").start()
    log.info("Admin web đang chạy trên port %s; bot_ready=%s", PORT, _polling_started)
    while True:
        time.sleep(3600)
