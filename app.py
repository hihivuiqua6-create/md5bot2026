# ============================================================
# TELEGRAM TOOL PRO v11.0 - RENDER FULL STACK
# 1 file duy nhất: FastAPI + Telethon + Web UI
# Deploy Render.com → chạy 24/7
# ============================================================

import os
import json
import asyncio
import random
import time
import re
from datetime import datetime
from typing import Optional, Dict, List
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import uvicorn

from telethon import TelegramClient, errors, functions
from telethon.tl.functions.channels import InviteToChannelRequest, JoinChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat, User
from telethon.errors import (
    FloodWaitError, UserPrivacyRestrictedError, PeerFloodError,
    SessionPasswordNeededError, ChatAdminRequiredError, UsersTooMuchError,
    UserNotMutualContactError, UserChannelsTooMuchError,
    InputUserDeactivatedError, UserAlreadyParticipantError
)

# ============================================================
# CẤU HÌNH
# ============================================================
VERIFY_KEY_URL = "https://bucac.onrender.com/api/verify-key"

# Thư mục lưu trữ
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
(DATA_DIR / "sessions").mkdir(exist_ok=True)
(DATA_DIR / "users").mkdir(exist_ok=True)
(DATA_DIR / "logs").mkdir(exist_ok=True)

# ============================================================
# FASTAPI
# ============================================================
app = FastAPI(title="Telegram Tool Pro v11.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# STORAGE
# ============================================================
ACTIVE_TASKS: Dict[str, dict] = {}
CLIENTS: Dict[str, TelegramClient] = {}
CLIENTS_LOCK = asyncio.Lock()


def safe_key(key: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_\-]', '', key or '')


def user_state_path(key: str) -> Path:
    d = DATA_DIR / "users" / safe_key(key)
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def load_state(key: str) -> dict:
    p = user_state_path(key)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(key: str, state: dict):
    # Giới hạn log 500 dòng
    if 'logs' in state and len(state['logs']) > 500:
        state['logs'] = state['logs'][-500:]
    try:
        user_state_path(key).write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
    except Exception as e:
        print(f"[save_state] {e}")


def append_log(key: str, msg: str):
    state = load_state(key)
    logs = state.get('logs', [])
    logs.append(f"{datetime.now().strftime('%H:%M:%S')} - {msg}")
    logs = logs[-500:]
    state['logs'] = logs
    save_state(key, state)


# ============================================================
# MODELS
# ============================================================
class VerifyKeyRequest(BaseModel):
    key: str
    device_id: str = ""


class LoginRequest(BaseModel):
    key: str
    api_id: str
    api_hash: str
    phone: str


class OTPRequest(BaseModel):
    key: str
    code: str = ""
    password_2fa: str = ""


class StartRequest(BaseModel):
    key: str
    target_link: str
    niche: str = ""
    min_members: int = 1000
    max_groups: int = 20
    delay: int = 12
    min_score: int = 45
    target_members: int = 5000


class KeyOnlyRequest(BaseModel):
    key: str


# ============================================================
# AI ENGINE
# ============================================================
class AIEngine:
    def __init__(self):
        self.consecutive_fails = 0
        self.blocked_users = set()

    def user_score(self, user, group_quality=50) -> int:
        score = 30
        if getattr(user, 'username', None): score += 20
        if getattr(user, 'premium', False): score += 15
        if getattr(user, 'verified', False): score += 20
        if getattr(user, 'photo', None): score += 10
        if getattr(user, 'bot', False): score -= 100
        if getattr(user, 'deleted', False): score -= 100
        if getattr(user, 'scam', False) or getattr(user, 'fake', False): score -= 100
        status = getattr(user, 'status', None)
        if status:
            sname = type(status).__name__
            if 'Online' in sname: score += 20
            elif 'Recently' in sname: score += 15
            elif 'LastWeek' in sname: score += 5
            elif 'LongAgo' in sname: score -= 20
        score += int(group_quality * 0.2)
        return min(100, max(0, score))

    def group_score(self, g: dict) -> int:
        score = 50
        m = g.get('participants_count', 0) or 0
        if m >= 100000: score += 30
        elif m >= 50000: score += 25
        elif m >= 10000: score += 20
        elif m >= 5000: score += 15
        elif m >= 1000: score += 10
        if g.get('username'): score += 10
        if g.get('megagroup'): score += 5
        if g.get('verified'): score += 15
        return min(100, max(0, score))

    def extract_niche(self, title: str, about: str = "") -> str:
        text = f"{title} {about}".lower()
        niches = {
            'crypto': ['crypto', 'bitcoin', 'btc', 'eth', 'blockchain', 'nft', 'coin'],
            'trading': ['trade', 'forex', 'stock', 'chứng khoán', 'đầu tư'],
            'marketing': ['marketing', 'mmo', 'affiliate', 'seo'],
            'tech': ['tech', 'lập trình', 'programming', 'code', 'dev'],
            'gaming': ['game', 'gaming', 'esport', 'liên quân', 'pubg'],
            'education': ['học', 'sinh viên', 'study', 'ielts'],
            'job': ['việc làm', 'tuyển dụng', 'job'],
        }
        for niche, kws in niches.items():
            for kw in kws:
                if kw in text:
                    return niche
        return 'general'

    def calc_delay(self, base_delay: int, success_rate: float) -> float:
        d = base_delay
        if success_rate >= 95: d *= 0.8
        elif success_rate >= 85: d *= 1.0
        elif success_rate >= 70: d *= 1.3
        elif success_rate >= 50: d *= 1.8
        else: d *= 2.5
        if self.consecutive_fails >= 3: d *= 1.5
        if self.consecutive_fails >= 5: d *= 2.0
        d += random.uniform(0, d * 0.3)
        return min(d, 300)


# ============================================================
# ENDPOINTS
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index():
    """Trang chính - full UI"""
    return HTML_PAGE


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "active_tasks": len(ACTIVE_TASKS),
        "time": datetime.now().isoformat()
    }


