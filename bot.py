import os
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

# Vietnam Bank Codes Map for VietQR
BANK_CODE_MAP = {
    "MBBank": "970422", "MSBBank": "970426", "TCBank": "970407", "VPBank": "970432",
    "ACBank": "970425", "SHB": "970443", "EIB": "970431", "GPBANK": "970408",
    "TPBank": "970423", "VAB": "971005", "Techcombank": "970407", "AgriBank": "970405"
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
            used_by INTEGER, created_at TEXT NOT NULL, used_at TEXT, expires_at TEXT,
            locked INTEGER NOT NULL DEFAULT 0
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
        key_cols = [r[1] for r in c.execute("PRAGMA table_info(keys)").fetchall()]
        if "locked" not in key_cols:
            c.execute("ALTER TABLE keys ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
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
        return c.execute("""SELECT * FROM keys WHERE used_by=? AND locked=0
                          AND expires_at IS NOT NULL AND expires_at>?
                          ORDER BY expires_at DESC LIMIT 1""", (uid, iso(now()))).fetchone()

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

    def _source_spectral_density(self, data):
        """Phần spectral density được trích từ md5bot.py, chỉ nhận bytes MD5."""
        if len(data) < 16:
            return 0.0, 0.0
        n = len(data)
        harmonics = []
        import math
        for k in range(n // 2):
            real = sum(data[t] * math.cos(2 * math.pi * k * t / n) for t in range(n))
            imag = sum(data[t] * math.sin(2 * math.pi * k * t / n) for t in range(n))
            harmonics.append(real * real + imag * imag)
        if not harmonics:
            return 0.0, 0.0
        total = sum(harmonics) + 1e-9
        odd_power = sum(harmonics[i] for i in range(1, len(harmonics), 2))
        even_power = sum(harmonics[i] for i in range(0, len(harmonics), 2))
        tai = xiu = 0.0
        spectral_bias = (odd_power - even_power) / total
        if spectral_bias > 0.15:
            tai += 16.0
        elif spectral_bias < -0.15:
            xiu += 16.0
        centroid = sum(i * power for i, power in enumerate(harmonics)) / total
        if centroid > len(harmonics) / 2:
            tai += 10.0
        else:
            xiu += 10.0
        return tai, xiu

    def _source_cellular_rule30(self, data):
        """Rule 30 trong md5bot.py, giữ độc lập với các phần kết nối bên ngoài."""
        if len(data) < 16:
            return 0.0, 0.0
        bits = self._bits(data)
        state = list(bits)
        density_history = []
        for _ in range(8):
            state = [state[(i - 1) % len(state)] ^ (state[i] | state[(i + 1) % len(state)]) for i in range(len(state))]
            density_history.append(sum(state) / len(state))
        tai = xiu = 0.0
        avg_density = sum(density_history) / len(density_history)
        if avg_density > 0.52:
            tai += 18.0
        elif avg_density < 0.48:
            xiu += 18.0
        if density_history[-1] > density_history[0]:
            tai += 8.0
        else:
            xiu += 8.0
        return tai, xiu

    def _source_ultimate_md5_core(self, data):
        """Lõi ultimate_md5_core_v4 đã tách riêng khỏi các tính năng VIP khác."""
        if len(data) < 16:
            return 0.0, 0.0, []
        import math
        tai = xiu = 0.0
        details = []
        nibbles = [(b >> 4) & 0xF for b in data] + [b & 0xF for b in data]
        high_nib = sum(1 for nib in nibbles if nib >= 8)
        low_nib = len(nibbles) - high_nib
        if high_nib > low_nib * 1.15:
            tai += 15.0; details.append('v4-high-nibble→Tài')
        elif low_nib > high_nib * 1.15:
            xiu += 15.0; details.append('v4-low-nibble→Xỉu')
        even_nib = sum(1 for nib in nibbles if nib % 2 == 0)
        odd_nib = len(nibbles) - even_nib
        if even_nib > odd_nib * 1.2:
            xiu += 8.0
        elif odd_nib > even_nib * 1.2:
            tai += 8.0

        byte_counts = Counter(data)
        entropy = 0.0
        n = len(data)
        for count in byte_counts.values():
            p = count / n
            if p > 0:
                entropy -= p * math.log2(p)
        max_entropy = math.log2(min(256, n))
        entropy_ratio = entropy / max_entropy if max_entropy else 0.0
        if entropy_ratio > 0.96:
            if data[-1] >= 128: tai += 18.0
            else: xiu += 18.0
        elif entropy_ratio < 0.85:
            if sum(data) / n > 128: xiu += 18.0
            else: tai += 18.0

        bits = self._bits(data)
        ones = sum(bits); zeros = len(bits) - ones
        if ones > zeros + 10: xiu += 12.0
        elif zeros > ones + 10: tai += 12.0
        runs = []; run = 1
        for i in range(1, len(bits)):
            if bits[i] == bits[i - 1]: run += 1
            else: runs.append(run); run = 1
        runs.append(run)
        avg_run = sum(runs) / len(runs)
        if avg_run > 2.5: tai += 10.0
        elif avg_run < 1.8: xiu += 10.0

        transitions = Counter()
        for i in range(len(nibbles) - 2):
            pair = (nibbles[i] >= 8, nibbles[i + 1] >= 8)
            transitions[(pair, nibbles[i + 2] >= 8)] += 1
        last_pair = (nibbles[-2] >= 8, nibbles[-1] >= 8)
        if transitions[(last_pair, True)] > transitions[(last_pair, False)]:
            tai += 20.0
        elif transitions[(last_pair, False)] > transitions[(last_pair, True)]:
            xiu += 20.0

        spectral_tai, spectral_xiu = self._source_spectral_density(data)
        cellular_tai, cellular_xiu = self._source_cellular_rule30(data)
        tai += spectral_tai + cellular_tai
        xiu += spectral_xiu + cellular_xiu
        if tai > xiu + 5:
            details.append('v4-core→Tài')
        elif xiu > tai + 5:
            details.append('v4-core→Xỉu')
        return tai, xiu, details

    def analyze(self, value):
        raw = re.sub(r"\s+", "", value or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", raw):
            return {"ok": False, "error": "Mã phải là MD5 32 ký tự hoặc SHA-256 64 ký tự hệ hex."}
        data = bytes.fromhex(raw)
        bits = self._bits(data)
        score_tai = 50.0
        score_xiu = 50.0
        details = []

        # Bổ sung lõi dự đoán v4 từ md5bot.py; không thay đổi các luồng
        # key, nạp tiền, tài khoản hoặc quản trị của bot hiện tại.
        v4_tai, v4_xiu, v4_details = self._source_ultimate_md5_core(data)
        score_tai += v4_tai
        score_xiu += v4_xiu
        details.extend(v4_details)

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
    
    # Fixed: Allow 5 pending deposits instead of 3
    if pending >= 5:
        bot.send_message(uid, "⚠️ Bạn đang có tối đa 5 đơn chờ. Vui lòng chờ xử lý."); return
    
    # Check 5 minute cooldown for new orders (only if no pending orders exist)
    if pending == 0 and last:
        try:
            if now() - datetime.fromisoformat(last["created_at"]) < timedelta(minutes=5):
                bot.send_message(uid, "⏳ Vui lòng chờ 5 phút giữa các lần tạo đơn nạp."); return
        except ValueError: pass
    
    bank = get_setting("bank", DEFAULT_BANK)
    content = f"{bank['note_prefix']}{uid}"
    with db() as c:
        cur = c.execute("INSERT INTO deposits(telegram_id,amount,content,status,created_at) VALUES(?,?,?,?,?)", (uid, amount, content, "pending", iso(now())))
        did = cur.lastrowid
    
    # Fixed: Support Vietnamese bank codes for VietQR
    bank_code = bank['bank_code']
    vietqr_code = BANK_CODE_MAP.get(bank_code, bank_code)
    
    qr = f"https://img.vietqr.io/image/{vietqr_code}-{bank['account_no']}-compact2.png?amount={amount}&addInfo={content}&accountName={requests.utils.quote(bank['account_name'])}"
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
    # Added Telegram Premium icons: ⭐ for premium features
    text = (f"🔮 <b>PHÂN TÍCH MD5 TÀI/XỈU</b> ⭐\n\n"
            f"📦 Phiên bản: <b>Mới Nhất</b>\n"
            f"📝 MD5 hiện tại: <code>{short}</code>\n\n"
            f"🎲 Bộ số mô phỏng: <b>{nums[0]}-{nums[1]}-{nums[2]}</b> | Tổng: <b>{total}</b>\n"
            f"📉 Kết luận: <b>{verdict}</b>\n"
            f"🎯 Tài/Xỉu %: <b>T {out['tai']}% · X {out['xiu']}%</b>")
    bot.send_message(message.chat.id, text)
    # Giữ phiên chơi mở: user có thể gửi mã tiếp theo ngay lập tức.
    bot.register_next_step_handler_by_chat_id(message.chat.id, analyze_message)


def activate_key(message):
    if (message.text or "").strip().lower() in ("/start", "/menu"):
        try: bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception: pass
        register_user(message); welcome(message.chat.id); return
    register_user(message); uid = message.chat.id; key = (message.text or "").strip().upper()
    if not re.match(r"^TX-[A-Z0-9]{20}$", key):
        bot.send_message(uid, "❌ Định dạng key không hợp lệ. Key phải có dạng TX-XXXX...XXXX (20 ký tự hex).")
        return
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
        if not row:
            bot.send_message(uid, "❌ Key này không tồn tại hoặc đã xoá.")
        elif row["locked"]:
            bot.send_message(uid, "🔒 Key này đã bị khoá.")
        elif row["used_by"] and row["used_by"] != uid:
            bot.send_message(uid, "❌ Key này đã được sử dụng bởi người khác.")
        elif row["used_by"] == uid:
            bot.send_message(uid, "✅ Key này đã được kích hoạt trước đó cho tài khoản của bạn.")
        else:
            created = iso(now()); exp = iso(now() + timedelta(days=row["days"]))
            c.execute("UPDATE keys SET used_by=?, used_at=?, expires_at=? WHERE key=?", (uid, created, exp, key))
            bot.send_message(uid, f"✅ <b>KÍCH HOẠT KEY THÀNH CÔNG</b>\n\n🔑 Key: <code>{key}</code>\n📦 Gói: <b>{html.escape(row['package_name'])}</b>\n⏳ Hạn dùng: <code>{exp}</code>")


def confirm_deposit(uid, did, call):
    with db() as c:
        dep = c.execute("SELECT * FROM deposits WHERE id=? AND telegram_id=?", (did, uid)).fetchone()
    if not dep:
        bot.answer_callback_query(call.id, "❌ Không tìm thấy đơn nạp.", show_alert=True)
        return
    if dep["status"] != "pending":
        bot.answer_callback_query(call.id, f"⚠️ Đơn đã {dep['status']}.", show_alert=True)
        return
    text = f"💳 <b>ĐÃ CHUYỂN KHOẢN?</b>\n\n✅ Xác nhận bạn đã chuyển khoản {fmt_money(dep['amount'])} cho đơn #{did}.\n\n📝 Nội dung chuyển: <code>{dep['content']}</code>\n\n⏳ Admin sẽ xử lý trong vòng 5 phút."
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("✅ Đã gửi", callback_data="home"), types.InlineKeyboardButton("❌ Quay lại", callback_data="home"))
    edit_page(call, text, k)


def show_account(cid, call=None):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (cid,)).fetchone()
    balance = fmt_money(u["balance"] if u and "balance" in u.keys() else 0)
    active = user_key(cid)
    text = f"👤 <b>TÀI KHOẢN CỦA BẠN</b>\n\n🆔 Telegram ID: <code>{cid}</code>\n💰 Số dư: <b>{balance}</b>\n📦 Gói hiện tại: <b>{html.escape(active['package_name']) if active else 'Không có'}</b>"
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("💳 Nạp tiền", callback_data="deposit"), types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)


def redeem_giftcode(message):
    if (message.text or "").strip().lower() in ("/start", "/menu"):
        try: bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception: pass
        register_user(message); welcome(message.chat.id); return
    register_user(message); uid = message.chat.id; code = (message.text or "").strip().upper()
    with db() as c:
        gc = c.execute("SELECT * FROM giftcodes WHERE code=?", (code,)).fetchone()
        if not gc:
            bot.send_message(uid, "❌ Mã giftcode không hợp lệ hoặc đã xoá."); return
        if gc["used_count"] >= gc["max_uses"]:
            bot.send_message(uid, "❌ Mã này đã dùng hết."); return
        used = c.execute("SELECT 1 FROM giftcode_uses WHERE code=? AND telegram_id=?", (code, uid)).fetchone()
        if used:
            bot.send_message(uid, "❌ Bạn đã dùng mã này rồi."); return
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO giftcode_uses(code,telegram_id,used_at) VALUES(?,?,?)", (code, uid, iso(now())))
        c.execute("UPDATE giftcodes SET used_count=used_count+1 WHERE code=?", (code,))
        c.execute("UPDATE users SET balance=COALESCE(balance,0)+? WHERE telegram_id=?", (gc["amount"], uid))
        text = f"✅ <b>SỬ DỤNG GIFTCODE THÀNH CÔNG</b>\n\n💝 Nhận được: <b>{fmt_money(gc['amount'])}</b>\n💰 Số dư mới: <b>{fmt_money((c.execute('SELECT COALESCE(balance,0) b FROM users WHERE telegram_id=?', (uid,)).fetchone() or {}).get('b', 0) + gc['amount'])}</b>"
    bot.send_message(uid, text)


def admin_menu(cid, call=None):
    text = "🛠 <b>MENU QUẢN TRỊ</b>\n\nChọn chức năng bạn muốn quản lý:"
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("📊 Thống kê", callback_data="admin_stats"), types.InlineKeyboardButton("🔑 Tạo key", callback_data="admin_key"))
    k.add(types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"), types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)


def send_stats(cid, call=None):
    with db() as c:
        stats = {
            "users": c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
            "keys": c.execute("SELECT COUNT(*) n FROM keys").fetchone()["n"],
            "pending": c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"],
            "approved": c.execute("SELECT COUNT(*) n FROM deposits WHERE status='approved'").fetchone()["n"],
            "revenue": c.execute("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status='approved'").fetchone()["s"],
        }
    text = f"📊 <b>THỐNG KÊ HỆ THỐNG</b>\n\n👥 Tổng user: <b>{stats['users']}</b>\n🔑 Tổng key: <b>{stats['keys']}</b>\n⏳ Đơn chờ: <b>{stats['pending']}</b>\n✅ Đơn duyệt: <b>{stats['approved']}</b>\n💰 Tổng doanh thu: <b>{fmt_money(stats['revenue'])}</b>"
    k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Admin", callback_data="admin_menu"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)


def broadcast_next_step(message):
    if (message.text or "").strip().lower() in ("/start", "/menu"):
        try: bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception: pass
        register_user(message); welcome(message.chat.id); return
    register_user(message)
    text = message.text or ""
    with db() as c:
        users = c.execute("SELECT telegram_id FROM users").fetchall()
        c.execute("INSERT INTO broadcasts(text,sent_at) VALUES(?,?)", (text, iso(now())))
    count = 0
    for u in users:
        try:
            bot.send_message(u["telegram_id"], f"📢 <b>THÔNG BÁO TỪ ADMIN</b>\n\n{text}")
            count += 1
        except Exception:
            pass
    bot.send_message(message.chat.id, f"✅ Đã gửi thông báo tới <b>{count}/{len(users)}</b> user.")


def request_approve(uid, did, call):
    if not is_admin(uid):
        bot.answer_callback_query(call.id, "Bạn không phải admin.", show_alert=True)
        return
    text = f"⚠️ <b>XÁC NHẬN DUYỆT?</b>\n\nBạn sắp duyệt đơn #{did}.\n\nHành động này không thể hoàn tác."
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("✅ Duyệt", callback_data=f"approve_confirm:{did}"), types.InlineKeyboardButton("❌ Hủy", callback_data="home"))
    edit_page(call, text, k)


