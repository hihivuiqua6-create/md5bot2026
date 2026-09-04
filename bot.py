import os
import re
import json
import time
import html
import sqlite3
import hashlib
import logging
import threading
import random
from datetime import datetime, timedelta, timezone
from collections import Counter
from typing import Dict, List, Tuple, Optional

import requests
import telebot
from telebot import types
from flask import Flask, request, redirect, render_template_string, jsonify, session, send_file
from werkzeug.security import generate_password_hash, check_password_hash

# ============================================================
# CẤU HÌNH QUA BIẾN MÔI TRƯỜNG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8477166662:AAHpUmD1-p9iPWIyvhKy_5I9Hc7sQkjwbU0").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "8030294480").split(",") if x.strip().lstrip("-").isdigit()}
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")
BOT_NAME = os.getenv("BOT_NAME", "MD5 Tài Xỉu Pro")

# Cấu hình ngân hàng (có thể sửa qua admin panel)
DEFAULT_BANK = {
    "bank_code": os.getenv("BANK_CODE", "MBBank"),
    "bank_name": os.getenv("BANK_NAME", "MB Bank"),
    "account_number": os.getenv("ACCOUNT_NUMBER", ""),
    "account_name": os.getenv("ACCOUNT_NAME", "")
}

# ============================================================
# KHỞI TẠO LOGGING
# ============================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# KHỞI TẠO DATABASE
# ============================================================
def init_db():
    """Khởi tạo database với schema đầy đủ"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Bảng users
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            user_id INTEGER UNIQUE,
            username TEXT,
            balance REAL DEFAULT 100000,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_admin INTEGER DEFAULT 0
        )
    ''')
    
    # Bảng games
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT UNIQUE,
            md5_hash TEXT,
            result TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP
        )
    ''')
    
    # Bảng bets
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_id TEXT UNIQUE,
            user_id INTEGER,
            game_id TEXT,
            amount REAL,
            choice TEXT,
            result TEXT,
            win_amount REAL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        )
    ''')
    
    # Bảng transactions
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id TEXT UNIQUE,
            user_id INTEGER,
            amount REAL,
            type TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    
    # Bảng settings
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # Bảng predictions
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT,
            prediction_data TEXT,
            accuracy REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Bảng cầu (pattern detection)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT,
            pattern_data TEXT,
            win_rate REAL,
            total_games INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

# Gọi init_db ngay khi khởi động
init_db()

# ============================================================
# THUẬT TOÁN PHÂN TÍCH MD5 NÂNG CAO
# ============================================================
class MD5Analyzer:
    """Thuật toán phân tích MD5 nâng cao - dự đoán kết quả tài xỉu"""
    
    def __init__(self):
        self.pattern_history = []
        self.md5_weights = {
            'first_digit': 0.15,
            'last_digit': 0.25,
            'sum_digits': 0.20,
            'hash_pattern': 0.25,
            'time_factor': 0.15
        }
    
    def analyze_md5(self, md5_hash: str) -> Dict:
        """Phân tích MD5 hash để dự đoán kết quả"""
        if not md5_hash or len(md5_hash) != 32:
            return {'prediction': 'unknown', 'confidence': 0.0}
        
        # Lấy các đặc trưng từ MD5
        first_digit = int(md5_hash[0], 16)
        last_digit = int(md5_hash[-1], 16)
        sum_digits = sum(int(c, 16) for c in md5_hash) % 10
        
        # Phân tích mẫu lặp trong hash
        char_freq = Counter(md5_hash)
        most_common = char_freq.most_common(3)
        
        # Tính tổng điểm dựa trên các đặc trưng
        score = 0
        score += first_digit * self.md5_weights['first_digit']
        score += last_digit * self.md5_weights['last_digit']
        score += sum_digits * self.md5_weights['sum_digits']
        
        # Phân tích mẫu lặp
        pattern_score = 0
        for char, count in most_common:
            pattern_score += count * 0.1
        score += pattern_score * self.md5_weights['hash_pattern']
        
        # Dự đoán: >= 50 là Tài, < 50 là Xỉu
        prediction = 'Tài' if score >= 5.0 else 'Xỉu'
        confidence = min(0.95, abs(score - 5.0) / 5.0 + 0.5)
        
        return {
            'prediction': prediction,
            'confidence': round(confidence, 2),
            'score': round(score, 2),
            'first_digit': first_digit,
            'last_digit': last_digit,
            'sum_digits': sum_digits,
            'char_freq': dict(most_common)
        }
    
    def detect_patterns(self, history: List[str]) -> Dict:
        """Phát hiện các mẫu cầu từ lịch sử"""
        if len(history) < 3:
            return {'patterns': [], 'next_prediction': 'unknown'}
        
        patterns = []
        recent = history[-20:]  # Xem 20 ván gần nhất
        
        # Mẫu 1: Cầu bệt (liên tiếp cùng kết quả)
        if len(set(recent[-5:])) == 1:
            patterns.append({'type': 'bệt', 'description': 'Cầu bệt 5 ván', 'confidence': 0.85})
        
        # Mẫu 2: Cầu 1-1 (đan xen)
        if len(recent) >= 4 and recent[-1] != recent[-2] and recent[-2] != recent[-3]:
            patterns.append({'type': '1-1', 'description': 'Cầu 1-1', 'confidence': 0.70})
        
        # Mẫu 3: Cầu 2-2
        if len(recent) >= 6:
            if recent[-1] == recent[-2] and recent[-3] == recent[-4] and recent[-1] != recent[-3]:
                patterns.append({'type': '2-2', 'description': 'Cầu 2-2', 'confidence': 0.75})
        
        # Mẫu 4: Cầu 3-2
        if len(recent) >= 7:
            if recent[-1] == recent[-2] == recent[-3] and recent[-4] == recent[-5] and recent[-1] != recent[-4]:
                patterns.append({'type': '3-2', 'description': 'Cầu 3-2', 'confidence': 0.65})
        
        # Dự đoán dựa trên mẫu
        next_prediction = 'unknown'
        if patterns:
            best_pattern = max(patterns, key=lambda p: p['confidence'])
            if best_pattern['type'] == 'bệt':
                next_prediction = recent[-1]
            elif best_pattern['type'] == '1-1':
                next_prediction = 'Xỉu' if recent[-1] == 'Tài' else 'Tài'
            elif best_pattern['type'] == '2-2':
                next_prediction = 'Xỉu' if recent[-1] == 'Tài' else 'Tài'
            elif best_pattern['type'] == '3-2':
                if recent[-1] == recent[-2] == recent[-3]:
                    next_prediction = recent[-1]
                else:
                    next_prediction = 'Xỉu' if recent[-1] == 'Tài' else 'Tài'
        
        return {'patterns': patterns, 'next_prediction': next_prediction}
    
    def combined_prediction(self, md5_hash: str, history: List[str]) -> Dict:
        """Kết hợp phân tích MD5 và phát hiện cầu"""
        md5_result = self.analyze_md5(md5_hash)
        pattern_result = self.detect_patterns(history)
        
        # Kết hợp kết quả
        final_prediction = md5_result['prediction']
        confidence = md5_result['confidence']
        
        # Nếu có mẫu cầu mạnh, ưu tiên mẫu cầu
        if pattern_result['next_prediction'] != 'unknown':
            for pattern in pattern_result['patterns']:
                if pattern['type'] in ['bệt', '2-2', '3-2'] and pattern['confidence'] > 0.75:
                    final_prediction = pattern_result['next_prediction']
                    confidence = max(confidence, pattern['confidence'])
                    break
        
        return {
            'prediction': final_prediction,
            'confidence': round(confidence, 2),
            'md5_analysis': md5_result,
            'pattern_analysis': pattern_result
        }
    
    def generate_mock_md5(self) -> str:
        """Tạo MD5 giả lập cho testing"""
        return hashlib.md5(str(random.random()).encode()).hexdigest()

# Khởi tạo analyzer
md5_analyzer = MD5Analyzer()

# ============================================================
# KHỞI TẠO BOT TELEGRAM
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN)

# ============================================================
# KHỞI TẠO FLASK APP
# ============================================================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-12345")

# ============================================================
# CÁC HÀM TIỆN ÍCH
# ============================================================
def get_db():
    """Lấy kết nối database"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def add_transaction(user_id, amount, type, description):
    """Thêm giao dịch"""
    conn = get_db()
    cursor = conn.cursor()
    txn_id = hashlib.md5(f"{user_id}{time.time()}{amount}".encode()).hexdigest()[:16]
    cursor.execute('''
        INSERT INTO transactions (txn_id, user_id, amount, type, description)
        VALUES (?, ?, ?, ?, ?)
    ''', (txn_id, user_id, amount, type, description))
    conn.commit()
    conn.close()
    return txn_id

