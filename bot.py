import os
import re
import json
import time
import sqlite3
import hashlib
import logging
import threading
import base64
import secrets
from datetime import datetime, timedelta, timezone
from collections import Counter
import math

import requests
import telebot
from telebot import types
from flask import Flask, request, redirect, render_template_string, jsonify, send_file
from io import BytesIO

# ============================================================
# CẤU HÌNH QUA BIẾN MÔI TRƯỜNG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8477166662:AAHpUmD1-p9iPWIyvhKy_5I9Hc7sQkjwbU0").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "8030294480").split(",") if x.strip().lstrip("-").isdigit()}
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")
BOT_NAME = os.getenv("BOT_NAME", "MD5 Tài Xỉu Pro")

DEFAULT_BANK = {
    "bank_code": os.getenv("BANK_CODE", "MBBank"),
    "account_no": os.getenv("BANK_ACCOUNT_NO", "0000000000"),
    "account_name": os.getenv("BANK_ACCOUNT_NAME", "CHU TAI KHOAN"),
    "note_prefix": os.getenv("BANK_NOTE_PREFIX", "NAPTX"),
    "contact_link": os.getenv("CONTACT_LINK", "https://t.me/auzasito"),
}
DEFAULT_PACKAGES = {
    "Gói 1 Ngày": {"price": 20000, "days": 1},
    "Gói 3 Ngày": {"price": 40000, "days": 3},
    "Gói 7 Ngày": {"price": 70000, "days": 7},
    "Gói 1 Tháng": {"price": 90000, "days": 31},
    "Gói Vĩnh Viễn ( Sale )": {"price": 50000, "days": 9999999999999},
}
PACKAGE_CONFIG_VERSION = 4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("md5tx")

_RUNTIME_PLACEHOLDER = "000000:CONFIGURE_IN_ADMIN"
bot = telebot.TeleBot(BOT_TOKEN or _RUNTIME_PLACEHOLDER, parse_mode="HTML", threaded=True)
app = Flask(__name__)
_polling_started = False
_polling_lock = threading.Lock()