def request_reject(uid, did, call):
    if not is_admin(uid):
        bot.answer_callback_query(call.id, "Bạn không phải admin.", show_alert=True)
        return
    text = f"⚠️ <b>XÁC NHẬN TỪ CHỐI?</b>\n\nBạn sắp từ chối đơn #{did}.\n\nHành động này không thể hoàn tác."
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("❌ Từ chối", callback_data=f"reject_confirm:{did}"), types.InlineKeyboardButton("🏠 Hủy", callback_data="home"))
    edit_page(call, text, k)


def decide_deposit(uid, did, approved):
    if not is_admin(uid):
        return
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        dep = c.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if not dep or dep["status"] != "pending":
            return
        status = "approved" if approved else "rejected"
        c.execute("UPDATE deposits SET status=?, decided_at=?, decided_by=? WHERE id=?", (status, iso(now()), uid, did))
        if approved:
            c.execute("UPDATE users SET balance=COALESCE(balance,0)+? WHERE telegram_id=?", (dep["amount"], dep["telegram_id"]))
            text = f"✅ <b>ĐƠN #{did} ĐÃ DUYỆT</b>\n\n💰 Cộng: <b>{fmt_money(dep['amount'])}</b>"
        else:
            text = f"❌ <b>ĐƠN #{did} ĐÃ TỪ CHỐI</b>\n\n📝 Vui lòng thử lại hoặc liên hệ admin."
    try:
        bot.send_message(dep["telegram_id"], text)
    except Exception:
        pass


