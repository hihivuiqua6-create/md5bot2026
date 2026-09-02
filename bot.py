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
}
DEFAULT_PACKAGES = {
    "Gói 1 ngày": {"price": 10000, "days": 1},
    "Gói 7 ngày": {"price": 50000, "days": 7},
    "Gói 30 ngày": {"price": 150000, "days": 30},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("md5tx")

if not BOT_TOKEN:
    raise RuntimeError("Thiếu BOT_TOKEN. Hãy tạo biến môi trường BOT_TOKEN trên Render.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
app = Flask(__name__)

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
        """)
        if c.execute("SELECT 1 FROM settings WHERE key='bank'").fetchone() is None:
            c.execute("INSERT INTO settings(key,value) VALUES('bank',?)", (json.dumps(DEFAULT_BANK),))
        if c.execute("SELECT 1 FROM settings WHERE key='packages'").fetchone() is None:
            c.execute("INSERT INTO settings(key,value) VALUES('packages',?)", (json.dumps(DEFAULT_PACKAGES),))


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
# GIAO DIỆN TELEGRAM
# ============================================================
def main_keyboard(uid):
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("Chơi ngay", callback_data="play"),
          types.InlineKeyboardButton("Mua gói", callback_data="packages"))
    k.add(types.InlineKeyboardButton("Nạp tiền", callback_data="deposit"),
          types.InlineKeyboardButton("Tài khoản", callback_data="account"))
    if is_admin(uid):
        k.add(types.InlineKeyboardButton("Admin", callback_data="admin_menu"))
    return k


def welcome(chat_id):
    bot.send_message(chat_id, f"<b>Chào mừng đến {html.escape(BOT_NAME)}</b>\n\nChọn chức năng bên dưới. Hệ thống phân tích MD5/SHA theo công thức deterministic; kết quả chỉ mang tính tham khảo và không bảo đảm thắng.", reply_markup=main_keyboard(chat_id))


@bot.message_handler(commands=["start", "menu", "help"])
def start(message):
    register_user(message); welcome(message.chat.id)


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    uid, cid = call.from_user.id, call.message.chat.id
    try:
        if call.data == "home": welcome(cid)
        elif call.data == "packages": show_packages(cid)
        elif call.data == "deposit": ask_deposit(cid)
        elif call.data == "play": play(cid)
        elif call.data == "enter_key":
            bot.send_message(cid, "Gửi key cần kích hoạt.")
            bot.register_next_step_handler_by_chat_id(cid, activate_key)
        elif call.data.startswith("confirm_deposit:"):
            confirm_deposit(uid, int(call.data.split(":", 1)[1]))
        elif call.data == "account": show_account(cid)
        elif call.data == "admin_menu": admin_menu(cid) if is_admin(uid) else None
        elif call.data.startswith("buy:"): buy_package(cid, call.data.split(":", 1)[1])
        elif call.data.startswith("approve:"): decide_deposit(uid, int(call.data.split(":")[1]), True)
        elif call.data.startswith("reject:"): decide_deposit(uid, int(call.data.split(":")[1]), False)
        elif call.data == "admin_key": bot.send_message(cid, "Dùng lệnh <code>/taokey Tên_gói</code>, ví dụ <code>/taokey Gói_7_ngày</code>.")
        elif call.data == "admin_stats": send_stats(cid)
        elif call.data == "admin_broadcast": bot.send_message(cid, "Dùng lệnh <code>/thongbao nội dung</code> để gửi cho toàn bộ người dùng.")
        bot.answer_callback_query(call.id)
    except Exception as e:
        log.exception("callback error")
        bot.answer_callback_query(call.id, "Có lỗi, hãy thử lại.", show_alert=True)


def show_packages(cid):
    packages = get_setting("packages", DEFAULT_PACKAGES)
    k = types.InlineKeyboardMarkup(row_width=1)
    lines = ["<b>Các gói key</b>"]
    for name, p in packages.items():
        lines.append(f"• <b>{html.escape(name)}</b>: {fmt_money(p['price'])} / {p['days']} ngày")
        k.add(types.InlineKeyboardButton(f"Mua {name}", callback_data="buy:" + name[:50]))
    k.add(types.InlineKeyboardButton("Nạp tiền / tạo đơn", callback_data="deposit"), types.InlineKeyboardButton("Quay lại", callback_data="home"))
    bot.send_message(cid, "\n".join(lines), reply_markup=k)


def ask_deposit(cid):
    bot.send_message(cid, "Nhập số tiền muốn nạp, chỉ nhập số. Ví dụ: <code>50000</code>.\nSau đó hệ thống gửi QR VietQR và nội dung chuyển khoản.")
    bot.register_next_step_handler_by_chat_id(cid, create_deposit)


def create_deposit(message):
    register_user(message); uid = message.chat.id
    try: amount = int(re.sub(r"[^0-9]", "", message.text or ""))
    except ValueError: amount = 0
    if amount <= 0:
        bot.send_message(uid, "Số tiền không hợp lệ."); return
    with db() as c:
        pending = c.execute("SELECT COUNT(*) n FROM deposits WHERE telegram_id=? AND status='pending'", (uid,)).fetchone()["n"]
        last = c.execute("SELECT created_at FROM deposits WHERE telegram_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if pending >= 3:
        bot.send_message(uid, "Bạn đang có tối đa 3 đơn chờ. Sau đơn thứ 3, vui lòng chờ 5 phút rồi thử lại."); return
    if pending == 0 and last:
        try:
            if now() - datetime.fromisoformat(last["created_at"]) < timedelta(minutes=5):
                bot.send_message(uid, "Vui lòng chờ 5 phút giữa các lần tạo đơn nạp."); return
        except ValueError: pass
    bank = get_setting("bank", DEFAULT_BANK)
    content = f"{bank['note_prefix']}{uid}"
    created = iso(now())
    with db() as c:
        cur = c.execute("INSERT INTO deposits(telegram_id,amount,content,status,created_at) VALUES(?,?,?,?,?)", (uid, amount, content, "pending", created))
        did = cur.lastrowid
    qr = f"https://img.vietqr.io/image/{bank['bank_code']}-{bank['account_no']}-compact2.png?amount={amount}&addInfo={content}&accountName={requests.utils.quote(bank['account_name'])}"
    caption = f"<b>Đơn nạp #{did}</b>\nNgân hàng: <code>{html.escape(bank['bank_code'])}</code>\nSTK: <code>{html.escape(bank['account_no'])}</code>\nTên: <code>{html.escape(bank['account_name'])}</code>\nSố tiền: <b>{fmt_money(amount)}</b>\nNội dung: <code>{content}</code>\n\nSau khi chuyển khoản, bấm nút xác nhận."
    k = types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("Tôi đã nạp tiền", callback_data=f"confirm_deposit:{did}"))
    bot.send_photo(uid, qr, caption=caption, reply_markup=k)


def buy_package(cid, name):
    packages = get_setting("packages", DEFAULT_PACKAGES)
    if name not in packages:
        bot.send_message(cid, "Gói không còn tồn tại."); return
    p = packages[name]
    bot.send_message(cid, f"Bạn chọn <b>{html.escape(name)}</b> — {fmt_money(p['price'])}, hạn {p['days']} ngày.\nVui lòng nạp đúng số tiền, ghi đúng nội dung và báo admin duyệt.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Tạo đơn nạp", callback_data="deposit")))


def play(cid):
    row = user_key(cid)
    if not row:
        k = types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("Nhập key", callback_data="enter_key"), types.InlineKeyboardButton("Mua gói", callback_data="packages"))
        bot.send_message(cid, "Bạn chưa có key còn hạn. Hãy nhập key hoặc mua một gói.", reply_markup=k); return
    bot.send_message(cid, "Gửi mã MD5 32 ký tự hoặc SHA-256 64 ký tự để phân tích.")
    bot.register_next_step_handler_by_chat_id(cid, analyze_message)


def analyze_message(message):
    row = user_key(message.chat.id)
    if not row:
        bot.send_message(message.chat.id, "Key đã hết hạn hoặc chưa được kích hoạt."); return
    out = analyzer.analyze(message.text)
    if not out["ok"]:
        bot.send_message(message.chat.id, out["error"]); return
    bot.send_message(message.chat.id, f"<b>KẾT QUẢ PHÂN TÍCH</b>\nMD5/SHA: <code>{out['hash']}</code>\nDự đoán Tài: <b>{out['tai']}%</b>\nDự đoán Xỉu: <b>{out['xiu']}%</b>\nNên đánh: <b>{out['result']}</b>\nĐộ nghiêng mô hình: {out['confidence']}%\n\n<i>{html.escape(out['detail'])}</i>\n\n<blockquote>Cảnh báo: hash không thể bảo đảm dự đoán kết quả của trò chơi công bằng.</blockquote>")


def show_account(cid):
    row = user_key(cid)
    if row:
        bot.send_message(cid, f"Key: <code>{html.escape(row['key'])}</code>\nGói: {html.escape(row['package_name'])}\nHết hạn: {html.escape(row['expires_at'])}")
    else: bot.send_message(cid, "Bạn chưa có key còn hạn.")

# Nhập key bằng text sau khi bấm chơi hoặc gửi trực tiếp lệnh.
@bot.message_handler(commands=["nhapkey"])
def enter_key_cmd(message):
    bot.send_message(message.chat.id, "Gửi key cần kích hoạt."); bot.register_next_step_handler_by_chat_id(message.chat.id, activate_key)

@bot.callback_query_handler(func=lambda call: call.data == "enter_key")
def enter_key_callback(call):
    bot.send_message(call.message.chat.id, "Gửi key cần kích hoạt."); bot.register_next_step_handler_by_chat_id(call.message.chat.id, activate_key)


def activate_key(message):
    key = (message.text or "").strip().upper()
    with db() as c:
        row = c.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
        if not row or row["used_by"] is not None:
            bot.send_message(message.chat.id, "Key không đúng hoặc đã được sử dụng."); return
        exp = iso(now() + timedelta(days=int(row["days"])))
        c.execute("UPDATE keys SET used_by=?,used_at=?,expires_at=? WHERE key=?", (message.chat.id, iso(now()), exp, key))
    bot.send_message(message.chat.id, f"Kích hoạt thành công gói <b>{html.escape(row['package_name'])}</b>. Hạn đến {exp}")

# ============================================================
# ADMIN TELEGRAM
# ============================================================
def admin_menu(cid):
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("Tạo key", callback_data="admin_key"), types.InlineKeyboardButton("Thống kê", callback_data="admin_stats"))
    k.add(types.InlineKeyboardButton("Thông báo toàn bộ", callback_data="admin_broadcast"))
    bot.send_message(cid, "<b>Admin</b>", reply_markup=k)

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

@bot.message_handler(commands=["thongbao"])
def broadcast_cmd(message):
    if not is_admin(message.from_user.id): return
    text = (message.text or "").partition(" ")[2].strip()
    if not text: bot.send_message(message.chat.id, "Dùng /thongbao nội dung"); return
    with db() as c: users = [r["telegram_id"] for r in c.execute("SELECT telegram_id FROM users")]
    sent = 0
    for uid in users:
        try: bot.send_message(uid, "<b>Thông báo từ admin</b>\n\n" + html.escape(text)); sent += 1
        except Exception: pass
    with db() as c: c.execute("INSERT INTO broadcasts(text,sent_at) VALUES(?,?)", (text, iso(now())))
    bot.send_message(message.chat.id, f"Đã gửi {sent}/{len(users)} người dùng.")


def send_stats(cid):
    with db() as c:
        u = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        k = c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NULL").fetchone()["n"]
        d = c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"]
        total = c.execute("SELECT COALESCE(SUM(amount),0) s FROM deposits WHERE status='approved'").fetchone()["s"]
    bot.send_message(cid, f"<b>Thống kê</b>\nNgười dùng: {u}\nKey chưa dùng: {k}\nĐơn chờ duyệt: {d}\nTổng đơn đã duyệt: {fmt_money(total)}")


def notify_admin_deposit(did):
    with db() as c:
        r = c.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
    if not r: return
    k = types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("Duyệt", callback_data=f"approve:{did}"), types.InlineKeyboardButton("Từ chối", callback_data=f"reject:{did}"))
    text = f"<b>Đơn nạp #{did}</b>\nUser: <code>{r['telegram_id']}</code>\nSố tiền: <b>{fmt_money(r['amount'])}</b>\nNội dung: <code>{r['content']}</code>"
    for aid in ADMIN_IDS:
        try: bot.send_message(aid, text, reply_markup=k)
        except Exception: pass


def confirm_deposit(uid, did):
    with db() as c: r = c.execute("SELECT * FROM deposits WHERE id=? AND telegram_id=?", (did, uid)).fetchone()
    if not r or r["status"] != "pending": bot.send_message(uid, "Đơn không tồn tại hoặc đã xử lý."); return
    bot.send_message(uid, "Đã gửi yêu cầu xác nhận tới admin. Vui lòng chờ duyệt."); notify_admin_deposit(did)


def decide_deposit(uid, did, approved):
    if not is_admin(uid): return
    with db() as c:
        r = c.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if not r or r["status"] != "pending": return
        status = "approved" if approved else "rejected"
        c.execute("UPDATE deposits SET status=?,decided_at=?,decided_by=? WHERE id=?", (status, iso(now()), uid, did))
    if approved:
        bot.send_message(r["telegram_id"], f"Đơn nạp #{did} đã được duyệt. Admin sẽ cấp key cho bạn bằng lệnh /taokey.")
    else: bot.send_message(r["telegram_id"], f"Đơn nạp #{did} đã bị từ chối. Vui lòng liên hệ admin nếu cần đối soát.")
    bot.send_message(uid, f"Đã {'duyệt' if approved else 'từ chối'} đơn #{did}.")


# ============================================================
# ADMIN WEB KHÔNG ĐĂNG NHẬP — CHỈ DÙNG MÔI TRƯỜNG RIÊNG
# ============================================================
ADMIN_HTML = """
<!doctype html><meta charset='utf-8'><title>MD5 TX Admin</title>
<style>body{font-family:Arial;max-width:1050px;margin:30px auto;background:#101827;color:#e5e7eb}input,textarea{width:100%;padding:9px;margin:5px 0 12px;background:#172235;color:white;border:1px solid #475569}button{padding:9px 14px;margin:4px;background:#2563eb;color:white;border:0;border-radius:5px}section{background:#172235;padding:18px;margin:14px 0;border-radius:8px}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #334155;text-align:left}</style>
<h1>MD5 Tài Xỉu — Admin</h1><p><b>Cảnh báo:</b> trang này không có đăng nhập theo yêu cầu. Không chia sẻ URL quản trị công khai.</p>
<section><h2>Ngân hàng / VietQR</h2><form method='post' action='/admin/bank'><input name='bank_code' placeholder='Mã ngân hàng' value='{{bank.bank_code}}'><input name='account_no' placeholder='Số tài khoản' value='{{bank.account_no}}'><input name='account_name' placeholder='Tên tài khoản' value='{{bank.account_name}}'><input name='note_prefix' placeholder='Tiền tố nội dung' value='{{bank.note_prefix}}'><button>Lưu ngân hàng</button></form></section>
<section><h2>Gói key</h2><form method='post' action='/admin/packages'><textarea name='packages' rows='8'>{{packages_json}}</textarea><p>JSON mẫu: {"Gói 1 ngày":{"price":10000,"days":1}}</p><button>Lưu gói</button></form></section>
<section><h2>Đơn nạp chờ duyệt</h2><table><tr><th>ID</th><th>User</th><th>Số tiền</th><th>Nội dung</th><th>Trạng thái</th></tr>{% for d in deposits %}<tr><td>{{d.id}}</td><td>{{d.telegram_id}}</td><td>{{d.amount}}</td><td>{{d.content}}</td><td>{{d.status}}</td></tr>{% endfor %}</table></section>
<section><h2>Tạo key nhanh</h2><form method='post' action='/admin/key'><select name='package'>{% for n in packages %}<option>{{n}}</option>{% endfor %}</select><button>Tạo key</button></form>{% if generated %}<p>Key mới: <code>{{generated}}</code></p>{% endif %}</section>
"""

@app.get("/")
def health(): return jsonify(ok=True, service=BOT_NAME, time=iso(now()))

@app.get("/admin")
def admin_page():
    with db() as c: deposits = c.execute("SELECT * FROM deposits ORDER BY id DESC LIMIT 100").fetchall()
    packages = get_setting("packages", DEFAULT_PACKAGES); bank = get_setting("bank", DEFAULT_BANK)
    return render_template_string(ADMIN_HTML, deposits=deposits, packages=packages, bank=bank, packages_json=json.dumps(packages, ensure_ascii=False, indent=2), generated=request.args.get("generated", ""))

@app.post("/admin/bank")
def admin_bank():
    set_setting("bank", {k: request.form.get(k, "").strip() for k in DEFAULT_BANK})
    return redirect("/admin")

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
    threading.Thread(target=run_web, daemon=True).start()
    log.info("Bot starting on port %s", PORT)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