@app.post("/api/verify-key")
async def verify_key(req: VerifyKeyRequest):
    """Verify key qua server bucac"""
    if not req.key:
        return {"success": False, "message": "Thiếu key"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                VERIFY_KEY_URL,
                json={"key": req.key, "device_id": req.device_id}
            )
            data = r.json()
            if data.get("success"):
                state = load_state(req.key)
                state["verified"] = True
                state["device_id"] = req.device_id
                state["verified_at"] = datetime.now().isoformat()
                save_state(req.key, state)
            return data
    except Exception as e:
        return {"success": False, "message": f"Không kết nối được server key: {str(e)[:100]}"}


@app.post("/api/login")
async def login(req: LoginRequest):
    """Đăng nhập Telegram - gửi OTP"""
    key = safe_key(req.key)
    if not key:
        return {"success": False, "message": "Thiếu key"}

    state = load_state(key)
    if not state.get("verified"):
        return {"success": False, "message": "Key chưa verify"}

    session_file = str(DATA_DIR / "sessions" / key)

    try:
        # Ngắt client cũ
        async with CLIENTS_LOCK:
            if key in CLIENTS:
                try:
                    await CLIENTS[key].disconnect()
                except Exception:
                    pass
                del CLIENTS[key]

            client = TelegramClient(session_file, int(req.api_id), req.api_hash)
            await client.connect()
            CLIENTS[key] = client

        if not await client.is_user_authorized():
            await client.send_code_request(req.phone)
            state.update({
                "phone": req.phone,
                "api_id": req.api_id,
                "api_hash": req.api_hash,
                "login_stage": "need_otp",
                "logged_in": False,
            })
            save_state(key, state)
            return {"success": True, "stage": "need_otp", "message": "OTP đã gửi"}

        me = await client.get_me()
        state.update({
            "phone": req.phone,
            "api_id": req.api_id,
            "api_hash": req.api_hash,
            "login_stage": "logged_in",
            "logged_in": True,
            "user_name": me.username or me.first_name or "",
            "user_id": me.id,
        })
        save_state(key, state)
        return {"success": True, "stage": "logged_in", "message": "Đã đăng nhập"}

    except Exception as e:
        return {"success": False, "message": str(e)[:200]}


@app.post("/api/verify-otp")
async def verify_otp(req: OTPRequest):
    """Xác thực OTP"""
    key = safe_key(req.key)
    if key not in CLIENTS:
        return {"success": False, "message": "Chưa gửi OTP. Gọi login trước."}

    client = CLIENTS[key]
    state = load_state(key)
    phone = state.get("phone", "")

    try:
        try:
            await client.sign_in(phone, req.code)
        except SessionPasswordNeededError:
            if req.password_2fa:
                await client.sign_in(password=req.password_2fa)
            else:
                return {"success": False, "stage": "need_2fa", "message": "Cần mật khẩu 2FA"}

        me = await client.get_me()
        state.update({
            "login_stage": "logged_in",
            "logged_in": True,
            "user_name": me.username or me.first_name or "",
            "user_id": me.id,
        })
        save_state(key, state)
        return {"success": True, "stage": "logged_in", "message": "Đăng nhập OK"}

    except Exception as e:
        return {"success": False, "message": str(e)[:200]}


@app.post("/api/start")
async def start_task(req: StartRequest):
    """Bắt đầu auto-add"""
    key = safe_key(req.key)
    if key not in CLIENTS:
        return {"success": False, "message": "Chưa đăng nhập"}

    if key in ACTIVE_TASKS and ACTIVE_TASKS[key].get("running"):
        return {"success": False, "message": "Đang chạy task khác"}

    ACTIVE_TASKS[key] = {
        "running": True,
        "start_time": time.time(),
        "target_link": req.target_link,
        "target_members": req.target_members,
        "current_members": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "stage": "starting",
    }

    state = load_state(key)
    state["config"] = req.dict()
    state["logs"] = []
    state["started_at"] = datetime.now().isoformat()
    save_state(key, state)

    # Chạy nền — vẫn sống kể cả khi tắt trình duyệt
    asyncio.create_task(run_auto_add(key, req))

    return {"success": True, "message": "Đã bắt đầu task"}


@app.post("/api/stop")
async def stop_task(req: KeyOnlyRequest):
    key = safe_key(req.key)
    if key in ACTIVE_TASKS:
        ACTIVE_TASKS[key]["running"] = False
        append_log(key, "🛑 Đã dừng theo yêu cầu")
        return {"success": True, "message": "Đã dừng"}
    return {"success": False, "message": "Không có task nào chạy"}


@app.get("/api/status/{key}")
async def get_status(key: str):
    """Lấy trạng thái realtime"""
    key = safe_key(key)
    task = ACTIVE_TASKS.get(key, {})
    state = load_state(key)
    return {
        "success": True,
        "running": task.get("running", False),
        "stage": task.get("stage", "idle"),
        "current_members": task.get("current_members", 0),
        "target_members": task.get("target_members", 0),
        "success_count": task.get("success", 0),
        "failed_count": task.get("failed", 0),
        "skipped_count": task.get("skipped", 0),
        "logs": state.get("logs", [])[-100:],
        "logged_in": state.get("logged_in", False),
        "user_name": state.get("user_name", ""),
    }