ADMIN_HTML = """
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ bot_name }} - Admin</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: #f5f5f5; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #333; margin-bottom: 20px; }
        h2 { color: #555; margin-top: 30px; margin-bottom: 15px; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }
        .stat-box { background: #f9f9f9; padding: 15px; border-left: 4px solid #007bff; border-radius: 4px; }
        .stat-box h3 { font-size: 14px; color: #666; margin-bottom: 5px; }
        .stat-box .value { font-size: 24px; font-weight: bold; color: #007bff; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; color: #333; font-weight: bold; }
        input[type="text"], input[type="number"], select, textarea { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        textarea { resize: vertical; min-height: 100px; }
        button { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
        button:hover { background: #0056b3; }
        .table-wrapper { overflow-x: auto; margin-bottom: 30px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { background: #007bff; color: white; padding: 10px; text-align: left; }
        td { padding: 10px; border-bottom: 1px solid #ddd; }
        tr:nth-child(even) { background: #f9f9f9; }
        .status-pending { color: #ff9800; font-weight: bold; }
        .status-approved { color: #4caf50; font-weight: bold; }
        .status-rejected { color: #f44336; font-weight: bold; }
        .code { background: #f0f0f0; padding: 2px 5px; border-radius: 3px; font-family: monospace; font-size: 12px; }
        .warning { background: #fff3cd; border: 1px solid #ffc107; padding: 10px; border-radius: 4px; margin-bottom: 15px; }
        .error { background: #f8d7da; border: 1px solid #f5c6cb; padding: 10px; border-radius: 4px; margin-bottom: 15px; }
        .success { background: #d4edda; border: 1px solid #c3e6cb; padding: 10px; border-radius: 4px; margin-bottom: 15px; }
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; border-bottom: 1px solid #ddd; }
        .tab { padding: 10px 15px; cursor: pointer; border: none; background: none; color: #666; font-weight: bold; }
        .tab.active { color: #007bff; border-bottom: 2px solid #007bff; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>⚙️ {{ bot_name }} - Quản Lý Admin</h1>
        
        {% if bot_ready %}
            <div class="success">✅ Bot đang hoạt động</div>
        {% else %}
            <div class="warning">⚠️ Bot chưa được cấu hình. Hãy nhập BOT_TOKEN bên dưới.</div>
        {% endif %}
        
        <div class="stats">
            <div class="stat-box">
                <h3>👥 Tổng User</h3>
                <div class="value">{{ stats.users }}</div>
            </div>
            <div class="stat-box">
                <h3>🔑 Key Chưa Dùng</h3>
                <div class="value">{{ stats.unused_keys }}</div>
            </div>
            <div class="stat-box">
                <h3>⏳ Đơn Chờ Xử Lý</h3>
                <div class="value">{{ stats.pending }}</div>
            </div>
            <div class="stat-box">
                <h3>💰 Tổng Doanh Thu</h3>
                <div class="value">{{ stats.revenue }}</div>
            </div>
        </div>
        
        <h2>⚙️ Cài Đặt Runtime</h2>
        <form method="POST" action="/admin/runtime">
            <div class="form-group">
                <label>🤖 BOT_TOKEN (từ BotFather)</label>
                <input type="text" name="bot_token" placeholder="123456789:ABCDEF-GHIJK...">
            </div>
            <div class="form-group">
                <label>👤 Admin IDs (cách nhau bằng dấu phẩy)</label>
                <input type="text" name="admin_ids" value="{{ admin_ids }}" placeholder="123456789, 987654321">
            </div>
            <button type="submit">💾 Lưu Cấu Hình</button>
        </form>
        
        <h2>🏦 Cấu Hình Ngân Hàng</h2>
        <form method="POST" action="/admin/bank">
            <div class="form-group">
                <label>Mã Ngân Hàng (VD: MSBBank, MBBank, TCBank)</label>
                <input type="text" name="bank_code" value="{{ bank.bank_code }}" placeholder="MSBBank">
            </div>
            <div class="form-group">
                <label>Số Tài Khoản</label>
                <input type="text" name="account_no" value="{{ bank.account_no }}" placeholder="0123456789">
            </div>
            <div class="form-group">
                <label>Tên Chủ Tài Khoản</label>
                <input type="text" name="account_name" value="{{ bank.account_name }}" placeholder="PHAM VAN A">
            </div>
            <div class="form-group">
                <label>Tiền Tố Nội Dung Chuyển</label>
                <input type="text" name="note_prefix" value="{{ bank.note_prefix }}" placeholder="NAPTX">
            </div>
            <div class="form-group">
                <label>Link Liên Hệ Admin (Telegram)</label>
                <input type="text" name="contact_link" value="{{ bank.contact_link }}" placeholder="https://t.me/username">
            </div>
            <button type="submit">💾 Lưu Ngân Hàng</button>
        </form>
        
        <h2>💎 Quản Lý Gói</h2>
        <form method="POST" action="/admin/packages">
            <div class="form-group">
                <label>JSON Gói (định dạng: {"Tên": {"price": 10000, "days": 30}})</label>
                <textarea name="packages">{{ packages_json }}</textarea>
            </div>
            <button type="submit">💾 Lưu Gói</button>
        </form>
        
        <h2>🔑 Tạo Key Mới</h2>
        <form method="POST" action="/admin/key">
            <div class="form-group">
                <label>Chọn Gói</label>
                <select name="package" required>
                    <option value="">-- Chọn gói --</option>
                    {% for pkg in packages_json %}{% endfor %}
                </select>
            </div>
            <button type="submit">🆕 Tạo Key</button>
        </form>
        
        {% if generated %}
            <div class="success">✅ Key mới: <span class="code">{{ generated }}</span></div>
        {% endif %}
        
        <h2>📋 Đơn Nạp Gần Đây</h2>
        <div class="table-wrapper">
            <table>
                <tr>
                    <th>#</th>
                    <th>User ID</th>
                    <th>Số Tiền</th>
                    <th>Nội Dung</th>
                    <th>Trạng Thái</th>
                    <th>Thời Gian</th>
                </tr>
                {% for dep in deposits[:20] %}
                <tr>
                    <td>{{ dep.id }}</td>
                    <td><span class="code">{{ dep.telegram_id }}</span></td>
                    <td>{{ dep.amount }}đ</td>
                    <td><span class="code">{{ dep.content }}</span></td>
                    <td class="status-{{ dep.status }}">{{ dep.status }}</td>
                    <td>{{ dep.created_at[:10] }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>
        
        <h2>👥 User Gần Đây</h2>
        <div class="table-wrapper">
            <table>
                <tr>
                    <th>ID</th>
                    <th>Username</th>
                    <th>Tên</th>
                    <th>Tham Gia</th>
                    <th>Lần Cuối</th>
                </tr>
                {% for user in users[:20] %}
                <tr>
                    <td><span class="code">{{ user.telegram_id }}</span></td>
                    <td>{{ user.username or '(chưa đặt)' }}</td>
                    <td>{{ user.first_name or '(chưa đặt)' }}</td>
                    <td>{{ user.created_at[:10] }}</td>
                    <td>{{ user.last_seen[:10] }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>
        
        <h2>🔑 Key Hoạt Động</h2>
        <div class="table-wrapper">
            <table>
                <tr>
                    <th>Key</th>
                    <th>Gói</th>
                    <th>User</th>
                    <th>Hạn Dùng</th>
                    <th>Trạng Thái</th>
                </tr>
                {% for key in keys[:30] %}
                <tr>
                    <td><span class="code">{{ key.key }}</span></td>
                    <td>{{ key.package_name }}</td>
                    <td>{{ key.username or key.first_name or (key.used_by if key.used_by else '(chưa dùng)') }}</td>
                    <td>{{ key.expires_at[:10] if key.expires_at else '(chưa kích hoạt)' }}</td>
                    <td>{{ '🔒 Khoá' if key.locked else ('✅ Hoạt' if key.used_by and key.expires_at > now_iso else '❌ Hết') }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>
    </div>
</body>
</html>
"""