def update_user_balance(user_id, amount):
    """Cập nhật số dư người dùng"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET balance = balance + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?', 
                   (amount, user_id))
    conn.commit()
    conn.close()

def get_user_balance(user_id):
    """Lấy số dư người dùng"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row['balance'] if row else 0

# ============================================================
# GIAO DIỆN WEB MỚI - NÂNG CẤP
# ============================================================
HOME_TEMPLATE = '''
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🏆 {{ BOT_NAME }} - MD5 Tài Xỉu Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            color: white;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
        }
        .header h1 { font-size: 2.5rem; margin-bottom: 10px; }
        .header p { font-size: 1.1rem; opacity: 0.9; }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: white;
            border-radius: 15px;
            padding: 20px;
            text-align: center;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            transition: transform 0.3s;
        }
        .stat-card:hover { transform: translateY(-5px); }
        .stat-card .value { font-size: 2.5rem; font-weight: bold; color: #764ba2; }
        .stat-card .label { color: #666; margin-top: 5px; }
        
        .prediction-section {
            background: white;
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
        }
        .prediction-box {
            background: linear-gradient(45deg, #ff6b6b, #feca57);
            border-radius: 15px;
            padding: 30px;
            text-align: center;
            margin: 20px 0;
        }
        .prediction-box h2 { color: white; font-size: 2rem; }
        .prediction-value {
            font-size: 4rem;
            font-weight: bold;
            color: white;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.05); }
        }
        .confidence-bar {
            background: #f0f0f0;
            border-radius: 10px;
            height: 20px;
            margin: 20px 0;
            overflow: hidden;
        }
        .confidence-fill {
            height: 100%;
            background: linear-gradient(90deg, #4ecdc4, #44bd32);
            border-radius: 10px;
            transition: width 0.5s;
        }
        
        .history-table {
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 15px;
            overflow: hidden;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        }
        .history-table th {
            background: #764ba2;
            color: white;
            padding: 15px;
            text-align: left;
        }
        .history-table td {
            padding: 12px 15px;
            border-bottom: 1px solid #eee;
        }
        .history-table tr:hover { background: #f8f9fa; }
        
        .btn {
            display: inline-block;
            padding: 12px 24px;
            border-radius: 50px;
            text-decoration: none;
            font-weight: bold;
            transition: all 0.3s;
            border: none;
            cursor: pointer;
            font-size: 1rem;
        }
        .btn-primary {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            box-shadow: 0 5px 15px rgba(102,126,234,0.4);
        }
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(102,126,234,0.5);
        }
        .btn-danger {
            background: linear-gradient(135deg, #f5576c, #f093fb);
            color: white;
        }
        .btn-success {
            background: linear-gradient(135deg, #4ecdc4, #44bd32);
            color: white;
        }
        
        .admin-panel {
            background: white;
            border-radius: 20px;
            padding: 30px;
            margin-top: 30px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: bold;
            color: #555;
        }
        .form-group input, .form-group select {
            width: 100%;
            padding: 12px;
            border: 2px solid #ddd;
            border-radius: 10px;
            font-size: 1rem;
            transition: border-color 0.3s;
        }
        .form-group input:focus, .form-group select:focus {
            border-color: #764ba2;
            outline: none;
        }
        
        .alert {
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            font-weight: bold;
        }
        .alert-success { background: #d4edda; color: #155724; }
        .alert-danger { background: #f8d7da; color: #721c24; }
        .alert-info { background: #d1ecf1; color: #0c5460; }
        
        .nav-menu {
            display: flex;
            gap: 15px;
            margin-bottom: 30px;
        }
        .nav-menu a {
            color: white;
            text-decoration: none;
            padding: 10px 20px;
            border-radius: 50px;
            background: rgba(255,255,255,0.2);
            transition: background 0.3s;
        }
        .nav-menu a:hover { background: rgba(255,255,255,0.3); }
        
        footer {
            text-align: center;
            padding: 30px;
            color: white;
            margin-top: 50px;
        }
        
        @media (max-width: 768px) {
            .stats-grid { grid-template-columns: 1fr; }
            .header h1 { font-size: 1.8rem; }
            .prediction-value { font-size: 3rem; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎲 {{ BOT_NAME }}</h1>
            <p>Hệ thống dự đoán MD5 Tài Xỉu chính xác cao</p>
        </div>
        
        <div class="nav-menu">
            <a href="/">🏠 Trang chủ</a>
            <a href="/admin">⚙️ Admin Panel</a>
            <a href="/history">📊 Lịch sử</a>
            <a href="/guide">📖 Hướng dẫn</a>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="value">{{ total_games }}</div>
                <div class="label">Tổng ván đấu</div>
            </div>
            <div class="stat-card">
                <div class="value">{{ total_users }}</div>
                <div class="label">Người chơi</div>
            </div>
            <div class="stat-card">
                <div class="value">{{ win_rate }}%</div>
                <div class="label">Tỷ lệ dự đoán đúng</div>
            </div>
            <div class="stat-card">
                <div class="value">{{ current_balance }}đ</div>
                <div class="label">Tổng cược hiện tại</div>
            </div>
        </div>
        
        <div class="prediction-section">
            <h2 style="text-align: center; margin-bottom: 30px;">🎯 DỰ ĐOÁN VÁN TIẾP THEO</h2>
            <div class="prediction-box">
                <h2>Dự đoán: <span class="prediction-value">{{ prediction.prediction }}</span></h2>
                <p style="color: white; margin-top: 20px; font-size: 1.2rem;">Độ tin cậy: {{ prediction.confidence }}%</p>
                <div class="confidence-bar">
                    <div class="confidence-fill" style="width: {{ prediction.confidence }}%"></div>
                </div>
            </div>
            
            <div style="margin-top: 20px; text-align: center;">
                <a href="/predict" class="btn btn-primary">🔄 Dự đoán mới</a>
            </div>
        </div>
        
        <div class="prediction-section">
            <h2 style="margin-bottom: 20px;">📊 PHÂN TÍCH CHI TIẾT MD5</h2>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                <div class="stat-card">
                    <div class="value">{{ analysis.md5_analysis.first_digit }}</div>
                    <div class="label">Chữ số đầu</div>
                </div>
                <div class="stat-card">
                    <div class="value">{{ analysis.md5_analysis.last_digit }}</div>
                    <div class="label">Chữ số cuối</div>
                </div>
                <div class="stat-card">
                    <div class="value">{{ analysis.md5_analysis.sum_digits }}</div>
                    <div class="label">Tổng chữ số</div>
                </div>
                <div class="stat-card">
                    <div class="value">{{ analysis.md5_analysis.score }}</div>
                    <div class="label">Điểm dự đoán</div>
                </div>
            </div>
            
            {% if analysis.pattern_analysis.patterns %}
            <h3 style="margin-top: 20px;">📈 MẪU CẦU PHÁT HIỆN:</h3>
            <ul style="margin-top: 10px; padding-left: 20px;">
                {% for pattern in analysis.pattern_analysis.patterns %}
                <li style="margin-bottom: 10px;">
                    <strong>{{ pattern.type }}</strong> - {{ pattern.description }} 
                    <span style="color: #44bd32;">({{ pattern.confidence }}% confidence)</span>
                </li>
                {% endfor %}
            </ul>
            {% endif %}
        </div>
        
        <div class="prediction-section">
            <h2 style="margin-bottom: 20px;">📋 LỊCH SỬ 10 VÁN GẦN NHẤT</h2>
            <table class="history-table">
                <thead>
                    <tr>
                        <th>#</th>
                        <th>MD5 Hash</th>
                        <th>Kết quả</th>
                        <th>Thời gian</th>
                    </tr>
                </thead>
                <tbody>
                    {% for game in recent_games %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td style="font-family: monospace;">{{ game.md5_hash[:16] }}...</td>
                        <td>
                            <span style="padding: 5px 10px; border-radius: 5px; {% if game.result == 'Tài' %}background: #f5576c; color: white;{% else %}background: #4ecdc4; color: white;{% endif %}">
                                {{ game.result }}
                            </span>
                        </td>
                        <td>{{ game.created_at }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        
        <footer>
            <p>© 2024 {{ BOT_NAME }} - All rights reserved</p>
            <p style="margin-top: 10px; font-size: 0.9rem;">Chơi có trách nhiệm. Đây là game giải trí.</p>
        </footer>
    </div>
</body>
</html>
'''

ADMIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⚙️ Admin Panel - {{ BOT_NAME }}</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #eee;
        }
        .container { max-width: 1000px; margin: 0 auto; padding: 20px; }
        .header {
            background: linear-gradient(135deg, #e94560, #533483);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 30px;
            text-align: center;
        }
        .header h1 { font-size: 2rem; }
        .card {
            background: rgba(255,255,255,0.1);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            backdrop-filter: blur(10px);
        }
        .card h3 { margin-bottom: 15px; color: #e94560; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; }
        .form-group input, .form-group select {
            width: 100%;
            padding: 10px;
            border: none;
            border-radius: 8px;
            background: rgba(255,255,255,0.2);
            color: white;
            font-size: 1rem;
        }
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s;
        }
        .btn-primary { background: #e94560; color: white; }
        .btn-primary:hover { background: #c23152; }
        .btn-success { background: #44bd32; color: white; }
        .btn-danger { background: #f5576c; color: white; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.1); }
        th { color: #e94560; }
        .nav { display: flex; gap: 10px; margin-bottom: 30px; }
        .nav a {
            color: white;
            text-decoration: none;
            padding: 10px 20px;
            background: rgba(255,255,255,0.2);
            border-radius: 8px;
        }
        .nav a:hover { background: rgba(255,255,255,0.4); }
        .alert { padding: 15px; border-radius: 8px; margin-bottom: 20px; }
        .alert-success { background: #44bd32; color: white; }
        .alert-danger { background: #f5576c; color: white; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚙️ ADMIN PANEL</h1>
            <p>{{ BOT_NAME }}</p>
        </div>
        
        <div class="nav">
            <a href="/">🏠 Trang chủ</a>
            <a href="/admin">⚙️ Admin</a>
            <a href="/history">📊 Lịch sử</a>
        </div>
        
        {% if message %}
        <div class="alert alert-success">{{ message }}</div>
        {% endif %}
        
        {% if error %}
        <div class="alert alert-danger">{{ error }}</div>
        {% endif %}
        
        <div class="card">
            <h3>📊 Thống kê hệ thống</h3>
            <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 20px;">
                <div style="text-align: center;">
                    <div style="font-size: 2rem; color: #e94560;">{{ total_users }}</div>
                    <div style="color: #aaa;">Người dùng</div>
                </div>
                <div style="text-align: center;">
                    <div style="font-size: 2rem; color: #44bd32;">{{ total_games }}</div>
                    <div style="color: #aaa;">Ván đấu</div>
                </div>
                <div style="text-align: center;">
                    <div style="font-size: 2rem; color: #3498db;">{{ total_bets }}</div>
                    <div style="color: #aaa;">Lượt cược</div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h3>🏦 Cấu hình ngân hàng</h3>
            <form method="POST" action="/admin">
                <div class="form-group">
                    <label>Mã ngân hàng</label>
                    <input type="text" name="bank_code" value="{{ bank.bank_code }}">
                </div>
                <div class="form-group">
                    <label>Tên ngân hàng</label>
                    <input type="text" name="bank_name" value="{{ bank.bank_name }}">
                </div>
                <div class="form-group">
                    <label>Số tài khoản</label>
                    <input type="text" name="account_number" value="{{ bank.account_number }}">
                </div>
                <div class="form-group">
                    <label>Tên tài khoản</label>
                    <input type="text" name="account_name" value="{{ bank.account_name }}">
                </div>
                <button type="submit" class="btn btn-primary">💾 Lưu cấu hình</button>
            </form>
        </div>
        
        <div class="card">
            <h3>👥 Quản lý người dùng</h3>
            <table>
                <thead>
                    <tr>
                        <th>User ID</th>
                        <th>Username</th>
                        <th>Số dư</th>
                        <th>Vai trò</th>
                        <th>Thao tác</th>
                    </tr>
                </thead>
                <tbody>
                    {% for user in users %}
                    <tr>
                        <td>{{ user.user_id }}</td>
                        <td>{{ user.username or 'N/A' }}</td>
                        <td>{{ user.balance }}đ</td>
                        <td>{% if user.is_admin %}👑 Admin{% else %}👤 User{% endif %}</td>
                        <td>
                            <a href="/admin/balance/{{ user.user_id }}" class="btn btn-success">💰 Cộng tiền</a>
                            {% if not user.is_admin %}
                            <a href="/admin/toggle_admin/{{ user.user_id }}" class="btn btn-primary">👑 Thêm admin</a>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        
        <div class="card">
            <h3>📥 Khôi phục dữ liệu</h3>
            <form method="POST" action="/admin/restore" enctype="multipart/form-data">
                <div class="form-group">
                    <label>File backup (.sqlite3 hoặc .json)</label>
                    <input type="file" name="backup_file" accept=".sqlite3,.json">
                </div>
                <button type="submit" class="btn btn-primary">🔄 Khôi phục</button>
            </form>
        </div>
        
        <div class="card">
            <h3>📤 Backup dữ liệu</h3>
            <a href="/admin/backup" class="btn btn-success">⬇️ Tải backup</a>
        </div>
        
        <footer style="text-align: center; margin-top: 30px; color: #666;">
            <p>© 2024 {{ BOT_NAME }} Admin</p>
        </footer>
    </div>
</body>
</html>
'''

HISTORY_TEMPLATE = '''
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📊 Lịch sử - {{ BOT_NAME }}</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 30px;
            color: white;
            text-align: center;
        }
        .nav { display: flex; gap: 10px; margin-bottom: 30px; }
        .nav a {
            color: white;
            text-decoration: none;
            padding: 10px 20px;
            background: rgba(255,255,255,0.2);
            border-radius: 8px;
            transition: background 0.3s;
        }
        .nav a:hover { background: rgba(255,255,255,0.4); }
        .card {
            background: white;
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        table { width: 100%; border-collapse: collapse; }
        th { background: #764ba2; color: white; padding: 12px; text-align: left; }
        td { padding: 12px; border-bottom: 1px solid #eee; }
        tr:hover { background: #f8f9fa; }
        .badge {
            padding: 5px 10px;
            border-radius: 5px;
            font-weight: bold;
        }
        .badge-tai { background: #f5576c; color: white; }
        .badge-xiu { background: #4ecdc4; color: white; }
        .pagination { display: flex; justify-content: center; gap: 10px; margin-top: 20px; }
        .pagination a {
            padding: 8px 16px;
            background: white;
            border-radius: 8px;
            text-decoration: none;
            color: #333;
            transition: background 0.3s;
        }
        .pagination a:hover { background: #764ba2; color: white; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 LỊCH SỬ VÁN ĐẤU</h1>
        </div>
        
        <div class="nav">
            <a href="/">🏠 Trang chủ</a>
            <a href="/admin">⚙️ Admin</a>
            <a href="/history">📊 Lịch sử</a>
        </div>
        
        <div class="card">
            <h3 style="margin-bottom: 20px; color: #764ba2;">Tất cả ván đấu</h3>
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Game ID</th>
                        <th>MD5 Hash</th>
                        <th>Kết quả</th>
                        <th>Thời gian</th>
                    </tr>
                </thead>
                <tbody>
                    {% for game in games %}
                    <tr>
                        <td>{{ loop.index + (page * per_page) }}</td>
                        <td style="font-family: monospace;">{{ game.game_id }}</td>
                        <td style="font-family: monospace;">{{ game.md5_hash[:16] }}...</td>
                        <td>
                            <span class="badge {% if game.result == 'Tài' %}badge-tai{% else %}badge-xiu{% endif %}">
                                {{ game.result }}
                            </span>
                        </td>
                        <td>{{ game.created_at }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            
            <div class="pagination">
                {% if page > 0 %}
                <a href="/history?page={{ page - 1 }}">← Trước</a>
                {% endif %}
                <span style="padding: 8px 16px; background: #eee; border-radius: 8px;">Trang {{ page + 1 }}</span>
                {% if games|length == per_page %}
                <a href="/history?page={{ page + 1 }}">Sau →</a>
                {% endif %}
            </div>
        </div>
        
        <footer style="text-align: center; padding: 30px; color: white;">
            <p>© 2024 {{ BOT_NAME }}</p>
        </footer>
    </div>
</body>
</html>
'''

# ============================================================
# ROUTES FLASK
# ============================================================
@app.route('/')
def home():
    """Trang chủ với giao diện mới"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Lấy thống kê
    cursor.execute('SELECT COUNT(*) FROM games')
    total_games = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM bets WHERE result IS NOT NULL')
    total_bets = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM bets WHERE result = "win"')
    total_wins = cursor.fetchone()[0]
    win_rate = round((total_wins / total_bets * 100), 1) if total_bets > 0 else 0
    
    cursor.execute('SELECT COALESCE(SUM(amount), 0) FROM bets WHERE status = "pending"')
    current_balance = cursor.fetchone()[0]
    
    # Tạo dự đoán mới
    latest_md5 = cursor.execute('SELECT md5_hash FROM games ORDER BY created_at DESC LIMIT 1').fetchone()
    
    # Lấy lịch sử kết quả
    cursor.execute('SELECT result FROM games ORDER BY created_at DESC LIMIT 20')
    history = [row['result'] for row in cursor.fetchall()]
    
    # Tạo MD5 giả lập cho dự đoán nếu chưa có
    mock_md5 = latest_md5['md5_hash'] if latest_md5 else md5_analyzer.generate_mock_md5()
    
    # Phân tích MD5
    analysis = md5_analyzer.combined_prediction(mock_md5, history)
    
    # Lấy 10 ván gần nhất
    cursor.execute('SELECT * FROM games ORDER BY created_at DESC LIMIT 10')
    recent_games = cursor.fetchall()
    
    conn.close()
    
    return render_template_string(
        HOME_TEMPLATE,
        BOT_NAME=BOT_NAME,
        total_games=total_games,
        total_users=total_users,
        win_rate=win_rate,
        current_balance=current_balance,
        prediction=analysis,
        analysis=analysis,
        recent_games=recent_games
    )

@app.route('/predict')
def predict():
    """Route dự đoán mới"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Lấy lịch sử
    cursor.execute('SELECT result FROM games ORDER BY created_at DESC LIMIT 20')
    history = [row['result'] for row in cursor.fetchall()]
    
    # Tạo MD5 mới
    new_md5 = md5_analyzer.generate_mock_md5()
    
    # Phân tích
    analysis = md5_analyzer.combined_prediction(new_md5, history)
    
    # Lưu dự đoán
    cursor.execute('''
        INSERT INTO predictions (game_id, prediction_data, accuracy)
        VALUES (?, ?, ?)
    ''', (f"PRED-{int(time.time())}", json.dumps(analysis), analysis['confidence']))
    
    conn.commit()
    conn.close()
    
    return render_template_string(
        HOME_TEMPLATE,
        BOT_NAME=BOT_NAME,
        total_games=0,
        total_users=0,
        win_rate=0,
        current_balance=0,
        prediction=analysis,
        analysis=analysis,
        recent_games=[]
    )

@app.route('/history')
def history():
    """Trang lịch sử"""
    page = int(request.args.get('page', 0))
    per_page = 50
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM games 
        ORDER BY created_at DESC 
        LIMIT ? OFFSET ?
    ''', (per_page, page * per_page))
    games = cursor.fetchall()
    
    conn.close()
    
    return render_template_string(
        HISTORY_TEMPLATE,
        BOT_NAME=BOT_NAME,
        games=games,
        page=page,
        per_page=per_page
    )

@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    """Admin panel"""
    # Kiểm tra đăng nhập qua session
    if 'admin_logged_in' not in session:
        # Kiểm tra qua query param (đơn giản)
        admin_key = request.args.get('key', '')
        if admin_key == os.getenv('ADMIN_KEY', 'admin123'):
            session['admin_logged_in'] = True
        else:
            return redirect('/login')
    
    if request.method == 'POST':
        # Lưu cấu hình ngân hàng
        bank_code = request.form.get('bank_code', '')
        bank_name = request.form.get('bank_name', '')
        account_number = request.form.get('account_number', '')
        account_name = request.form.get('account_name', '')
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Lưu settings
        settings = {
            'bank_code': bank_code,
            'bank_name': bank_name,
            'account_number': account_number,
            'account_name': account_name
        }
        
        for key, value in settings.items():
            cursor.execute('''
                INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)
            ''', (key, value))
        
        conn.commit()
        conn.close()
        
        return render_template_string(
            ADMIN_TEMPLATE,
            BOT_NAME=BOT_NAME,
            message="✅ Đã lưu cấu hình ngân hàng thành công!",
            error=None,
            total_users=0,
            total_games=0,
            total_bets=0,
            bank=settings,
            users=[]
        )
    
    # GET request
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM games')
    total_games = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM bets')
    total_bets = cursor.fetchone()[0]
    
    # Lấy cấu hình ngân hàng
    cursor.execute('SELECT key, value FROM settings WHERE key IN ("bank_code", "bank_name", "account_number", "account_name")')
    settings_rows = cursor.fetchall()
    bank = {row['key']: row['value'] for row in settings_rows}
    
    if not bank:
        bank = DEFAULT_BANK
    
    # Lấy danh sách users
    cursor.execute('SELECT * FROM users ORDER BY balance DESC LIMIT 50')
    users = cursor.fetchall()
    
    conn.close()
    
    return render_template_string(
        ADMIN_TEMPLATE,
        BOT_NAME=BOT_NAME,
        message=None,
        error=None,
        total_users=total_users,
        total_games=total_games,
        total_bets=total_bets,
        bank=bank,
        users=users
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Trang đăng nhập admin"""
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == os.getenv('ADMIN_PASSWORD', 'admin123'):
            session['admin_logged_in'] = True
            return redirect('/admin')
        else:
            return render_template_string('''
                <html><body style="font-family: Arial; text-align: center; padding: 50px;">
                <h1 style="color: red;">❌ Sai mật khẩu!</h1>
                <a href="/login">Thử lại</a>
                </body></html>
            ''')
    
    return render_template_string('''
        <!DOCTYPE html>
        <html lang="vi">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Đăng nhập Admin</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                }
                .login-box {
                    background: white;
                    padding: 40px;
                    border-radius: 15px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.3);
                    text-align: center;
                    max-width: 400px;
                    width: 100%;
                }
                h1 { color: #764ba2; margin-bottom: 30px; }
                input {
                    width: 100%;
                    padding: 12px;
                    margin: 10px 0;
                    border: 2px solid #ddd;
                    border-radius: 8px;
                    font-size: 1rem;
                }
                button {
                    width: 100%;
                    padding: 12px;
                    background: #764ba2;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    font-size: 1rem;
                    cursor: pointer;
                    margin-top: 20px;
                }
                button:hover { background: #5a3a7e; }
            </style>
        </head>
        <body>
            <div class="login-box">
                <h1>🔐 Admin Login</h1>
                <form method="POST">
                    <input type="password" name="password" placeholder="Mật khẩu" required>
                    <button type="submit">Đăng nhập</button>
                </form>
            </div>
        </body>
        </html>
    ''')

@app.route('/admin/backup')
def admin_backup():
    """Backup database"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Lấy toàn bộ dữ liệu
    data = {}
    
    tables = ['users', 'games', 'bets', 'transactions', 'settings', 'predictions', 'patterns']
    for table in tables:
        try:
            cursor.execute(f'SELECT * FROM {table}')
            data[table] = [dict(row) for row in cursor.fetchall()]
        except:
            data[table] = []
    
    conn.close()
    
    # Tạo file backup
    backup_file = f"backup_{int(time.time())}.json"
    with open(backup_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    return send_file(backup_file, as_attachment=True)

@app.route('/admin/restore', methods=['POST'])
def admin_restore():
    """Khôi phục dữ liệu từ backup"""
    try:
        if 'backup_file' not in request.files:
            return render_template_string(
                ADMIN_TEMPLATE,
                BOT_NAME=BOT_NAME,
                message=None,
                error="❌ Không có file backup được tải lên!",
                total_users=0,
                total_games=0,
                total_bets=0,
                bank=DEFAULT_BANK,
                users=[]
            )
        
        file = request.files['backup_file']
        if file.filename == '':
            return render_template_string(
                ADMIN_TEMPLATE,
                BOT_NAME=BOT_NAME,
                message=None,
                error="❌ File rỗng!",
                total_users=0,
                total_games=0,
                total_bets=0,
                bank=DEFAULT_BANK,
                users=[]
            )
        
        # Đọc dữ liệu từ file
        content = file.read().decode('utf-8')
        
        # Kiểm tra định dạng file
        if file.filename.endswith('.json'):
            data = json.loads(content)
        else:
            # Nếu là file sqlite3, xử lý khác
            return render_template_string(
                ADMIN_TEMPLATE,
                BOT_NAME=BOT_NAME,
                message=None,
                error="❌ Chỉ hỗ trợ file .json!",
                total_users=0,
                total_games=0,
                total_bets=0,
                bank=DEFAULT_BANK,
                users=[]
            )
        
        # FIX LỖI: Kiểm tra và xử lý dữ liệu trước khi khôi phục
        conn = get_db()
        cursor = conn.cursor()
        
        # Kiểm tra cấu trúc dữ liệu
        valid_tables = ['users', 'games', 'bets', 'transactions', 'settings', 'predictions', 'patterns']
        
        for table in valid_tables:
            if table not in data:
                continue
            
            records = data[table]
            if not records or not isinstance(records, list):
                continue
            
            # Xóa dữ liệu cũ
            try:
                cursor.execute(f'DELETE FROM {table}')
            except:
                pass
            
            # Khôi phục từng bản ghi
            for record in records:
                try:
                    # Lấy danh sách cột từ record
                    columns = list(record.keys())
                    placeholders = ','.join(['?'] * len(columns))
                    column_names = ','.join(columns)
                    
                    query = f'INSERT OR IGNORE INTO {table} ({column_names}) VALUES ({placeholders})'
                    cursor.execute(query, [record[col] for col in columns])
                except Exception as e:
                    logger.warning(f"Lỗi khi khôi phục bản ghi trong bảng {table}: {e}")
                    continue
        
        conn.commit()
        conn.close()
        
        return render_template_string(
            ADMIN_TEMPLATE,
            BOT_NAME=BOT_NAME,
            message="✅ Khôi phục dữ liệu thành công!",
            error=None,
            total_users=0,
            total_games=0,
            total_bets=0,
            bank=DEFAULT_BANK,
            users=[]
        )
    
    except Exception as e:
        logger.error(f"Lỗi khôi phục dữ liệu: {e}")
        return render_template_string(
            ADMIN_TEMPLATE,
            BOT_NAME=BOT_NAME,
            message=None,
            error=f"❌ Lỗi khi khôi phục dữ liệu: {str(e)}",
            total_users=0,
            total_games=0,
            total_bets=0,
            bank=DEFAULT_BANK,
            users=[]
        )

@app.route('/admin/balance/<int:user_id>')
def admin_add_balance(user_id):
    """Cộng tiền cho người dùng"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Cộng 100k mặc định
    cursor.execute('UPDATE users SET balance = balance + 100000 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()
    
    return redirect('/admin?message=Đã cộng 100k cho user ' + str(user_id))

@app.route('/admin/toggle_admin/<int:user_id>')
def admin_toggle_admin(user_id):
    """Thêm/gỡ admin"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_admin = CASE WHEN is_admin = 1 THEN 0 ELSE 1 END WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()
    
    return redirect('/admin?message=Đã cập nhật quyền admin cho user ' + str(user_id))

# ============================================================
# TELEGRAM BOT HANDLERS
# ============================================================
@bot.message_handler(commands=['start', 'help'])
def start_command(message):
    """Lệnh start/help"""
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    # Đăng ký user mới
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)
    ''', (user_id, username))
    conn.commit()
    conn.close()
    
    welcome_text = f"""
🎲 Chào mừng bạn đến với {BOT_NAME}!

📊 Hệ thống dự đoán MD5 Tài Xỉu thông minh

💡 Các lệnh:
- /start - Bắt đầu
- /balance - Xem số dư
- /nap - Nạp tiền
- /rut - Rút tiền
- /play - Chơi game
- /history - Xem lịch sử
- /predict - Dự đoán ván tiếp theo

🎯 Chúc bạn chơi vui vẻ và thắng lớn!
    """
    
    bot.send_message(message.chat.id, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['balance'])
def balance_command(message):
    """Xem số dư"""
    user_id = message.from_user.id
    balance = get_user_balance(user_id)
    
    bot.send_message(
        message.chat.id,
        f"💰 Số dư của bạn: **{balance:,}đ**",
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['play'])
def play_command(message):
    """Chơi game"""
    user_id = message.from_user.id
    
    # Kiểm tra số dư
    balance = get_user_balance(user_id)
    if balance < 10000:
        bot.send_message(message.chat.id, "❌ Số dư không đủ! Vui lòng nạp thêm tiền.")
        return
    
    # Tạo bàn chơi
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🀄 TÀI", callback_data="bet_tai"),
        types.InlineKeyboardButton("🀄 XỈU", callback_data="bet_xiu")
    )
    
    bot.send_message(
        message.chat.id,
        "🎲 **Chọn Tài hoặc Xỉu**\n\n💵 Mức cược tối thiểu: 10,000đ\n\nChọn loại cược:",
        reply_markup=markup,
        parse_mode='Markdown'
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('bet_'))
def bet_callback(call):
    """Xử lý đặt cược"""
    user_id = call.from_user.id
    choice = call.data.split('_')[1]
    
    # Yêu cầu nhập số tiền
    bot.send_message(
        call.message.chat.id,
        f"💰 Nhập số tiền cược cho **{choice.upper()}**:",
        parse_mode='Markdown'
    )
    
    # Lưu tạm lựa chọn
    bot.register_next_step_handler(call.message, process_bet_amount, choice)

def process_bet_amount(message, choice):
    """Xử lý số tiền cược"""
    try:
        amount = int(message.text.replace(',', '').replace('.', ''))
        user_id = message.from_user.id
        
        if amount < 10000:
            bot.send_message(message.chat.id, "❌ Số tiền tối thiểu là 10,000đ!")
            return
        
        balance = get_user_balance(user_id)
        if amount > balance:
            bot.send_message(message.chat.id, "❌ Số dư không đủ!")
            return
        
        # Trừ tiền
        update_user_balance(user_id, -amount)
        
        # Tạo game mới
        conn = get_db()
        cursor = conn.cursor()
        
        # Tạo MD5 mới
        md5_hash = md5_analyzer.generate_mock_md5()
        game_id = f"GAME-{int(time.time())}"
        
        # Lưu game
        cursor.execute('''
            INSERT INTO games (game_id, md5_hash, result)
            VALUES (?, ?, ?)
        ''', (game_id, md5_hash, None))
        
        # Lưu cược
        cursor.execute('''
            INSERT INTO bets (bet_id, user_id, game_id, amount, choice)
            VALUES (?, ?, ?, ?, ?)
        ''', (f"BET-{int(time.time())}", user_id, game_id, amount, choice))
        
        conn.commit()
        
        # Tính kết quả
        result = md5_analyzer.analyze_md5(md5_hash)['prediction']
        
        # Cập nhật kết quả game
        cursor.execute('UPDATE games SET result = ? WHERE game_id = ?', (result, game_id))
        
        # Kiểm tra thắng/thua
        if result == choice:
            win_amount = amount * 2
            update_user_balance(user_id, win_amount)
            cursor.execute('UPDATE bets SET result = "win", win_amount = ?, status = "completed" WHERE game_id = ?', (win_amount, game_id))
            
            bot.send_message(
                message.chat.id,
                f"🎉 **CHÚC MỪNG!**\n\n"
                f"🎲 Kết quả: **{result}**\n"
                f"💰 Bạn thắng: **{win_amount:,}đ**\n"
                f"💵 Số dư hiện tại: **{get_user_balance(user_id):,}đ**",
                parse_mode='Markdown'
            )
        else:
            cursor.execute('UPDATE bets SET result = "lose", win_amount = 0, status = "completed" WHERE game_id = ?', (game_id,))
            
            bot.send_message(
                message.chat.id,
                f"😢 **Rất tiếc!**\n\n"
                f"🎲 Kết quả: **{result}**\n"
                f"💰 Bạn thua: **{amount:,}đ**\n"
                f"💵 Số dư hiện tại: **{get_user_balance(user_id):,}đ**",
                parse_mode='Markdown'
            )
        
        conn.close()
    
    except ValueError:
        bot.send_message(message.chat.id, "❌ Vui lòng nhập số hợp lệ!")
    except Exception as e:
        logger.error(f"Lỗi xử lý cược: {e}")
        bot.send_message(message.chat.id, f"❌ Đã xảy ra lỗi: {str(e)}")

@bot.message_handler(commands=['predict'])
def predict_command(message):
    """Dự đoán ván tiếp theo"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Lấy lịch sử
    cursor.execute('SELECT result FROM games ORDER BY created_at DESC LIMIT 20')
    history = [row['result'] for row in cursor.fetchall()]
    
    # Tạo MD5 mới
    new_md5 = md5_analyzer.generate_mock_md5()
    
    # Phân tích
    analysis = md5_analyzer.combined_prediction(new_md5, history)
    
    bot.send_message(
        message.chat.id,
        f"🎯 **DỰ ĐOÁN VÁN TIẾP THEO**\n\n"
        f"📊 Kết quả dự đoán: **{analysis['prediction']}**\n"
        f"🎯 Độ tin cậy: **{analysis['confidence'] * 100:.1f}%**\n\n"
        f"📈 Mẫu cầu phát hiện: {len(analysis['pattern_analysis']['patterns'])} mẫu\n"
        f"🔍 Điểm MD5: **{analysis['md5_analysis']['score']}**",
        parse_mode='Markdown'
    )

# ============================================================
# KHỞI ĐỘNG WEBHOOK VÀ BOT
# ============================================================
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Webhook cho Telegram"""
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return jsonify({'status': 'ok'}), 200

def start_bot_polling():
    """Khởi động bot polling"""
    try:
        bot.polling(non_stop=True, timeout=60)
    except Exception as e:
        logger.error(f"Lỗi polling: {e}")
        time.sleep(5)
        start_bot_polling()

if __name__ == '__main__':
    # Đăng ký webhook
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f'https://{os.getenv("RENDER_EXTERNAL_HOSTNAME", "localhost")}/{BOT_TOKEN}')
        logger.info("Đã đăng ký webhook")
    except Exception as e:
        logger.warning(f"Không thể đăng ký webhook, sử dụng polling: {e}")
        # Khởi động polling trong thread riêng
        polling_thread = threading.Thread(target=start_bot_polling)
        polling_thread.daemon = True
        polling_thread.start()
    
    # Khởi động Flask
    app.run(host='0.0.0.0', port=PORT, debug=False)