# ============================================================
# MAIN LOGIC
# ============================================================
async def run_auto_add(key: str, req: StartRequest):
    """Logic chính — chạy nền 24/7"""
    client = CLIENTS.get(key)
    if not client:
        return

    ai = AIEngine()
    task = ACTIVE_TASKS[key]

    try:
        append_log(key, f"🎯 Bắt đầu: {req.target_link}")

        target = await client.get_entity(req.target_link)
        if isinstance(target, Chat):
            append_log(key, "❌ Nhóm đích là NHÓM THƯỜNG! Cần SUPERGROUP.")
            task["running"] = False
            return
        if isinstance(target, Channel) and not target.megagroup:
            append_log(key, "❌ Nhóm đích là KÊNH!")
            task["running"] = False
            return

        task["stage"] = "analyzing"
        target_title = target.title
        target_about = getattr(target, 'about', '') or ''
        append_log(key, f"✅ Nhóm đích: {target_title}")

        niche = req.niche.lower() if req.niche else ai.extract_niche(target_title, target_about)
        append_log(key, f"🎯 Niche: {niche}")

        current = getattr(target, 'participants_count', 0) or 0
        task["current_members"] = current
        append_log(key, f"📊 Hiện tại: {current} / {req.target_members}")

        invited = set(load_invited(key))

        round_num = 0
        while task.get("running") and current < req.target_members:
            round_num += 1
            append_log(key, f"")
            append_log(key, f"🔄 VÒNG {round_num}")
            task["stage"] = f"round_{round_num}"

            # Update member count
            try:
                await asyncio.sleep(3)
                t = await client.get_entity(req.target_link)
                current = getattr(t, 'participants_count', 0) or 0
                task["current_members"] = current
                append_log(key, f"📊 Check: {current} / {req.target_members}")
                if current >= req.target_members:
                    append_log(key, "🎉 ĐỦ MEMBER!")
                    break
            except Exception:
                pass

            remaining = req.target_members - current
            scan_amount = int(remaining * 1.4)

            # Tìm nhóm nguồn
            task["stage"] = "discovering"
            append_log(key, f"🔍 Tìm nhóm nguồn...")
            groups = await discover_groups(client, ai, niche, target_title, req.min_members, req.max_groups, key)

            if not groups:
                append_log(key, "⚠️ Không có nhóm! Nghỉ 5 phút...")
                for _ in range(30):
                    if not task.get("running"): break
                    await asyncio.sleep(10)
                continue

            # Thu hoạch user
            task["stage"] = "harvesting"
            append_log(key, f"📥 Quét user từ {len(groups)} nhóm...")
            users = await harvest_users(client, ai, groups, req.min_score, scan_amount, invited, key, task)

            if not users:
                append_log(key, "⚠️ Không có user! Nghỉ 5 phút...")
                for _ in range(30):
                    if not task.get("running"): break
                    await asyncio.sleep(10)
                continue

            # ADD
            task["stage"] = "adding"
            append_log(key, f"🚀 ADD {len(users)} user...")

            for u in users:
                if not task.get("running"): break

                # Check mỗi 10
                if task["success"] > 0 and task["success"] % 10 == 0:
                    try:
                        await asyncio.sleep(2)
                        t = await client.get_entity(req.target_link)
                        current = getattr(t, 'participants_count', 0) or 0
                        task["current_members"] = current
                        append_log(key, f"   📊 {current} / {req.target_members}")
                        if current >= req.target_members:
                            append_log(key, "🎉 ĐỦ!")
                            task["running"] = False
                            break
                    except Exception:
                        pass

                user = u['user']
                name = user.username or f"ID:{user.id}"

                try:
                    append_log(key, f"📤 {name}")
                    await client(InviteToChannelRequest(target, [user]))
                    append_log(key, f"   ✅ OK")
                    task["success"] += 1
                    invited.add(user.id)
                    save_invited(key, invited)
                    ai.consecutive_fails = 0

                    rate = task["success"] / max(1, task["success"] + task["failed"]) * 100
                    d = ai.calc_delay(req.delay, rate)
                    append_log(key, f"   ⏳ {int(d)}s...")
                    await asyncio.sleep(d)

                except FloodWaitError as e:
                    append_log(key, f"⚠️ FloodWait {e.seconds}s")
                    for _ in range(e.seconds // 10 + 1):
                        if not task.get("running"): break
                        await asyncio.sleep(min(10, e.seconds))

                except PeerFloodError:
                    append_log(key, f"🚨 PeerFlood! Nghỉ 3 phút...")
                    ai.consecutive_fails += 1
                    for _ in range(18):
                        if not task.get("running"): break
                        await asyncio.sleep(10)

                except UserPrivacyRestrictedError:
                    append_log(key, f"   🚫 Privacy")
                    task["failed"] += 1
                    invited.add(user.id)
                    save_invited(key, invited)
                    ai.consecutive_fails += 1

                except UserNotMutualContactError:
                    append_log(key, f"   🚫 Not mutual")
                    task["skipped"] += 1
                    invited.add(user.id)
                    save_invited(key, invited)

                except UserAlreadyParticipantError:
                    append_log(key, f"   ⏭️ Đã trong")
                    task["skipped"] += 1
                    invited.add(user.id)
                    save_invited(key, invited)

                except UserChannelsTooMuchError:
                    append_log(key, f"   ⚠️ Quá nhiều nhóm")
                    task["skipped"] += 1
                    invited.add(user.id)
                    save_invited(key, invited)

                except InputUserDeactivatedError:
                    append_log(key, f"   ⏭️ Acc xóa")
                    task["skipped"] += 1
                    invited.add(user.id)
                    save_invited(key, invited)

                except ChatAdminRequiredError:
                    append_log(key, "❌ Không có quyền!")
                    task["running"] = False
                    break

                except UsersTooMuchError:
                    append_log(key, "❌ Nhóm đầy!")
                    task["running"] = False
                    break

                except Exception as e:
                    append_log(key, f"   ❌ {str(e)[:80]}")
                    task["failed"] += 1
                    await asyncio.sleep(5)

            if task.get("running"):
                append_log(key, f"⏸️ Nghỉ 60s...")
                for _ in range(6):
                    if not task.get("running"): break
                    await asyncio.sleep(10)

        append_log(key, "=" * 40)
        append_log(key, f"🎉 HOÀN TẤT - OK: {task['success']}, Fail: {task['failed']}, Skip: {task['skipped']}")

    except Exception as e:
        append_log(key, f"❌ LỖI: {str(e)[:200]}")
    finally:
        task["running"] = False
        task["stage"] = "idle"


async def discover_groups(client, ai, niche, title, min_members, max_groups, key):
    groups = []

    # Dialogs
    try:
        async for dialog in client.iter_dialogs(limit=200):
            e = dialog.entity
            if not isinstance(e, (Channel, Chat)): continue
            if isinstance(e, Channel) and not e.megagroup: continue
            cnt = getattr(e, 'participants_count', 0) or 0
            if cnt >= min_members:
                groups.append({
                    'id': e.id, 'title': e.title, 'entity': e,
                    'participants_count': cnt,
                    'username': getattr(e, 'username', None),
                    'megagroup': getattr(e, 'megagroup', False),
                    'verified': getattr(e, 'verified', False),
                })
    except Exception as ex:
        append_log(key, f"⚠️ Dialogs: {str(ex)[:60]}")

    append_log(key, f"   📂 Dialogs: {len(groups)}")

    # Search
    queries = [
        niche, f"{niche} việt nam", f"{niche} vn", f"group {niche}",
        "việt nam", "vietnam chat", "cộng đồng việt", "mmo việt nam",
    ]
    if niche == 'general':
        queries = ["việt nam", "vietnam", "cộng đồng việt", "sinh viên", "mmo"]

    for q in queries[:10]:
        if len(groups) >= max_groups * 2: break
        try:
            r = await client(SearchRequest(q=q, limit=10))
            for chat in r.chats:
                if not isinstance(chat, Channel): continue
                if not chat.megagroup: continue
                if any(g['id'] == chat.id for g in groups): continue
                cnt = getattr(chat, 'participants_count', 0) or 0
                if cnt >= min_members:
                    groups.append({
                        'id': chat.id, 'title': chat.title, 'entity': chat,
                        'participants_count': cnt,
                        'username': getattr(chat, 'username', None),
                        'megagroup': getattr(chat, 'megagroup', False),
                        'verified': getattr(chat, 'verified', False),
                    })
            await asyncio.sleep(random.uniform(2, 4))
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 60))
        except Exception:
            continue

    for g in groups:
        g['ai_score'] = ai.group_score(g)
    groups.sort(key=lambda x: x['ai_score'], reverse=True)

    append_log(key, f"   ✅ Tổng {len(groups)}")
    return groups[:max_groups]