@app.get("/")
def health(): return jsonify(ok=True, service=BOT_NAME, time=iso(now()))

@app.get("/admin")
def admin_page():
    with db() as c:
        deposits = c.execute("SELECT * FROM deposits ORDER BY id DESC LIMIT 100").fetchall()
        users = c.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 200").fetchall()
        keys = c.execute("""SELECT k.*, u.username, u.first_name FROM keys k
                          LEFT JOIN users u ON u.telegram_id=k.used_by
                          ORDER BY CASE WHEN k.used_by IS NOT NULL AND k.expires_at>? AND k.locked=0 THEN 0 ELSE 1 END,
                                   k.created_at DESC LIMIT 300""", (iso(now()),)).fetchall()
        stats = {
            "users": c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
            "unused_keys": c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NULL").fetchone()["n"],
            "pending": c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"],
            "revenue": fmt_money(c.execute("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status='approved'").fetchone()["s"]),
        }
    packages = get_setting("packages", DEFAULT_PACKAGES); bank = get_setting("bank", DEFAULT_BANK)
    token = str(get_setting("bot_token", BOT_TOKEN) or "")
    ids = get_setting("admin_ids", sorted(x for x in ADMIN_IDS if x != 0))
    return render_template_string(ADMIN_HTML, bot_name=BOT_NAME, bot_ready=bool(token), deposits=deposits, users=users, packages=packages, bank=bank, stats=stats, keys=keys, admin_ids=','.join(map(str, ids)), token_value="", packages_json=json.dumps(packages, ensure_ascii=False, indent=2), generated=request.args.get("generated", ""), now_iso=iso(now()))

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
    return redirect("/admin")

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

@app.post("/admin/key/toggle")
def admin_key_toggle():
    key = request.form.get("key", "").strip().upper()
    if not key:
        return "Key không hợp lệ", 400
    with db() as c:
        row = c.execute("SELECT locked FROM keys WHERE key=?", (key,)).fetchone()
        if not row:
            return "Không tìm thấy key", 404
        c.execute("UPDATE keys SET locked=? WHERE key=?", (0 if row["locked"] else 1, key))
    return redirect("/admin#keys")

@app.post("/admin/key/delete")
def admin_key_delete():
    key = request.form.get("key", "").strip().upper()
    if not key:
        return "Key không hợp lệ", 400
    with db() as c:
        c.execute("DELETE FROM keys WHERE key=?", (key,))
    return redirect("/admin#keys")


@app.get("/admin/stats")
def admin_stats_api():
    with db() as c:
        return jsonify(users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"], pending_deposits=c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"], unused_keys=c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NULL AND locked=0").fetchone()["n"], running_keys=c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NOT NULL AND locked=0 AND expires_at IS NOT NULL AND expires_at>?", (iso(now()),)).fetchone()["n"], locked_keys=c.execute("SELECT COUNT(*) n FROM keys WHERE locked=1").fetchone()["n"])


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