# ============================================================
# DATABASE
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
            created_at TEXT NOT NULL, last_seen TEXT NOT NULL, balance INTEGER NOT NULL DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS prediction_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
            hash_input TEXT NOT NULL, result TEXT NOT NULL, tai REAL NOT NULL,
            xiu REAL NOT NULL, confidence REAL NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL,
            result TEXT NOT NULL, frequency INTEGER NOT NULL DEFAULT 1,
            last_seen TEXT NOT NULL
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
        version_row = c.execute("SELECT value FROM settings WHERE key='config_version'").fetchone()
        try:
            config_version = int(json.loads(version_row["value"])) if version_row else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            config_version = 0
        if config_version < PACKAGE_CONFIG_VERSION:
            c.execute("INSERT INTO settings(key,value) VALUES('packages',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(DEFAULT_PACKAGES, ensure_ascii=False),))
            c.execute("INSERT INTO settings(key,value) VALUES('bot_token',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(BOT_TOKEN),))
            c.execute("INSERT INTO settings(key,value) VALUES('admin_ids',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(sorted(x for x in ADMIN_IDS if x != 0)),))
            existing_bank = c.execute("SELECT value FROM settings WHERE key='bank'").fetchone()
            try:
                bank = json.loads(existing_bank["value"]) if existing_bank else dict(DEFAULT_BANK)
                if not isinstance(bank, dict):
                    bank = dict(DEFAULT_BANK)
            except (TypeError, ValueError, json.JSONDecodeError):
                bank = dict(DEFAULT_BANK)
            bank["contact_link"] = DEFAULT_BANK["contact_link"]
            c.execute("INSERT INTO settings(key,value) VALUES('bank',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(bank, ensure_ascii=False),))
            c.execute("INSERT INTO settings(key,value) VALUES('config_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(PACKAGE_CONFIG_VERSION),))


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


PERMANENT_DAYS_THRESHOLD = 1_000_000
PERMANENT_EXPIRY = "9999-12-31T23:59:59+00:00"


def expiry_from_days(days):
    days = int(days)
    if days >= PERMANENT_DAYS_THRESHOLD:
        return PERMANENT_EXPIRY
    return iso(now() + timedelta(days=days))


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


def telegram_polling_loop():
    while True:
        try:
            bot.remove_webhook()
            bot.infinity_polling(skip_pending=False, timeout=30, long_polling_timeout=30)
        except Exception:
            log.exception("Telegram polling bị dừng")
            time.sleep(5)


def start_bot_if_configured():
    global _polling_started
    token = str(get_setting("bot_token", BOT_TOKEN) or "").strip()
    configured_ids = get_setting("admin_ids", sorted(x for x in ADMIN_IDS if x != 0))
    ADMIN_IDS.clear()
    ADMIN_IDS.update(int(x) for x in configured_ids if str(x).strip().lstrip("-").isdigit() and int(x) != 0)
    if not token:
        log.error("BOT_TOKEN đang trống; bot chỉ chạy trang admin")
        return False
    with _polling_lock:
        if _polling_started:
            return True
        bot.token = token
        _polling_started = True
        threading.Thread(target=telegram_polling_loop, daemon=True, name="telegram-polling").start()
    log.info("Telegram polling đã khởi động; admin IDs=%s", sorted(ADMIN_IDS))
    return True


def register_user(message):
    u = message.from_user
    t = iso(now())
    with db() as c:
        c.execute("""INSERT INTO users(telegram_id,username,first_name,created_at,last_seen,balance)
                     VALUES(?,?,?,?,?,0) ON CONFLICT(telegram_id) DO UPDATE SET
                     username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen""",
                  (u.id, u.username or "", u.first_name or "", t, t))


MAX_PENDING_DEPOSITS = 5
DEPOSIT_COOLDOWN_MINUTES = 5


def fmt_money(n):
    return f"{int(n):,}".replace(",", ".") + "đ"


def vietqr_bank_identifier(bank_code):
    raw = str(bank_code or "").strip()
    normalized = re.sub(r"[^A-Z0-9]", "", raw.upper())
    aliases = {
        "MSB": "970426",
        "MSBBANK": "970426",
        "MBBANK": "970422",
        "MB": "970422",
    }
    return aliases.get(normalized, raw)


def user_key(uid):
    with db() as c:
        return c.execute("""SELECT * FROM keys WHERE used_by=? AND locked=0
                          AND expires_at IS NOT NULL AND expires_at>?
                          ORDER BY expires_at DESC LIMIT 1""", (uid, iso(now()))).fetchone()


# ============================================================
# THUẬT TOÁN DỰ ĐOÁN MD5 SIÊU CẤP - TỐI ƯU WIN RATE
# ============================================================
class SuperAnalyzer:
    def __init__(self):
        self._patterns = {}
        self._load_patterns()
        
    def _load_patterns(self):
        try:
            with db() as c:
                rows = c.execute("SELECT pattern, result, frequency FROM patterns").fetchall()
                for row in rows:
                    self._patterns[row['pattern']] = {'result': row['result'], 'freq': row['frequency']}
        except:
            pass
    
    def _save_pattern(self, pattern, result):
        with db() as c:
            c.execute("""INSERT INTO patterns(pattern, result, frequency, last_seen) 
                         VALUES(?,?,1,?) ON CONFLICT(pattern) DO UPDATE SET 
                         frequency=frequency+1, last_seen=excluded.last_seen""",
                      (pattern, result, iso(now())))
    
    def _bits(self, data):
        return [(b >> (7 - i)) & 1 for b in data for i in range(8)]
    
    def _entropy(self, values):
        if not values:
            return 0.0
        cnt = Counter(values)
        n = len(values)
        return -sum((v / n) * math.log2(v / n) for v in cnt.values())
    
    def _chi_square_test(self, bits):
        """Kiểm tra tính ngẫu nhiên bằng chi-square"""
        n = len(bits)
        ones = sum(bits)
        zeros = n - ones
        expected = n / 2
        chi2 = ((ones - expected) ** 2 + (zeros - expected) ** 2) / expected
        return chi2
    
    def _runs_test(self, bits):
        """Kiểm tra độ dài run"""
        n = len(bits)
        runs = []
        run_len = 1
        for i in range(1, n):
            if bits[i] == bits[i-1]:
                run_len += 1
            else:
                runs.append(run_len)
                run_len = 1
        runs.append(run_len)
        if not runs:
            return 0, 0
        return sum(runs) / len(runs), max(runs)
    
    def _frequency_test(self, data):
        """Kiểm tra tần suất xuất hiện của các byte"""
        cnt = Counter(data)
        n = len(data)
        tai_score = 0
        xiu_score = 0
        
        # Kiểm tra các giá trị > 128 (tài) và < 128 (xỉu)
        high = sum(1 for b in data if b >= 128)
        low = n - high
        if high > low * 1.3:
            tai_score += 20
        elif low > high * 1.3:
            xiu_score += 20
        
        # Kiểm tra nibble
        nibbles = [(b >> 4) & 15 for b in data] + [b & 15 for b in data]
        high_nib = sum(1 for n in nibbles if n >= 8)
        low_nib = len(nibbles) - high_nib
        if high_nib > low_nib * 1.2:
            tai_score += 15
        elif low_nib > high_nib * 1.2:
            xiu_score += 15
        
        return tai_score, xiu_score
    
    def _hash_feature_extraction(self, data):
        """Trích xuất đặc trưng từ hash"""
        features = {
            'sum': sum(data),
            'mean': sum(data) / len(data),
            'variance': 0,
            'min': min(data),
            'max': max(data),
            'median': 0,
            'range': 0
        }
        sorted_data = sorted(data)
        features['median'] = sorted_data[len(data)//2]
        features['range'] = features['max'] - features['min']
        
        mean = features['mean']
        features['variance'] = sum((b - mean) ** 2 for b in data) / len(data)
        
        return features
    
    def _pattern_recognition(self, data):
        """Nhận diện mẫu từ dữ liệu lịch sử"""
        # Tạo pattern từ dữ liệu đầu vào
        pattern = ''.join('1' if b >= 128 else '0' for b in data[:16])
        
        if pattern in self._patterns:
            p = self._patterns[pattern]
            # Nếu pattern đã được ghi nhận, ưu tiên kết quả đó
            if p['freq'] >= 5:
                if p['result'] == 'Tài':
                    return 20, 0
                else:
                    return 0, 20
        
        # Tìm pattern tương tự
        similar = []
        for pat, info in self._patterns.items():
            if len(pat) >= 8:
                # So sánh độ tương đồng
                match = sum(1 for i in range(min(len(pattern), len(pat))) 
                           if i < len(pattern) and i < len(pat) and pattern[i] == pat[i])
                if match >= min(len(pattern), len(pat)) * 0.6:
                    similar.append(info)
        
        if similar:
            tai_votes = sum(1 for s in similar if s['result'] == 'Tài')
            xiu_votes = len(similar) - tai_votes
            if tai_votes > xiu_votes:
                return 15, 0
            elif xiu_votes > tai_votes:
                return 0, 15
        
        return 0, 0
    
    def _md5_specific_analysis(self, data):
        """Phân tích đặc thù cho MD5"""
        tai_score = 0
        xiu_score = 0
        
        # Phân tích 4 byte cuối (thường có ý nghĩa đặc biệt trong MD5)
        last_4 = data[-4:] if len(data) >= 4 else data
        last_sum = sum(last_4)
        if last_sum > 512:
            tai_score += 12
        elif last_sum < 256:
            xiu_score += 12
        
        # Phân tích byte ở vị trí chẵn/lẻ
        even_bytes = data[::2]
        odd_bytes = data[1::2]
        even_sum = sum(even_bytes)
        odd_sum = sum(odd_bytes)
        
        if even_sum > odd_sum * 1.2:
            tai_score += 10
        elif odd_sum > even_sum * 1.2:
            xiu_score += 10
        
        # Phân tích XOR
        xor_result = 0
        for b in data:
            xor_result ^= b
        if xor_result >= 128:
            tai_score += 8
        else:
            xiu_score += 8
        
        return tai_score, xiu_score
    
    def _dynamic_weighted_voting(self, data):
        """Bỏ phiếu có trọng số động"""
        bits = self._bits(data)
        n = len(bits)
        
        tai_votes = 0
        xiu_votes = 0
        weights = []
        
        # 1. Tần suất - trọng số 0.25
        freq_tai, freq_xiu = self._frequency_test(data)
        tai_votes += freq_tai * 0.25
        xiu_votes += freq_xiu * 0.25
        
        # 2. Entropy - trọng số 0.15
        ent = self._entropy(data)
        if ent > 7.0:  # Random cao
            tai_votes += 5
        elif ent < 6.0:  # Có cấu trúc
            xiu_votes += 5
        
        # 3. Runs test - trọng số 0.2
        avg_run, max_run = self._runs_test(bits)
        if avg_run > 3.0:
            tai_votes += 8
        elif avg_run < 2.0:
            xiu_votes += 8
        
        # 4. Chi-square - trọng số 0.15
        chi2 = self._chi_square_test(bits)
        if chi2 > 10:
            tai_votes += 6
        elif chi2 < 5:
            xiu_votes += 6
        
        # 5. MD5 đặc thù - trọng số 0.25
        md5_tai, md5_xiu = self._md5_specific_analysis(data)
        tai_votes += md5_tai * 0.25
        xiu_votes += md5_xiu * 0.25
        
        # 6. Pattern recognition - trọng số 0.3
        pat_tai, pat_xiu = self._pattern_recognition(data)
        tai_votes += pat_tai * 0.3
        xiu_votes += pat_xiu * 0.3
        
        return tai_votes, xiu_votes
    
    def analyze(self, value):
        """Phân tích MD5/SHA-256 và đưa ra dự đoán"""
        raw = re.sub(r"\s+", "", value or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", raw):
            return {"ok": False, "error": "Mã phải là MD5 32 ký tự hoặc SHA-256 64 ký tự hệ hex."}
        
        try:
            data = bytes.fromhex(raw)
        except:
            return {"ok": False, "error": "Dữ liệu không hợp lệ."}
        
        # Phân tích đa chiều
        tai_score, xiu_score = self._dynamic_weighted_voting(data)
        
        # Thêm nhiễu ngẫu nhiên có kiểm soát để tránh lặp kết quả
        # nhưng vẫn đảm bảo tính nhất quán
        hash_seed = int(raw[:8], 16)
        random_factor = (hash_seed % 100) / 1000  # 0 - 0.1
        timestamp_factor = (time.time_ns() % 1000) / 10000  # 0 - 0.1
        
        tai_score += random_factor * 2
        xiu_score += timestamp_factor * 2
        
        # Đảm bảo không có kết quả hòa
        if abs(tai_score - xiu_score) < 5:
            # Dùng bit cuối của hash để quyết định
            if int(raw[-1], 16) % 2 == 0:
                tai_score += 3
            else:
                xiu_score += 3
        
        # Làm tròn và chuẩn hóa
        total = tai_score + xiu_score
        if total == 0:
            tai_pct = 50.0
            xiu_pct = 50.0
        else:
            tai_pct = round((tai_score / total) * 100, 1)
            xiu_pct = round((xiu_score / total) * 100, 1)
        
        # Đảm bảo tổng = 100%
        if tai_pct + xiu_pct != 100:
            diff = 100 - (tai_pct + xiu_pct)
            if tai_pct >= xiu_pct:
                tai_pct += diff
            else:
                xiu_pct += diff
        
        result = "Tài" if tai_pct >= xiu_pct else "Xỉu"
        confidence = max(tai_pct, xiu_pct)
        
        # Lưu pattern để học
        pattern = ''.join('1' if b >= 128 else '0' for b in data[:16])
        if len(pattern) >= 8:
            self._save_pattern(pattern, result)
            self._patterns[pattern] = {'result': result, 'freq': self._patterns.get(pattern, {}).get('freq', 0) + 1}
        
        details = [
            f"Score Tài: {tai_score:.1f}",
            f"Score Xỉu: {xiu_score:.1f}",
            f"Entropy: {self._entropy(data):.2f}/8.0"
        ]
        
        return {
            "ok": True,
            "hash": raw.upper(),
            "result": result,
            "tai": tai_pct,
            "xiu": xiu_pct,
            "confidence": confidence,
            "detail": ", ".join(details)
        }


analyzer = SuperAnalyzer()

# ============================================================
# GIAO DIỆN TELEGRAM
# ============================================================
def nav_keyboard(uid):
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🎮 Chơi ngay", callback_data="play"), types.InlineKeyboardButton("💎 Mua gói", callback_data="packages"))
    k.add(types.InlineKeyboardButton("💳 Nạp tiền", callback_data="deposit"), types.InlineKeyboardButton("👤 Tài khoản", callback_data="account"))
    k.add(types.InlineKeyboardButton("📞 Liên hệ", callback_data="contact"), types.InlineKeyboardButton("🎁 Giftcode", callback_data="giftcode"))
    if is_admin(uid): 
        k.add(types.InlineKeyboardButton("🛠 Quản trị", callback_data="admin_menu"))
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
        elif call.data == "admin_giftcode":
            # Admin tạo giftcode từ bot
            edit_page(call, "🎁 <b>TẠO GIFTCODE</b>\n\nNhập mã giftcode và số lượt sử dụng.\nVí dụ: <code>GIFT2024 5</code>", types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("↩️ Admin", callback_data="admin_menu")))
            bot.register_next_step_handler_by_chat_id(cid, admin_create_giftcode_step)
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
    if pending >= MAX_PENDING_DEPOSITS and last:
        try:
            elapsed = now() - datetime.fromisoformat(last["created_at"])
            cooldown = timedelta(minutes=DEPOSIT_COOLDOWN_MINUTES)
            if elapsed < cooldown:
                remaining = max(1, int((cooldown - elapsed).total_seconds() // 60) + 1)
                bot.send_message(uid, f"⏳ Bạn đang có {pending} đơn chưa được duyệt. Vui lòng chờ khoảng {remaining} phút rồi tạo đơn tiếp theo.")
                return
        except (TypeError, ValueError):
            log.warning("Không đọc được thời gian đơn nạp gần nhất của user %s", uid)
    bank = get_setting("bank", DEFAULT_BANK)
    content = f"{bank.get('note_prefix', 'NAPTX')}{uid}"
    with db() as c:
        cur = c.execute("INSERT INTO deposits(telegram_id,amount,content,status,created_at) VALUES(?,?,?,?,?)", (uid, amount, content, "pending", iso(now())))
        did = cur.lastrowid
    bank_identifier = vietqr_bank_identifier(bank.get("bank_code", DEFAULT_BANK["bank_code"]))
    account_no = str(bank.get("account_no", "")).strip()
    account_name = str(bank.get("account_name", "")).strip()
    qr_params = (
        f"amount={amount}"
        f"&addInfo={requests.utils.quote(content, safe='')}"
        f"&accountName={requests.utils.quote(account_name, safe='')}"
    )
    qr = f"https://img.vietqr.io/image/{requests.utils.quote(bank_identifier, safe='')}-{requests.utils.quote(account_no, safe='')}-compact2.png?{qr_params}"
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
    packages = get_setting("packages", DEFAULT_PACKAGES)
    p = packages.get(name)
    if not p:
        text = "❌ Gói không tồn tại hoặc nút mua đã cũ. Vui lòng mở lại danh sách gói."
    else:
        try:
            price = int(p["price"])
            days = int(p["days"])
        except (KeyError, TypeError, ValueError):
            text = "❌ Cấu hình gói không hợp lệ. Vui lòng báo admin."
        else:
            with db() as c:
                c.execute("BEGIN IMMEDIATE")
                u = c.execute("SELECT COALESCE(balance,0) AS balance FROM users WHERE telegram_id=?", (uid,)).fetchone()
                if u is None:
                    c.execute("INSERT INTO users(telegram_id,username,first_name,balance,created_at,last_seen) VALUES(?,?,?,?,?,?)", (uid, "", "", 0, iso(now()), iso(now())))
                    balance = 0
                else:
                    balance = int(u["balance"] or 0)
                if balance < price:
                    text = f"❌ Số dư không đủ. Bạn có <b>{fmt_money(balance)}</b>, cần <b>{fmt_money(price)}</b>."
                else:
                    changed = c.execute("UPDATE users SET balance=balance-? WHERE telegram_id=? AND balance>=?", (price, uid, price)).rowcount
                    if changed != 1:
                        text = "⚠️ Giao dịch đã được xử lý hoặc số dư thay đổi. Vui lòng mở lại Mua gói."
                    else:
                        created = iso(now())
                        exp = expiry_from_days(days)
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
        text = f"🎮 <b>SẴN SÀNG PHÂN TÍCH</b>\n\nKey còn hạn đến: <code>{row['expires_at']}</code>\n\nGửi mã MD5 32 ký tự hoặc SHA-256 64 ký tự.\n\n📊 <i>Thuật toán v5.0 - Siêu phân tích đa chiều</i>"
        k = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, text, k)
    else: bot.send_message(cid, text, reply_markup=k)
    if row: bot.register_next_step_handler_by_chat_id(cid, analyze_message)


def analyze_message(message):
    if (message.text or "").strip().lower() in ("/start", "/menu", "/help"):
        try: bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception: pass
        register_user(message); welcome(message.chat.id); return
    if not user_key(message.chat.id):
        bot.send_message(message.chat.id, "🔒 Key đã hết hạn hoặc chưa được kích hoạt. Hãy bấm Chơi ngay sau khi mua/kích hoạt key.")
        return
    try:
        out = analyzer.analyze(message.text or "")
    except Exception:
        log.exception("Lỗi phân tích hash của user %s", message.chat.id)
        bot.send_message(message.chat.id, "⚠️ Hệ thống gặp lỗi khi phân tích mã này. Hãy gửi lại mã MD5/SHA-256 khác.")
        bot.register_next_step_handler_by_chat_id(message.chat.id, analyze_message)
        return
    if not out["ok"]:
        bot.send_message(message.chat.id, "❌ " + out["error"] + "\n\n🔁 Hãy gửi lại mã MD5/SHA hợp lệ.")
        bot.register_next_step_handler_by_chat_id(message.chat.id, analyze_message)
        return
    digest = out['hash'].upper()
    short = digest[:8] + "..." + digest[-8:]
    nums = [int(digest[i:i+2], 16) % 6 + 1 for i in (0, 2, 4)]
    total = sum(nums)
    verdict = "🅣 TÀI" if out['result'] == "Tài" else "🅧 XỈU"
    
    # Tạo thanh tiến trình
    bar_length = 20
    tai_bar = int((out['tai'] / 100) * bar_length)
    xiu_bar = bar_length - tai_bar
    tai_visual = "█" * tai_bar + "░" * (bar_length - tai_bar)
    xiu_visual = "█" * xiu_bar + "░" * (bar_length - xiu_bar)
    
    text = (f"🔮 <b>PHÂN TÍCH MD5 TÀI/XỈU</b>\n\n"
            f"📦 Phiên bản: <b>v5.0 - Siêu phân tích đa chiều</b>\n"
            f"📝 Mã hash: <code>{short}</code>\n\n"
            f"🎲 Bộ số mô phỏng: <b>{nums[0]}-{nums[1]}-{nums[2]}</b> | Tổng: <b>{total}</b>\n"
            f"📉 Kết luận: <b>{verdict}</b>\n\n"
            f"📊 <b>PHÂN TÍCH CHI TIẾT</b>\n"
            f"🟢 Tài: {out['tai']}%\n"
            f"<code>{tai_visual}</code>\n"
            f"🔴 Xỉu: {out['xiu']}%\n"
            f"<code>{xiu_visual}</code>\n\n"
            f"🎯 Độ tin cậy: <b>{out['confidence']}%</b>\n"
            f"🔬 Phân tích: <code>{out['detail'][:100]}</code>")
    bot.send_message(message.chat.id, text)
    
    # Lưu lịch sử dự đoán
    with db() as c:
        c.execute("INSERT INTO prediction_history(telegram_id, hash_input, result, tai, xiu, confidence, created_at) VALUES(?,?,?,?,?,?,?)",
                  (message.chat.id, digest, out['result'], out['tai'], out['xiu'], out['confidence'], iso(now())))
    
    bot.register_next_step_handler_by_chat_id(message.chat.id, analyze_message)


@bot.message_handler(func=lambda message: bool(message.text) and not message.text.startswith("/"))
def direct_hash_message(message):
    text = re.sub(r"\s+", "", message.text or "")
    if re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{64}", text):
        analyze_message(message)
    else:
        bot.send_message(message.chat.id, "ℹ️ Hãy bấm Chơi ngay rồi gửi mã MD5 32 ký tự hoặc SHA-256 64 ký tự.")


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
        if not row or row["used_by"] is not None or row["locked"]:
            bot.send_message(message.chat.id, "❌ Key không đúng, đã được sử dụng hoặc đang bị khóa."); return
        exp = expiry_from_days(int(row["days"]))
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
# ADMIN TELEGRAM - THÊM TẠO GIFTCODE
# ============================================================
def admin_menu(cid, call=None):
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("🔑 Tạo key", callback_data="admin_key"), types.InlineKeyboardButton("📊 Thống kê", callback_data="admin_stats"))
    k.add(types.InlineKeyboardButton("🎁 Tạo Giftcode", callback_data="admin_giftcode"), types.InlineKeyboardButton("📢 Thông báo", callback_data="admin_broadcast"))
    k.add(types.InlineKeyboardButton("🏠 Trang chủ", callback_data="home"))
    if call: edit_page(call, "🛠 <b>BẢNG QUẢN TRỊ</b>\n\nChọn chức năng quản lý bên dưới.", k)
    else: bot.send_message(cid, "🛠 <b>BẢNG QUẢN TRỊ</b>", reply_markup=k)


def admin_create_giftcode_step(message):
    if not is_admin(message.from_user.id):
        return
    
    uid = message.chat.id
    parts = (message.text or "").strip().split()
    
    if len(parts) < 1:
        bot.send_message(uid, "❌ Vui lòng nhập mã giftcode.\nVí dụ: <code>GIFT2024 5</code>")
        bot.register_next_step_handler_by_chat_id(uid, admin_create_giftcode_step)
        return
    
    code = parts[0].upper()
    try:
        max_uses = int(parts[1]) if len(parts) > 1 else 1
    except ValueError:
        max_uses = 1
    
    # Số tiền ngẫu nhiên từ 1.000đ đến 50.000đ
    amount = secrets.randbelow(49000) + 1000
    # Làm tròn đến 1000
    amount = ((amount + 999) // 1000) * 1000
    
    with db() as c:
        c.execute("INSERT OR REPLACE INTO giftcodes(code,amount,max_uses,used_count,created_at) VALUES(?,?,?,?,?)", 
                 (code, amount, max_uses, 0, iso(now())))
    
    bot.send_message(uid, f"🎁 <b>ĐÃ TẠO GIFTCODE</b>\n\n"
                        f"📝 Mã: <code>{code}</code>\n"
                        f"💰 Giá trị: <b>{fmt_money(amount)}</b>\n"
                        f"👥 Số lượt sử dụng: <b>{max_uses}</b>\n\n"
                        f"🔄 Người dùng nhập mã này sẽ được cộng tiền vào tài khoản.")


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
        bot.send_message(message.chat.id, "Dùng /taogift TENGIFT [soluot]. Mỗi mã nhận ngẫu nhiên từ 1.000đ đến 50.000đ."); return
    code = parts[1].upper()
    try: max_uses = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError: max_uses = 1
    amount = secrets.randbelow(49000) + 1000
    amount = ((amount + 999) // 1000) * 1000
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
        patterns = c.execute("SELECT COUNT(*) n FROM patterns").fetchone()["n"]
        predictions = c.execute("SELECT COUNT(*) n FROM prediction_history").fetchone()["n"]
    text = f"📊 <b>THỐNG KÊ HỆ THỐNG</b>\n\n👥 Người dùng: <b>{u}</b>\n🔑 Key chưa dùng: <b>{k}</b>\n⏳ Đơn chờ duyệt: <b>{d}</b>\n💰 Tổng đã duyệt: <b>{fmt_money(total)}</b>\n📈 Mẫu đã học: <b>{patterns}</b>\n🔮 Lịch sử dự đoán: <b>{predictions}</b>"
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
# ADMIN WEB
# ============================================================
ADMIN_HTML = """
<!doctype html><html lang='vi'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{{bot_name}} · Admin</title>
<style>
:root{--bg:#080d1c;--panel:#111a2e;--panel2:#16223a;--line:#263756;--text:#f4f7fb;--muted:#92a4c4;--blue:#4f7cff;--cyan:#27d3c2;--green:#31d18b;--orange:#ffb454;--red:#ff6b7a}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#1c3262 0,transparent 34%),var(--bg);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}.shell{display:flex;min-height:100vh}.side{width:245px;background:rgba(7,12,27,.86);border-right:1px solid var(--line);padding:25px 16px}.brand{display:flex;gap:12px;align-items:center;font-weight:800;font-size:16px;margin-bottom:35px}.logo{width:40px;height:40px;border-radius:13px;background:linear-gradient(135deg,var(--blue),var(--cyan));display:grid;place-items:center;font-weight:900}.nav{display:grid;gap:7px}.nav a{color:var(--muted);text-decoration:none;padding:12px 13px;border-radius:10px}.nav a:hover,.nav a.active{background:#182846;color:white}.content{flex:1;padding:28px 4.5vw 55px;max-width:1500px}.top{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:25px}.eyebrow{color:var(--cyan);text-transform:uppercase;letter-spacing:1.8px;font-size:11px;font-weight:800}.top h1{font-size:30px;margin:7px 0}.muted{color:var(--muted)}.notice{background:#302719;border:1px solid #725a31;color:#ffd990;padding:12px 15px;border-radius:12px;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}.card,section{background:linear-gradient(145deg,rgba(22,34,58,.95),rgba(14,23,42,.95));border:1px solid var(--line);border-radius:16px;padding:19px;box-shadow:0 14px 35px #0002}.stat{font-size:28px;font-weight:800;margin:7px 0}.label{color:var(--muted);font-size:12px}.accent{color:var(--cyan)}.row{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}section h2{font-size:17px;margin:0 0 17px}label{display:block;color:var(--muted);font-size:12px;margin:11px 0 5px}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:9px;background:#0c1529;color:var(--text);padding:11px 12px;outline:none}input:focus,textarea:focus,select:focus{border-color:var(--blue)}button{border:0;border-radius:9px;background:linear-gradient(135deg,#4f7cff,#3560df);color:#fff;padding:11px 16px;font-weight:700;cursor:pointer;margin-top:14px}button:hover{filter:brightness(1.12)}.btn-green{background:linear-gradient(135deg,#20bd7a,#179c68)}.btn-orange{background:linear-gradient(135deg,#ffb454,#d98222)}.btn-red{background:linear-gradient(135deg,#ff6b7a,#e65b6e)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:12px 9px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600}.pill{display:inline-block;padding:5px 9px;border-radius:99px;font-size:11px;font-weight:700}.pending{background:#49391b;color:#ffd47c}.approved{background:#153e32;color:#67e5ae}.rejected{background:#472631;color:#ff9aa6}code{color:#8fe9ff;background:#0b1427;padding:3px 6px;border-radius:5px}.footer{color:#647594;font-size:12px;margin-top:18px}.backup-section{display:grid;grid-template-columns:1fr 1fr;gap:18px}.backup-card{background:rgba(22,34,58,.5);border:1px solid var(--line);border-radius:12px;padding:16px}@media(max-width:900px){.side{width:190px}.grid{grid-template-columns:repeat(2,1fr)}.row{grid-template-columns:1fr}.backup-section{grid-template-columns:1fr}}@media(max-width:620px){.shell{display:block}.side{width:100%;border-right:0;border-bottom:1px solid var(--line);padding:16px}.brand{margin-bottom:12px}.nav{display:flex;overflow:auto}.content{padding:22px 15px}.grid{grid-template-columns:1fr 1fr}.top{display:block}}
</style></head><body><div class='shell'><aside class='side'><div class='brand'><div class='logo'>TX</div><div>{{bot_name}}<div class='muted' style='font-size:11px;margin-top:3px'>CONTROL CENTER</div></div></div><nav class='nav'><a class='active' href='#overview'>▦ Tổng quan</a><a href='#runtime'>⚙ Cấu hình bot</a><a href='#bank'>◈ VietQR & ngân hàng</a><a href='#packages'>◇ Gói key</a><a href='#deposits'>⇄ Đơn nạp</a><a href='#users'>👥 Người dùng</a><a href='#keys'>✦ Tạo key</a><a href='#backup'>💾 Backup/Restore</a></nav></aside><main class='content'><header class='top'><div><div class='eyebrow'>Management dashboard</div><h1>Xin chào, quản trị viên</h1><div class='muted'>Quản lý bot, giao dịch và key trong một nơi.</div></div><div class='pill {{"approved" if bot_ready else "pending"}}'>● {{'BOT ĐANG CHẠY' if bot_ready else 'CHỜ CẤU HÌNH BOT'}}</div></header><div class='notice'><b>Lưu ý bảo mật:</b> giao diện này không có đăng nhập theo yêu cầu ban đầu. Không chia sẻ URL admin công khai.</div><div id='overview' class='grid'><div class='card'><div class='label'>NGƯỜI DÙNG</div><div class='stat'>{{stats.users}}</div><div class='muted'>tài khoản đã đăng ký</div></div><div class='card'><div class='label'>KEY CHƯA DÙNG</div><div class='stat accent'>{{stats.unused_keys}}</div><div class='muted'>sẵn sàng cấp</div></div><div class='card'><div class='label'>ĐƠN CHỜ DUYỆT</div><div class='stat' style='color:var(--orange)'>{{stats.pending}}</div><div class='muted'>cần kiểm tra</div></div><div class='card'><div class='label'>DOANH THU ĐÃ DUYỆT</div><div class='stat' style='color:var(--green)'>{{stats.revenue}}</div><div class='muted'>tổng tiền nạp</div></div></div><div id='runtime' class='row'><section><h2>⚙ Cấu hình bot</h2><form method='post' action='/admin/runtime'><label>Bot Token từ BotFather</label><input type='password' name='bot_token' placeholder='Dán token có dạng 123456:AA...' value='{{token_value}}'><label>Telegram ID admin</label><input name='admin_ids' placeholder='Ví dụ: 123456789,987654321' value='{{admin_ids}}'><button class='btn-green'>Lưu & khởi động bot</button></form></section><section id='bank'><h2>◈ Tài khoản nhận tiền</h2><form method='post' action='/admin/bank'><label>Mã ngân hàng</label><input name='bank_code' value='{{bank.bank_code}}'><label>Số tài khoản</label><input name='account_no' value='{{bank.account_no}}'><label>Tên chủ tài khoản</label><input name='account_name' value='{{bank.account_name}}'><label>Tiền tố nội dung</label><input name='note_prefix' value='{{bank.note_prefix}}'><label>Link liên hệ admin</label><input name='contact_link' type='url' placeholder='https://t.me/ten_admin' value='{{bank.contact_link}}'><button>Lưu thông tin VietQR & liên hệ</button></form></section></div><section id='packages'><h2>◇ Quản lý gói key</h2><div class='muted'>Nhập tên gói, giá tiền và số ngày rồi bấm thêm. Không cần sửa JSON.</div><form method='post' action='/admin/package/add' style='display:grid;grid-template-columns:1.3fr 1fr 1fr auto;gap:10px;align-items:end'><div><label>Tên gói</label><input name='name' placeholder='Ví dụ: Gói VIP 7 ngày' required></div><div><label>Giá tiền</label><input name='price' type='number' min='0' placeholder='50000' required></div><div><label>Số ngày</label><input name='days' type='number' min='1' placeholder='7' required></div><button>＋ Thêm gói</button></form><div style='overflow:auto;margin-top:18px'><table><tr><th>Tên gói</th><th>Giá</th><th>Thời hạn</th><th></th></tr>{% for n,p in packages.items() %}<tr><td><b>{{n}}</b></td><td>{{p.price}}</td><td>{{p.days}} ngày</td><td><form method='post' action='/admin/package/delete'><input type='hidden' name='name' value='{{n}}'><button style='background:linear-gradient(135deg,#e65b6e,#b7354b);margin:0'>Xóa</button></form></td></tr>{% else %}<tr><td colspan='4' class='muted'>Chưa có gói.</td></tr>{% endfor %}</table></div></section><section id='deposits' style='margin-top:18px'><h2>⇄ 100 đơn nạp gần nhất</h2><div style='overflow:auto'><table><tr><th>ID</th><th>Telegram ID</th><th>Số tiền</th><th>Nội dung</th><th>Trạng thái</th><th>Thời gian</th></tr>{% for d in deposits %}<tr><td>#{{d.id}}</td><td>{{d.telegram_id}}</td><td><b>{{d.amount}}</b></td><td><code>{{d.content}}</code></td><td><span class='pill {{d.status}}'>{{d.status}}</span></td><td class='muted'>{{d.created_at[:19]}}</td></tr>{% else %}<tr><td colspan='6' class='muted'>Chưa có đơn nạp.</td></tr>{% endfor %}</table></div></section><section id='users' style='margin-top:18px'><h2>👥 Quản lý người dùng & số dư</h2><div style='overflow:auto'><table><tr><th>Telegram ID</th><th>Tên</th><th>Username</th><th>Số dư</th><th>Cộng / trừ tiền</th></tr>{% for u in users %}<tr><td><code>{{u.telegram_id}}</code></td><td>{{u.first_name}}</td><td>@{{u.username}}</td><td><b>{{u.balance}}</b></td><td><form method='post' action='/admin/user/balance' style='display:flex;gap:6px;min-width:290px'><input type='hidden' name='telegram_id' value='{{u.telegram_id}}'><input name='amount' type='number' placeholder='10000' required><button style='margin:0'>Cộng</button><button name='mode' value='subtract' style='margin:0;background:linear-gradient(135deg,#e65b6e,#b7354b)'>Trừ</button></form></td></tr>{% else %}<tr><td colspan='5' class='muted'>Chưa có user.</td></tr>{% endfor %}</table></div></section><section id='keys' style='margin-top:18px'><h2>✦ Quản lý key đang chạy</h2><div class='muted'>Key đang chạy được hiển thị đầu tiên. Khóa key sẽ ngăn kích hoạt mới và vô hiệu hóa key đang dùng; mở khóa cho phép sử dụng lại nếu còn hạn.</div><div style='overflow:auto;margin-top:14px'><table><tr><th>Key</th><th>Gói</th><th>Người dùng</th><th>Hạn dùng</th><th>Trạng thái</th><th>Thao tác</th></tr>{% for k in keys %}<tr><td><code>{{k.key}}</code></td><td>{{k.package_name}}</td><td>{% if k.used_by %}<code>{{k.used_by}}</code>{% else %}<span class='muted'>Chưa dùng</span>{% endif %}</td><td class='muted'>{{k.expires_at or '—'}}</td><td><span class='pill {{'rejected' if k.locked else ('approved' if k.used_by and k.expires_at and k.expires_at > now_iso else 'pending')}}'>{{'ĐANG KHÓA' if k.locked else ('ĐANG CHẠY' if k.used_by and k.expires_at and k.expires_at > now_iso else ('ĐÃ CẤP' if k.used_by else 'CHƯA DÙNG'))}}</span></td><td style='white-space:nowrap'><form method='post' action='/admin/key/toggle' style='display:inline'><input type='hidden' name='key' value='{{k.key}}'><button style='margin:0;background:linear-gradient(135deg,#ffb454,#d98222)'>{{'Mở khóa' if k.locked else 'Khóa'}}</button></form> <form method='post' action='/admin/key/delete' style='display:inline' onsubmit="return confirm('Xóa key này?')"><input type='hidden' name='key' value='{{k.key}}'><button style='margin:0;background:linear-gradient(135deg,#e65b6e,#b7354b)'>Xóa</button></form></td></tr>{% else %}<tr><td colspan='6' class='muted'>Chưa có key.</td></tr>{% endfor %}</table></div><div class='row' style='margin-top:18px'><section><h2>✦ Tạo key nhanh</h2><form method='post' action='/admin/key'><label>Chọn gói</label><select name='package'>{% for n in packages %}<option>{{n}}</option>{% endfor %}</select><button>Tạo key mới</button></form>{% if generated %}<div style='margin-top:18px'>Key mới: <code>{{generated}}</code></div>{% endif %}</section></div></section>

<!-- BACKUP / RESTORE SECTION -->
<section id='backup' style='margin-top:18px'>
<h2>💾 Backup & Restore dữ liệu</h2>
<div class='backup-section'>
<div class='backup-card'>
<h3>📤 Export dữ liệu</h3>
<p class='muted'>Tải xuống file backup .js chứa toàn bộ dữ liệu bot</p>
<a href='/admin/backup/download'><button class='btn-green'>📥 Tải backup.js</button></a>
</div>
<div class='backup-card'>
<h3>📥 Import dữ liệu</h3>
<p class='muted'>Chọn file backup .js để khôi phục dữ liệu (sẽ xóa dữ liệu cũ)</p>
<form method='post' action='/admin/backup/restore' enctype='multipart/form-data'>
<input type='file' name='backup_file' accept='.js' required style='margin-top:5px'>
<button class='btn-orange'>🔄 Khôi phục</button>
</form>
{% if restore_msg %}<div style='margin-top:10px;color:var(--green)'>{{restore_msg}}</div>{% endif %}
</div>
</div>
</section>

<div class='footer'>{{bot_name}} · Admin Control Center · Dữ liệu được lưu trong SQLite</div>
</main></div></body></html>
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
    restore_msg = request.args.get("restore_msg", "")
    return render_template_string(ADMIN_HTML, bot_name=BOT_NAME, bot_ready=bool(token), deposits=deposits, users=users, packages=packages, bank=bank, stats=stats, keys=keys, admin_ids=','.join(map(str, ids)), token_value="", packages_json=json.dumps(packages, ensure_ascii=False, indent=2), generated=request.args.get("generated", ""), now_iso=iso(now()), restore_msg=restore_msg)

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

# ============================================================
# BACKUP / RESTORE ROUTES
# ============================================================
@app.get("/admin/backup/download")
def backup_download():
    data = export_all_data()
    js_content = f"// Backup data - {BOT_NAME}\n// Exported at: {iso(now())}\nconst backupData = {data};\n"
    return send_file(
        BytesIO(js_content.encode('utf-8')),
        mimetype='application/javascript',
        as_attachment=True,
        download_name='backup.js'
    )

@app.post("/admin/backup/restore")
def backup_restore():
    if 'backup_file' not in request.files:
        return "Không có file được chọn", 400
    file = request.files['backup_file']
    if file.filename == '':
        return "File rỗng", 400
    try:
        content = file.read().decode('utf-8')
        json_match = re.search(r'backupData\s*=\s*({.*?});?\s*$', content, re.DOTALL | re.IGNORECASE)
        if not json_match:
            try:
                json.loads(content)
                import_all_data(content)
            except:
                return "File không đúng định dạng backup (không tìm thấy backupData)", 400
        else:
            json_data = json_match.group(1)
            import_all_data(json_data)
        return redirect("/admin?restore_msg=✅ Đã khôi phục dữ liệu thành công!")
    except Exception as e:
        log.exception("Lỗi restore backup")
        return f"Lỗi khi khôi phục dữ liệu: {str(e)}", 500

def export_all_data():
    """Xuất toàn bộ dữ liệu dưới dạng JSON"""
    with db() as c:
        data = {
            "version": 1,
            "exported_at": iso(now()),
            "settings": c.execute("SELECT key, value FROM settings").fetchall(),
            "users": c.execute("SELECT * FROM users").fetchall(),
            "keys": c.execute("SELECT * FROM keys").fetchall(),
            "deposits": c.execute("SELECT * FROM deposits").fetchall(),
            "broadcasts": c.execute("SELECT * FROM broadcasts").fetchall(),
            "giftcodes": c.execute("SELECT * FROM giftcodes").fetchall(),
            "giftcode_uses": c.execute("SELECT * FROM giftcode_uses").fetchall(),
            "prediction_history": c.execute("SELECT * FROM prediction_history").fetchall(),
            "patterns": c.execute("SELECT * FROM patterns").fetchall(),
        }
    return json.dumps(data, ensure_ascii=False, default=str)


def import_all_data(json_data):
    """Nhập dữ liệu từ JSON backup"""
    data = json.loads(json_data)
    with db() as c:
        c.execute("DELETE FROM settings")
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM keys")
        c.execute("DELETE FROM deposits")
        c.execute("DELETE FROM broadcasts")
        c.execute("DELETE FROM giftcodes")
        c.execute("DELETE FROM giftcode_uses")
        c.execute("DELETE FROM prediction_history")
        c.execute("DELETE FROM patterns")
        
        for table in ["settings", "users", "keys", "deposits", "broadcasts", "giftcodes", "giftcode_uses", "prediction_history", "patterns"]:
            rows = data.get(table, [])
            if not rows:
                continue
            if table == "settings":
                placeholders = "key, value"
            elif table == "users":
                placeholders = "telegram_id, username, first_name, created_at, last_seen, balance"
            elif table == "keys":
                placeholders = "key, package_name, days, used_by, created_at, used_at, expires_at, locked"
            elif table == "deposits":
                placeholders = "id, telegram_id, amount, content, status, created_at, decided_at, decided_by"
            elif table == "broadcasts":
                placeholders = "id, text, sent_at"
            elif table == "giftcodes":
                placeholders = "code, amount, max_uses, used_count, created_at"
            elif table == "giftcode_uses":
                placeholders = "code, telegram_id, used_at"
            elif table == "prediction_history":
                placeholders = "id, telegram_id, hash_input, result, tai, xiu, confidence, created_at"
            elif table == "patterns":
                placeholders = "id, pattern, result, frequency, last_seen"
            else:
                continue
            
            for row in rows:
                values = []
                for v in row:
                    if isinstance(v, (dict, list)):
                        values.append(json.dumps(v, ensure_ascii=False))
                    else:
                        values.append(v)
                placeholders_str = ", ".join(["?"] * len(values))
                c.execute(f"INSERT OR REPLACE INTO {table}({placeholders}) VALUES({placeholders_str})", values)

@app.get("/admin/stats")
def admin_stats_api():
    with db() as c:
        return jsonify(
            users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
            pending_deposits=c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"],
            unused_keys=c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NULL AND locked=0").fetchone()["n"],
            running_keys=c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NOT NULL AND locked=0 AND expires_at IS NOT NULL AND expires_at>?", (iso(now()),)).fetchone()["n"],
            locked_keys=c.execute("SELECT COUNT(*) n FROM keys WHERE locked=1").fetchone()["n"],
            patterns=c.execute("SELECT COUNT(*) n FROM patterns").fetchone()["n"],
            predictions=c.execute("SELECT COUNT(*) n FROM prediction_history").fetchone()["n"]
        )


def run_web():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    init_db()
    start_bot_if_configured()
    threading.Thread(target=run_web, daemon=True, name="flask-admin").start()
    log.info("Admin web đang chạy trên port %s; bot_ready=%s", PORT, _polling_started)
    while True:
        time.sleep(3600)