async def harvest_users(client, ai, groups, min_score, max_users, invited, key, task):
    users = {}
    scanned = set()

    for i, g in enumerate(groups, 1):
        if not task.get("running"): break
        if len(users) >= max_users: break

        append_log(key, f"   📥 [{i}/{len(groups)}] {g['title'][:35]}...")
        try:
            e = g['entity']
            try:
                await client(JoinChannelRequest(e))
            except Exception:
                pass

            cnt = 0
            async for user in client.iter_participants(e, aggressive=True):
                if not task.get("running"): break
                if cnt >= max_users // len(groups) + 100: break
                if not isinstance(user, User): continue
                if user.id in scanned: continue
                if user.bot or user.deleted: continue
                if getattr(user, 'scam', False) or getattr(user, 'fake', False): continue
                if user.id in invited: continue

                score = ai.user_score(user, g['ai_score'])
                if score >= min_score:
                    users[user.id] = {'user': user, 'score': score}
                    cnt += 1
                scanned.add(user.id)

            append_log(key, f"      ✅ {cnt} user")
            await asyncio.sleep(random.uniform(2, 4))

        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 60))
        except Exception as ex:
            append_log(key, f"      ⚠️ {str(ex)[:50]}")

    sorted_users = sorted(users.values(), key=lambda x: x['score'], reverse=True)
    append_log(key, f"   🎯 Tổng: {len(sorted_users)}")
    return sorted_users[:max_users]


def save_invited(key: str, invited: set):
    p = user_state_path(key).parent / "invited.json"
    try:
        p.write_text(json.dumps(list(invited)[-5000:]), encoding='utf-8')
    except Exception:
        pass


def load_invited(key: str) -> list:
    p = user_state_path(key).parent / "invited.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []


# ============================================================
# HTML PAGE
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Telegram Tool Pro · v11.0</title>
<script src="https://cdn.jsdelivr.net/npm/vue@2.7.14/dist/vue.min.js"></script>
<style>
:root{
  --pri:#00e5ff; --pri2:#7c5eff; --pink:#ff2fb3;
  --ok:#00e676; --warn:#ffab00; --err:#ff5252;
  --line:rgba(0,229,255,.15);
}
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
html,body{height:100%;overflow:hidden;color:#e6f1ff;background:radial-gradient(circle at 20% 10%,#101a3a 0%,#05060a 55%),#000}
body::before{
  content:"";position:fixed;inset:0;
  background-image:
    radial-gradient(2px 2px at 20% 30%,rgba(0,229,255,.6),transparent),
    radial-gradient(1px 1px at 60% 70%,rgba(124,94,255,.6),transparent),
    radial-gradient(1.5px 1.5px at 80% 20%,rgba(255,47,179,.5),transparent);
  background-size:400px 400px,300px 300px,500px 500px;
  animation:stars 40s linear infinite;pointer-events:none;opacity:.7;
}
@keyframes stars{to{background-position:400px 400px,-300px 300px,500px -500px}}
#app{position:relative;height:100%;display:flex;align-items:center;justify-content:center;padding:12px}
.card{
  width:440px;max-width:100%;max-height:96vh;
  background:linear-gradient(160deg,rgba(13,16,32,.96),rgba(5,6,10,.96));
  border:1px solid var(--line);border-radius:16px;overflow:hidden;
  box-shadow:0 24px 60px rgba(0,0,0,.7),0 0 40px rgba(0,229,255,.08) inset;
  display:flex;flex-direction:column;
}
.bar{
  height:52px;display:flex;align-items:center;gap:10px;padding:0 14px;
  background:linear-gradient(135deg,#141830,#0a0d1e);
  border-bottom:1px solid var(--line);
  cursor:move;user-select:none;position:relative;
}
.bar::after{content:"";position:absolute;left:0;right:0;bottom:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--pri),var(--pri2),transparent);opacity:.6}
.dots{display:flex;gap:6px}
.dot{width:10px;height:10px;border-radius:50%}
.d1{background:#ff5f56}.d2{background:#ffbd2e}.d3{background:#27c93f}
.ttl{flex:1;text-align:center;font-weight:700;font-size:13px;letter-spacing:.5px;
  background:linear-gradient(90deg,var(--pink),var(--pri),var(--pri2));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
  display:flex;align-items:center;justify-content:center;gap:8px}
.tgl{cursor:pointer;color:var(--pri);font-size:18px;padding:4px 8px;border-radius:6px;transition:.2s}
.tgl:hover{background:rgba(0,229,255,.1)}
.body{padding:14px;overflow-y:auto;flex:1}
.body::-webkit-scrollbar{width:6px}
.body::-webkit-scrollbar-thumb{background:linear-gradient(var(--pri),var(--pri2));border-radius:3px}
.login{padding:20px 8px;text-align:center}
.logo{width:88px;height:88px;border-radius:50%;margin:0 auto 16px;
  background:conic-gradient(from 0deg,var(--pri),var(--pri2),var(--pink),var(--pri));
  padding:3px;animation:spin 8s linear infinite}
.logo>div{width:100%;height:100%;border-radius:50%;background:#05060a;display:flex;align-items:center;justify-content:center;font-size:32px}
@keyframes spin{to{transform:rotate(360deg)}}
.h1{font-size:18px;font-weight:800;letter-spacing:1px;margin-bottom:6px;
  background:linear-gradient(90deg,var(--pink),var(--pri),var(--pri2));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sub{font-size:12px;color:#8892b0;margin-bottom:18px}
.field{margin:12px 0;text-align:left}
.field label{display:block;font-size:11px;font-weight:600;color:#8892b0;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px}
.field input{width:100%;padding:11px 13px;background:rgba(0,0,0,.4);
  border:1px solid rgba(0,229,255,.2);border-radius:9px;color:#fff;font-size:13px;outline:none;transition:.2s}
.field input:focus{border-color:var(--pri);box-shadow:0 0 0 3px rgba(0,229,255,.12)}
.btn{width:100%;height:44px;margin-top:14px;font-size:13px;font-weight:700;
  text-transform:uppercase;letter-spacing:1px;color:#fff;border:none;border-radius:11px;cursor:pointer;
  background:linear-gradient(135deg,var(--pink),var(--pri2));transition:.25s}
.btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 10px 24px rgba(124,94,255,.35)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.ok{background:linear-gradient(135deg,#00b894,var(--ok))}
.btn.err{background:linear-gradient(135deg,#c0392b,var(--err))}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--pri)}
.dev{font-size:10px;color:#6a7fa8;word-break:break-all;margin-top:12px;padding:8px;background:rgba(0,0,0,.3);border-radius:6px}
.msg{margin-top:10px;padding:9px 12px;border-radius:8px;font-size:12px}
.msg.err{background:rgba(255,82,82,.12);border:1px solid rgba(255,82,82,.4);color:#ff8a8a}
.msg.ok{background:rgba(0,230,118,.12);border:1px solid rgba(0,230,118,.4);color:#69f0ae}
.tabs{display:flex;background:rgba(0,0,0,.35);border-radius:10px;padding:4px;gap:4px;margin-bottom:12px}
.tabs button{flex:1;padding:8px 0;background:transparent;border:none;font-size:11px;font-weight:700;
  color:#8892b0;cursor:pointer;border-radius:7px;transition:.2s;letter-spacing:.5px;text-transform:uppercase}
.tabs button.active{background:linear-gradient(135deg,rgba(0,229,255,.2),rgba(124,94,255,.2));color:var(--pri)}
.prog-wrap{margin:12px 0;padding:12px;background:rgba(0,0,0,.3);border-radius:10px;border:1px solid var(--line)}
.prog-head{display:flex;justify-content:space-between;font-size:11px;color:#8892b0;margin-bottom:8px}
.prog-head b{color:var(--pri)}
.prog-bar{height:8px;background:rgba(255,255,255,.06);border-radius:4px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--pri),var(--pri2),var(--pink));
  border-radius:4px;transition:width .5s;box-shadow:0 0 12px rgba(0,229,255,.5)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}
.stat{padding:10px;background:rgba(0,0,0,.3);border-radius:9px;text-align:center;border:1px solid var(--line)}
.stat b{display:block;font-size:18px;font-weight:800;margin-bottom:2px}
.stat span{font-size:10px;color:#8892b0;text-transform:uppercase;letter-spacing:.5px}
.stat.s1 b{color:var(--ok)}.stat.s2 b{color:var(--err)}.stat.s3 b{color:var(--warn)}
.log{background:#02030a;border:1px solid var(--line);border-radius:10px;padding:10px;
  height:220px;overflow-y:auto;font-family:'Consolas','Monaco',monospace;font-size:11px;
  line-height:1.6;color:#8892b0}
.log::-webkit-scrollbar{width:5px}
.log::-webkit-scrollbar-thumb{background:var(--pri);border-radius:3px}
.log div{padding:1px 0;word-break:break-word}
.log .ok{color:var(--ok)}
.log .warn{color:var(--warn)}
.log .err{color:var(--err)}
.log .info{color:var(--pri)}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.badge{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;font-size:10px;
  border-radius:20px;font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.badge.on{background:rgba(0,230,118,.15);color:var(--ok);border:1px solid rgba(0,230,118,.4)}
.badge.idle{background:rgba(136,146,176,.15);color:#8892b0;border:1px solid rgba(136,146,176,.3)}
.dot-live{width:7px;height:7px;border-radius:50%;background:var(--ok);
  box-shadow:0 0 8px var(--ok);animation:pulse 1.5s infinite}
@keyframes pulse{50%{opacity:.4;transform:scale(.85)}}
.footer{text-align:center;font-size:10px;color:#4a5578;padding:8px;letter-spacing:1px}
</style>
</head>
<body>
<div id="app">
  <div class="card" ref="card" :style="cardStyle">
    <div class="bar" @mousedown="startDrag" @touchstart="startTouch">
      <div class="dots"><span class="dot d1"></span><span class="dot d2"></span><span class="dot d3"></span></div>
      <div class="ttl">
        <template v-if="!logged">🔐 KEY VERIFY · MINHDUC PRO</template>
        <template v-else>
          ⚡ TELEGRAM PRO v11.0
          <span :class="['badge', running ? 'on' : 'idle']">
            <span v-if="running" class="dot-live"></span>
            {{ running ? 'RUNNING' : 'IDLE' }}
          </span>
        </template>
      </div>
      <div class="tgl" @click="show = !show">{{ show ? '▼' : '▲' }}</div>
    </div>

    <div class="body" v-show="show && !logged">
      <div class="login">
        <div class="logo"><div>🔑</div></div>
        <div class="h1">TELEGRAM TOOL PRO</div>
        <div class="sub">Nhập key để kích hoạt</div>
        <div class="field">
          <label>License Key</label>
          <input v-model="key" placeholder="xxxx-xxxx-xxxx-xxxx" @keyup.enter="verifyKey" />
        </div>
        <button class="btn" :disabled="busy || !key" @click="verifyKey">
          {{ busy ? '⏳ Đang xác thực...' : '✅ XÁC THỰC KEY' }}
        </button>
        <div class="dev">Device ID: {{ deviceId || 'Đang tạo...' }}</div>
        <div v-if="msg" :class="['msg', msgType]">{{ msg }}</div>
        <div class="sub" style="margin-top:14px;font-size:11px">📞 Contact: @phamcduc0</div>
      </div>
    </div>

    <div class="body" v-show="show && logged">
      <div class="tabs">
        <button :class="{active:tab==='main'}" @click="tab='main'">🚀 MAIN</button>
        <button :class="{active:tab==='log'}" @click="tab='log'">📋 LOG</button>
        <button :class="{active:tab==='acc'}" @click="tab='acc'">👤 ACC</button>
      </div>

      <div v-show="tab==='main'">
        <div v-if="!telegramLogged">
          <div class="h1" style="font-size:14px;margin-bottom:10px">🔐 ĐĂNG NHẬP TELEGRAM</div>
          <div class="g2">
            <div class="field"><label>API ID</label><input v-model="apiId" placeholder="1234567" /></div>
            <div class="field"><label>SĐT</label><input v-model="phone" placeholder="+849..." /></div>
          </div>
          <div class="field"><label>API HASH</label><input v-model="apiHash" placeholder="32 ký tự" /></div>
          <button class="btn ok" :disabled="busy" @click="doLogin">
            {{ busy ? '⏳...' : '📱 GỬI OTP' }}
          </button>
          <div v-if="needOtp" class="field"><label>Mã OTP</label>
            <input v-model="otp" placeholder="12345" @keyup.enter="doVerifyOtp" /></div>
          <div v-if="need2fa" class="field"><label>Mật khẩu 2FA</label>
            <input v-model="pwd2fa" type="password" @keyup.enter="doVerifyOtp" /></div>
          <button v-if="needOtp || need2fa" class="btn" :disabled="busy" @click="doVerifyOtp">
            {{ busy ? '⏳...' : '✅ XÁC THỰC' }}
          </button>
        </div>

        <div v-else>
          <div class="field"><label>Link nhóm đích</label>
            <input v-model="targetLink" placeholder="https://t.me/nhom_dich" /></div>
          <div class="g2">
            <div class="field"><label>Niche</label><input v-model="niche" placeholder="crypto, mmo..." /></div>
            <div class="field"><label>🎯 Member mục tiêu</label><input v-model.number="targetMembers" type="number" /></div>
          </div>
          <div class="g3">
            <div class="field"><label>Min mem</label><input v-model.number="minMembers" type="number" /></div>
            <div class="field"><label>Max nhóm</label><input v-model.number="maxGroups" type="number" /></div>
            <div class="field"><label>Delay(s)</label><input v-model.number="delay" type="number" /></div>
          </div>
          <div class="field"><label>AI Score (0-100)</label>
            <input v-model.number="minScore" type="number" min="0" max="100" /></div>
          <div class="prog-wrap">
            <div class="prog-head"><span>TIẾN ĐỘ</span>
              <b>{{ status.current_members || 0 }} / {{ targetMembers }}</b></div>
            <div class="prog-bar"><div class="prog-fill" :style="{width: progress + '%'}"></div></div>
          </div>
          <div class="stats">
            <div class="stat s1"><b>{{ status.success_count || 0 }}</b><span>✅ OK</span></div>
            <div class="stat s2"><b>{{ status.failed_count || 0 }}</b><span>❌ Fail</span></div>
            <div class="stat s3"><b>{{ status.skipped_count || 0 }}</b><span>⏭️ Skip</span></div>
          </div>
          <div class="g2">
            <button class="btn ok" :disabled="running || busy" @click="doStart">
              {{ running ? '▶️ ĐANG CHẠY' : '🚀 CHẠY' }}
            </button>
            <button class="btn err" :disabled="!running" @click="doStop">🛑 DỪNG</button>
          </div>
        </div>
      </div>

      <div v-show="tab==='log'">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:11px;color:#8892b0">
            <span class="dot-live"></span> LIVE · {{ logs.length }} dòng
          </span>
          <button class="btn ghost" style="width:auto;padding:4px 10px;height:auto;margin:0;font-size:10px" @click="logs=[]">🗑️ Clear</button>
        </div>
        <div class="log" ref="logBox">
          <div v-for="(l,i) in logs" :key="i" :class="logClass(l)">{{ l }}</div>
          <div v-if="!logs.length" style="text-align:center;color:#4a5578;padding:20px">Chưa có log</div>
        </div>
      </div>

      <div v-show="tab==='acc'">
        <div class="logo" style="width:70px;height:70px"><div style="font-size:26px">👤</div></div>
        <div style="text-align:center">
          <div class="h1" style="font-size:14px">{{ status.user_name || 'Chưa login' }}</div>
          <div class="sub">Key: {{ key.slice(0,12) }}...</div>
        </div>
        <div style="margin-top:14px;padding:12px;background:rgba(0,0,0,.3);border-radius:10px;border:1px solid var(--line)">
          <div style="font-size:11px;color:#8892b0;display:flex;justify-content:space-between;padding:4px 0">
            <span>Trạng thái</span>
            <b :style="{color: telegramLogged?'var(--ok)':'var(--err)'}">{{ telegramLogged?'Online':'Offline' }}</b>
          </div>
          <div style="font-size:11px;color:#8892b0;display:flex;justify-content:space-between;padding:4px 0">
            <span>Device ID</span>
            <b style="font-family:monospace;font-size:10px">{{ (deviceId||'').slice(0,16) }}...</b>
          </div>
        </div>
        <button class="btn err" style="margin-top:14px" @click="doLogout">🚪 ĐĂNG XUẤT</button>
      </div>

      <div class="footer">MINHDUC FFTH MENU · RENDER v11.0</div>
    </div>
  </div>
</div>

<script>
new Vue({
  el: '#app',
  data: {
    show: true, logged: false, busy: false, tab: 'main',
    msg: '', msgType: 'err',
    dragging: false, ox: 0, oy: 0, px: 0, py: 0, cardStyle: {},
    key: '', deviceId: '',
    apiId: '', apiHash: '', phone: '',
    needOtp: false, need2fa: false, otp: '', pwd2fa: '',
    telegramLogged: false,
    targetLink: '', niche: '', targetMembers: 5000,
    minMembers: 1000, maxGroups: 20, delay: 12, minScore: 45,
    status: {}, logs: [], running: false,
    pollId: null
  },
  computed: {
    progress() {
      if (!this.targetMembers) return 0;
      return Math.min(100, ((this.status.current_members || 0) / this.targetMembers) * 100);
    }
  },
  mounted() {
    this.genDevice();
    const savedKey = localStorage.getItem('tg_key');
    if (savedKey) { this.key = savedKey; this.verifyKey(); }
  },
  beforeDestroy() { clearInterval(this.pollId); },
  methods: {
    api(path, data, method) {
      method = method || 'POST';
      return fetch(path, {
        method: method,
        headers: {'Content-Type':'application/json'},
        body: method === 'POST' ? JSON.stringify(data) : undefined
      }).then(r => r.json()).catch(e => ({success:false, message:'Network: '+e.message}));
    },
    genDevice() {
      const nav = navigator, s = screen;
      const info = [nav.userAgent, nav.language, s.width+'x'+s.height, new Date().getTimezoneOffset(), nav.hardwareConcurrency||0].join('|');
      let h = 0;
      for (let i=0;i<info.length;i++) { h = ((h<<5)-h) + info.charCodeAt(i); h = h & h; }
      this.deviceId = 'WEB-' + Math.abs(h).toString(16).toUpperCase();
    },
    verifyKey() {
      if (!this.key) return;
      this.busy = true; this.msg = '';
      this.api('/api/verify-key', {key: this.key, device_id: this.deviceId}).then(r => {
        this.busy = false;
        if (r.success) {
          this.logged = true;
          localStorage.setItem('tg_key', this.key);
          this.startPolling();
          this.checkLoginState();
        } else {
          this.msg = '❌ ' + (r.message || 'Key không hợp lệ');
          this.msgType = 'err';
          localStorage.removeItem('tg_key');
        }
      });
    },
    checkLoginState() {
      this.api('/api/status/' + encodeURIComponent(this.key), null, 'GET').then(r => {
        if (r.logged_in) this.telegramLogged = true;
        if (r.user_name) this.status = Object.assign({}, this.status, {user_name: r.user_name});
      });
    },
    doLogin() {
      if (!this.apiId || !this.apiHash || !this.phone) {
        this.flashMsg('Nhập đủ API ID / Hash / SĐT', 'err'); return;
      }
      this.busy = true;
      this.api('/api/login', {key:this.key, api_id:this.apiId, api_hash:this.apiHash, phone:this.phone}).then(r => {
        this.busy = false;
        if (r.success) {
          if (r.stage === 'need_otp') { this.needOtp = true; this.flashMsg('📱 OTP đã gửi', 'ok'); }
          else if (r.stage === 'logged_in') { this.telegramLogged = true; this.flashMsg('✅ Đã đăng nhập', 'ok'); }
        } else this.flashMsg(r.message || 'Lỗi login', 'err');
      });
    },
    doVerifyOtp() {
      this.busy = true;
      this.api('/api/verify-otp', {key:this.key, code:this.otp, password_2fa:this.pwd2fa}).then(r => {
        this.busy = false;
        if (r.success) {
          this.telegramLogged = true; this.needOtp = false; this.need2fa = false;
          this.flashMsg('✅ Đăng nhập OK', 'ok');
        } else if (r.stage === 'need_2fa') {
          this.need2fa = true;
          this.flashMsg('🔐 Cần mật khẩu 2FA', 'err');
        } else this.flashMsg(r.message || 'OTP sai', 'err');
      });
    },
    doStart() {
      if (!this.targetLink) { this.flashMsg('Nhập link nhóm đích', 'err'); return; }
      this.busy = true;
      const payload = {
        key: this.key, target_link: this.targetLink, niche: this.niche,
        min_members: this.minMembers, max_groups: this.maxGroups,
        delay: this.delay, min_score: this.minScore, target_members: this.targetMembers
      };
      this.api('/api/start', payload).then(r => {
        this.busy = false;
        if (r.success) { this.running = true; this.tab = 'log'; this.flashMsg('🚀 Đã bắt đầu', 'ok'); }
        else this.flashMsg(r.message || 'Lỗi start', 'err');
      });
    },
    doStop() {
      this.api('/api/stop', {key:this.key}).then(() => {
        this.running = false;
        this.flashMsg('🛑 Đã dừng', 'ok');
      });
    },
    doLogout() {
      localStorage.removeItem('tg_key');
      this.logged = false; this.telegramLogged = false;
      this.key = ''; this.logs = []; this.status = {};
      clearInterval(this.pollId);
    },
    startPolling() {
      clearInterval(this.pollId);
      this.pollId = setInterval(() => {
        if (!this.logged) return;
        fetch('/api/status/' + encodeURIComponent(this.key)).then(r => r.json()).then(r => {
          if (!r.success) return;
          this.status = r;
          this.running = !!r.running;
          if (r.logs) this.logs = r.logs;
          if (r.logged_in) this.telegramLogged = true;
          this.$nextTick(() => {
            const b = this.$refs.logBox;
            if (b) b.scrollTop = b.scrollHeight;
          });
        }).catch(()=>{});
      }, 2000);
    },
    flashMsg(t, type) {
      this.msg = t; this.msgType = type;
      setTimeout(() => { this.msg = ''; }, 4000);
    },
    logClass(l) {
      const s = String(l).toLowerCase();
      if (s.includes('❌') || s.includes('lỗi') || s.includes('fail')) return 'err';
      if (s.includes('⚠️') || s.includes('chặn') || s.includes('flood')) return 'warn';
      if (s.includes('✅') || s.includes('ok') || s.includes('thành công')) return 'ok';
      return 'info';
    },
    startDrag(e) {
      if (e.target.closest('.tgl')) return;
      this.dragging = true;
      this.ox = e.clientX; this.oy = e.clientY;
      const rect = this.$refs.card.getBoundingClientRect();
      this.px = rect.left; this.py = rect.top;
      window.addEventListener('mousemove', this.onDrag);
      window.addEventListener('mouseup', this.endDrag);
    },
    startTouch(e) {
      if (e.target.closest('.tgl')) return;
      const t = e.touches[0];
      this.dragging = true;
      this.ox = t.clientX; this.oy = t.clientY;
      const rect = this.$refs.card.getBoundingClientRect();
      this.px = rect.left; this.py = rect.top;
      window.addEventListener('touchmove', this.onTouchDrag, {passive:false});
      window.addEventListener('touchend', this.endDrag);
    },
    onDrag(e) {
      if (!this.dragging) return;
      const nx = this.px + (e.clientX - this.ox);
      const ny = this.py + (e.clientY - this.oy);
      this.cardStyle = {position:'fixed', left: nx+'px', top: ny+'px', margin:'0'};
    },
    onTouchDrag(e) {
      if (!this.dragging) return;
      e.preventDefault();
      const t = e.touches[0];
      const nx = this.px + (t.clientX - this.ox);
      const ny = this.py + (t.clientY - this.oy);
      this.cardStyle = {position:'fixed', left: nx+'px', top: ny+'px', margin:'0'};
    },
    endDrag() {
      this.dragging = false;
      window.removeEventListener('mousemove', this.onDrag);
      window.removeEventListener('mouseup', this.endDrag);
      window.removeEventListener('touchmove', this.onTouchDrag);
      window.removeEventListener('touchend', this.endDrag);
    }
  }
});
</script>
</body>
</html>
"""


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
