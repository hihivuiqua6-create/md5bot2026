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
import numpy as np
from datetime import datetime, timedelta, timezone
from collections import Counter, deque
from flask import Flask, request, redirect, render_template_string, jsonify, session
import telebot
from telebot import types

# ============================================================
# CẤU HÌNH QUA BIẾN MÔI TRƯỜNG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8477166662:AAHpUmD1-p9iPWIyvhKy_5I9Hc7sQkjwbU0").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "8030294480").split(",") if x.strip().lstrip("-").isdigit()}
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")
BOT_NAME = os.getenv("BOT_NAME", "MD5 Tài Xỉu Pro Max")

# ============================================================
# KHỞI TẠO LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ============================================================
# KHỞI TẠO DATABASE
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Bảng lịch sử kết quả
    c.execute('''CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        result TEXT NOT NULL,
        md5_hash TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Bảng cầu dự đoán
    c.execute('''CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction TEXT NOT NULL,
        confidence REAL,
        pattern TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Bảng thống kê
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized successfully")

init_db()

# ============================================================
# THUẬT TOÁN DỰ ĐOÁN SIÊU VIỆT - PHÂN TÍCH ĐA CHIỀU
# ============================================================
class PredictionEngine:
    def __init__(self):
        self.history = deque(maxlen=1000)
        self.patterns = {}
        self.weights = {}
        self.load_history()
        logger.info("🧠 Prediction Engine initialized")
    
    def load_history(self):
        """Tải lịch sử từ database"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT result, timestamp FROM history ORDER BY timestamp DESC LIMIT 500")
            rows = c.fetchall()
            for row in rows:
                self.history.append(row[0])
            conn.close()
            logger.info(f"📊 Loaded {len(self.history)} history records")
        except Exception as e:
            logger.error(f"❌ Error loading history: {e}")
    
    def add_result(self, result):
        """Thêm kết quả mới vào lịch sử"""
        self.history.append(result)
        self.update_patterns(result)
        logger.info(f"📝 Added result: {result}")
    
    def update_patterns(self, result):
        """Cập nhật mẫu cầu"""
        if len(self.history) >= 3:
            # Phân tích theo các cặp
            pairs = []
            for i in range(min(10, len(self.history) - 1)):
                pair = self.history[i] + self.history[i+1]
                pairs.append(pair)
            
            # Cập nhật trọng số cho các mẫu
            pattern_key = ''.join(self.history[-5:]) if len(self.history) >= 5 else ''.join(self.history)
            if pattern_key not in self.patterns:
                self.patterns[pattern_key] = {'Tài': 0, 'Xỉu': 0}
            self.patterns[pattern_key][result] = self.patterns[pattern_key].get(result, 0) + 1
    
    def calculate_md5(self, text):
        """Tính MD5 và chuyển thành dự đoán"""
        md5_hash = hashlib.md5(text.encode()).hexdigest()
        # Lấy 2 ký tự cuối của MD5 để dự đoán
        last_two = md5_hash[-2:]
        # Chuyển hex sang số
        try:
            num = int(last_two, 16)
        except:
            num = 0
        return 'Tài' if num % 2 == 0 else 'Xỉu', md5_hash
    
    def analyze_patterns(self):
        """Phân tích mẫu cầu từ lịch sử"""
        if len(self.history) < 3:
            return None
        
        analysis = {
            'recent': ''.join(self.history[-10:]),
            'trend': self.get_trend(),
            'patterns': self.find_patterns(),
            'cycle': self.find_cycle()
        }
        return analysis
    
    def get_trend(self):
        """Lấy xu hướng hiện tại"""
        if len(self.history) < 10:
            return None
        
        recent = self.history[-10:]
        tai_count = recent.count('Tài')
        xiu_count = recent.count('Xỉu')
        
        if tai_count > xiu_count:
            return 'Tài' if tai_count - xiu_count >= 3 else 'Cân bằng nghiêng Tài'
        elif xiu_count > tai_count:
            return 'Xỉu' if xiu_count - tai_count >= 3 else 'Cân bằng nghiêng Xỉu'
        else:
            return 'Cân bằng tuyệt đối'
    
    def find_patterns(self):
        """Tìm các mẫu cầu lặp lại"""
        patterns = []
        for length in range(2, 6):
            if len(self.history) >= length * 2:
                pattern = ''.join(self.history[:length])
                count = sum(1 for i in range(len(self.history) - length) 
                           if ''.join(self.history[i:i+length]) == pattern)
                if count >= 2:
                    patterns.append({
                        'pattern': pattern,
                        'length': length,
                        'count': count,
                        'next': self.predict_next(pattern)
                    })
        return patterns
    
    def find_cycle(self):
        """Tìm chu kỳ"""
        if len(self.history) < 20:
            return None
        
        # Tìm chu kỳ lặp lại
        sequence = ''.join(self.history)
        for period in range(2, 10):
            if len(sequence) >= period * 2:
                if sequence[:period] == sequence[period:period*2]:
                    return {
                        'period': period,
                        'pattern': sequence[:period],
                        'next': self.predict_next(sequence[:period])
                    }
        return None
    
    def predict_next(self, pattern=None):
        """Dự đoán kết quả tiếp theo với độ chính xác cao"""
        if not pattern and len(self.history) > 0:
            pattern = ''.join(self.history[-5:]) if len(self.history) >= 5 else ''.join(self.history)
        
        # 1. Dùng MD5 để dự đoán
        md5_pred, md5_hash = self.calculate_md5(str(time.time()) + pattern)
        
        # 2. Phân tích mẫu cầu
        pattern_analysis = self.analyze_patterns()
        
        # 3. Tính xác suất dựa trên các yếu tố
        scores = {'Tài': 0, 'Xỉu': 0}
        weights = {'md5': 0.3, 'trend': 0.25, 'pattern': 0.25, 'cycle': 0.2}
        
        # MD5 score
        scores[md5_pred] += weights['md5'] * 2
        
        # Trend score
        trend = self.get_trend()
        if trend:
            if 'Tài' in trend:
                scores['Tài'] += weights['trend'] * 1.5
            elif 'Xỉu' in trend:
                scores['Xỉu'] += weights['trend'] * 1.5
            else:
                scores['Tài'] += weights['trend']
                scores['Xỉu'] += weights['trend']
        
        # Pattern score
        patterns = self.find_patterns()
        if patterns:
            for p in patterns:
                if p['next'] in scores:
                    scores[p['next']] += weights['pattern'] * min(p['count'] / 2, 1)
        
        # Cycle score
        cycle = self.find_cycle()
        if cycle and cycle['next'] in scores:
            scores[cycle['next']] += weights['cycle'] * 1.5
        
        # Tính độ tin cậy
        total = sum(scores.values())
        if total > 0:
            confidence = {
                'Tài': scores['Tài'] / total,
                'Xỉu': scores['Xỉu'] / total
            }
        else:
            confidence = {'Tài': 0.5, 'Xỉu': 0.5}
        
        # Chọn dự đoán cuối cùng
        final_pred = 'Tài' if confidence['Tài'] >= confidence['Xỉu'] else 'Xỉu'
        
        result = {
            'prediction': final_pred,
            'confidence': confidence[final_pred] * 100,
            'md5_hash': md5_hash,
            'md5_pred': md5_pred,
            'scores': scores,
            'analysis': pattern_analysis,
            'next_pattern': pattern
        }
        
        # Lưu vào database
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO predictions (prediction, confidence, pattern) VALUES (?, ?, ?)", 
                     (final_pred, result['confidence'], pattern))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"❌ Error saving prediction: {e}")
        
        return result
    
    def get_statistics(self):
        """Thống kê chi tiết"""
        if not self.history:
            return None
        
        total = len(self.history)
        tai_count = self.history.count('Tài')
        xiu_count = self.history.count('Xỉu')
        
        # Lấy 100 kết quả gần nhất
        recent = self.history[-100:] if len(self.history) > 100 else self.history
        recent_tai = recent.count('Tài')
        recent_xiu = recent.count('Xỉu')
        
        return {
            'total': total,
            'tai_count': tai_count,
            'xiu_count': xiu_count,
            'tai_rate': (tai_count / total * 100) if total > 0 else 0,
            'xiu_rate': (xiu_count / total * 100) if total > 0 else 0,
            'recent_tai': recent_tai,
            'recent_xiu': recent_xiu,
            'recent_rate': (recent_tai / len(recent) * 100) if recent else 0,
            'last_result': self.history[-1] if self.history else None
        }

# ============================================================
# KHỞI TẠO ENGINE DỰ ĐOÁN
# ============================================================
prediction_engine = PredictionEngine()

# ============================================================
# KHỞI TẠO FLASK APP
# ============================================================
app = Flask(__name__)
app.secret_key = os.urandom(24)

# ============================================================
# GIAO DIỆN WEB CAO CẤP - FULL HTML
# ============================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MD5 Tài Xỉu Pro Max - Dự Đoán Siêu Chính Xác</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Roboto:wght@300;400;700&display=swap');
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Roboto', sans-serif;
            background: linear-gradient(135deg, #0a0a2a 0%, #1a0a3e 50%, #0a0a2a 100%);
            min-height: 100vh;
            color: #fff;
            overflow-x: hidden;
        }
        
        /* Particle Background */
        #particles {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: 0;
            overflow: hidden;
        }
        
        .particle {
            position: absolute;
            width: 4px;
            height: 4px;
            background: rgba(255, 215, 0, 0.3);
            border-radius: 50%;
            animation: float linear infinite;
        }
        
        @keyframes float {
            0% { transform: translateY(100vh) rotate(0deg); opacity: 0; }
            10% { opacity: 1; }
            90% { opacity: 1; }
            100% { transform: translateY(-10vh) rotate(720deg); opacity: 0; }
        }
        
        .container {
            position: relative;
            z-index: 1;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        /* Header */
        .header {
            text-align: center;
            padding: 30px 0;
            background: linear-gradient(180deg, rgba(255,215,0,0.1) 0%, transparent 100%);
            border-bottom: 2px solid rgba(255,215,0,0.2);
            margin-bottom: 30px;
        }
        
        .header h1 {
            font-family: 'Orbitron', monospace;
            font-size: 3em;
            background: linear-gradient(135deg, #ffd700, #ff6b00, #ffd700);
            background-size: 300% 300%;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: gradientShift 3s ease infinite;
            text-shadow: 0 0 30px rgba(255,215,0,0.3);
        }
        
        @keyframes gradientShift {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        
        .header .subtitle {
            color: rgba(255,255,255,0.7);
            font-size: 1.2em;
            margin-top: 10px;
            letter-spacing: 3px;
        }
        
        /* Main Card */
        .main-card {
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(20px);
            border-radius: 30px;
            border: 1px solid rgba(255,215,0,0.15);
            padding: 40px;
            margin-bottom: 30px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            position: relative;
            overflow: hidden;
        }
        
        .main-card::before {
            content: '';
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: radial-gradient(circle, rgba(255,215,0,0.05) 0%, transparent 70%);
            animation: rotate 20s linear infinite;
        }
        
        @keyframes rotate {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        .prediction-area {
            text-align: center;
            position: relative;
            z-index: 1;
        }
        
        .prediction-result {
            display: inline-block;
            padding: 20px 60px;
            border-radius: 20px;
            font-family: 'Orbitron', monospace;
            font-size: 3.5em;
            font-weight: 900;
            margin: 20px 0;
            position: relative;
            transition: all 0.5s ease;
            animation: pulse 2s ease-in-out infinite;
        }
        
        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.02); }
        }
        
        .prediction-result.tai {
            background: linear-gradient(135deg, #ff6b00, #ff4500);
            box-shadow: 0 0 50px rgba(255,107,0,0.4);
            color: #fff;
        }
        
        .prediction-result.xiu {
            background: linear-gradient(135deg, #00bfff, #0066ff);
            box-shadow: 0 0 50px rgba(0,191,255,0.4);
            color: #fff;
        }
        
        .prediction-result .confidence {
            font-size: 0.4em;
            display: block;
            margin-top: 10px;
            opacity: 0.9;
        }
        
        /* Stats Grid */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }
        
        .stat-card {
            background: rgba(255,255,255,0.05);
            border-radius: 15px;
            padding: 20px;
            text-align: center;
            border: 1px solid rgba(255,255,255,0.05);
            transition: all 0.3s ease;
        }
        
        .stat-card:hover {
            transform: translateY(-5px);
            border-color: rgba(255,215,0,0.3);
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        }
        
        .stat-card .label {
            font-size: 0.8em;
            color: rgba(255,255,255,0.5);
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        
        .stat-card .value {
            font-size: 2em;
            font-weight: 700;
            margin-top: 10px;
            font-family: 'Orbitron', monospace;
        }
        
        .stat-card .value.tai-color { color: #ff6b00; }
        .stat-card .value.xiu-color { color: #00bfff; }
        .stat-card .value.gold { color: #ffd700; }
        
        /* Pattern Display */
        .pattern-display {
            margin: 30px 0;
            padding: 20px;
            background: rgba(0,0,0,0.3);
            border-radius: 15px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        
        .pattern-display h3 {
            color: rgba(255,255,255,0.7);
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 15px;
        }
        
        .pattern-balls {
            display: flex;
            gap: 15px;
            justify-content: center;
            flex-wrap: wrap;
        }
        
        .ball {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 1.2em;
            transition: all 0.3s ease;
            animation: bounceIn 0.5s ease forwards;
        }
        
        .ball.tai {
            background: linear-gradient(135deg, #ff6b00, #ff4500);
            color: #fff;
            box-shadow: 0 0 20px rgba(255,107,0,0.3);
        }
        
        .ball.xiu {
            background: linear-gradient(135deg, #00bfff, #0066ff);
            color: #fff;
            box-shadow: 0 0 20px rgba(0,191,255,0.3);
        }
        
        @keyframes bounceIn {
            0% { transform: scale(0) rotate(-180deg); opacity: 0; }
            100% { transform: scale(1) rotate(0deg); opacity: 1; }
        }
        
        .ball:nth-child(1) { animation-delay: 0.1s; }
        .ball:nth-child(2) { animation-delay: 0.2s; }
        .ball:nth-child(3) { animation-delay: 0.3s; }
        .ball:nth-child(4) { animation-delay: 0.4s; }
        .ball:nth-child(5) { animation-delay: 0.5s; }
        .ball:nth-child(6) { animation-delay: 0.6s; }
        .ball:nth-child(7) { animation-delay: 0.7s; }
        .ball:nth-child(8) { animation-delay: 0.8s; }
        .ball:nth-child(9) { animation-delay: 0.9s; }
        .ball:nth-child(10) { animation-delay: 1s; }
        
        /* Analysis Section */
        .analysis-section {
            margin-top: 30px;
            padding: 20px;
            background: rgba(0,0,0,0.2);
            border-radius: 15px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        
        .analysis-section h3 {
            color: rgba(255,255,255,0.7);
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 15px;
        }
        
        .analysis-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 15px;
        }
        
        .analysis-item {
            padding: 15px;
            background: rgba(255,255,255,0.03);
            border-radius: 10px;
            border-left: 3px solid #ffd700;
        }
        
        .analysis-item .title {
            font-size: 0.8em;
            color: rgba(255,255,255,0.5);
        }
        
        .analysis-item .content {
            font-size: 1.1em;
            font-weight: 700;
            margin-top: 5px;
            font-family: 'Orbitron', monospace;
        }
        
        /* MD5 Info */
        .md5-info {
            margin-top: 20px;
            padding: 15px;
            background: rgba(0,0,0,0.3);
            border-radius: 10px;
            font-family: 'Orbitron', monospace;
            font-size: 0.8em;
            color: rgba(255,255,255,0.4);
            word-break: break-all;
        }
        
        /* Progress Bar */
        .progress-container {
            margin: 20px 0;
            background: rgba(255,255,255,0.05);
            border-radius: 10px;
            overflow: hidden;
            height: 8px;
        }
        
        .progress-bar {
            height: 100%;
            background: linear-gradient(90deg, #ff6b00, #ffd700);
            border-radius: 10px;
            transition: width 1s ease;
            width: 0%;
        }
        
        /* Buttons */
        .btn {
            display: inline-block;
            padding: 12px 30px;
            border: none;
            border-radius: 25px;
            font-weight: 700;
            font-size: 1em;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin: 5px;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #ffd700, #ff6b00);
            color: #fff;
            box-shadow: 0 5px 20px rgba(255,215,0,0.3);
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 30px rgba(255,215,0,0.5);
        }
        
        .btn-secondary {
            background: rgba(255,255,255,0.1);
            color: #fff;
            border: 1px solid rgba(255,255,255,0.2);
        }
        
        .btn-secondary:hover {
            background: rgba(255,255,255,0.2);
        }
        
        .btn-danger {
            background: linear-gradient(135deg, #ff0040, #ff0040);
            color: #fff;
        }
        
        /* Responsive */
        @media (max-width: 768px) {
            .header h1 { font-size: 2em; }
            .prediction-result { font-size: 2.5em; padding: 15px 30px; }
            .stats-grid { grid-template-columns: repeat(2, 1fr); }
            .main-card { padding: 20px; }
            .ball { width: 40px; height: 40px; font-size: 1em; }
        }
        
        @media (max-width: 480px) {
            .header h1 { font-size: 1.5em; }
            .prediction-result { font-size: 1.8em; padding: 10px 20px; }
            .stats-grid { grid-template-columns: 1fr; }
            .analysis-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div id="particles"></div>
    
    <div class="container">
        <!-- Header -->
        <div class="header">
            <h1>🎯 MD5 TÀI XỈU PRO MAX</h1>
            <div class="subtitle">⚡ Dự Đoán Siêu Chính Xác Với AI ⚡</div>
        </div>
        
        <!-- Main Prediction -->
        <div class="main-card">
            <div class="prediction-area">
                <h2 style="color: rgba(255,255,255,0.5); font-size: 1em; letter-spacing: 3px; text-transform: uppercase;">
                    🔮 DỰ ĐOÁN TIẾP THEO
                </h2>
                
                <div class="prediction-result {{ prediction_class }}" id="predictionDisplay">
                    {{ prediction }}
                    <span class="confidence">Độ tin cậy: {{ confidence }}%</span>
                </div>
                
                <div class="progress-container">
                    <div class="progress-bar" style="width: {{ confidence }}%;"></div>
                </div>
                
                <div style="margin-top: 20px;">
                    <button class="btn btn-primary" onclick="refreshPrediction()">🔄 Dự Đoán Mới</button>
                    <button class="btn btn-secondary" onclick="addResult('Tài')">✅ Tài</button>
                    <button class="btn btn-secondary" onclick="addResult('Xỉu')">✅ Xỉu</button>
                </div>
                
                <div class="md5-info" id="md5Info">
                    MD5: {{ md5_hash }}
                </div>
            </div>
        </div>
        
        <!-- Statistics -->
        <div class="main-card">
            <h3 style="color: rgba(255,255,255,0.7); font-size: 0.9em; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 20px;">
                📊 THỐNG KÊ CHI TIẾT
            </h3>
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="label">Tổng Số Kết Quả</div>
                    <div class="value gold">{{ stats.total }}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Tài</div>
                    <div class="value tai-color">{{ stats.tai_count }}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Xỉu</div>
                    <div class="value xiu-color">{{ stats.xiu_count }}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Tỷ Lệ Tài</div>
                    <div class="value gold">{{ stats.tai_rate }}%</div>
                </div>
                <div class="stat-card">
                    <div class="label">Tỷ Lệ Xỉu</div>
                    <div class="value gold">{{ stats.xiu_rate }}%</div>
                </div>
                <div class="stat-card">
                    <div class="label">Kết Quả Gần Đây (100)</div>
                    <div class="value gold">{{ stats.recent_tai }} - {{ stats.recent_xiu }}</div>
                </div>
            </div>
        </div>
        
        <!-- Pattern Display -->
        <div class="main-card">
            <h3 style="color: rgba(255,255,255,0.7); font-size: 0.9em; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 20px;">
                🔄 MẪU CẦU GẦN ĐÂY
            </h3>
            <div class="pattern-display">
                <div class="pattern-balls">
                    {% for result in recent_results %}
                        <div class="ball {{ result|lower }}">
                            {{ result }}
                        </div>
                    {% endfor %}
                </div>
            </div>
        </div>
        
        <!-- Analysis -->
        <div class="main-card">
            <h3 style="color: rgba(255,255,255,0.7); font-size: 0.9em; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 20px;">
                🔍 PHÂN TÍCH CHUYÊN SÂU
            </h3>
            <div class="analysis-section">
                <div class="analysis-grid">
                    <div class="analysis-item">
                        <div class="title">Xu Hướng</div>
                        <div class="content" style="color: #ffd700;">{{ analysis.trend or 'Chưa đủ dữ liệu' }}</div>
                    </div>
                    <div class="analysis-item">
                        <div class="title">Chu Kỳ</div>
                        <div class="content" style="color: #00bfff;">{{ analysis.cycle or 'Chưa xác định' }}</div>
                    </div>
                    <div class="analysis-item">
                        <div class="title">Mẫu Cầu</div>
                        <div class="content" style="color: #ff6b00;">{{ analysis.patterns or 'Đang phân tích' }}</div>
                    </div>
                    <div class="analysis-item">
                        <div class="title">Độ Tin Cậy</div>
                        <div class="content" style="color: #ffd700;">{{ confidence }}%</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        // Particle animation
        (function createParticles() {
            const container = document.getElementById('particles');
            for (let i = 0; i < 50; i++) {
                const particle = document.createElement('div');
                particle.className = 'particle';
                particle.style.left = Math.random() * 100 + '%';
                particle.style.animationDuration = (Math.random() * 20 + 10) + 's';
                particle.style.animationDelay = (Math.random() * 20) + 's';
                particle.style.width = (Math.random() * 6 + 2) + 'px';
                particle.style.height = particle.style.width;
                container.appendChild(particle);
            }
        })();
        
        // Refresh prediction
        function refreshPrediction() {
            fetch('/api/predict')
                .then(response => response.json())
                .then(data => {
                    const display = document.getElementById('predictionDisplay');
                    display.className = 'prediction-result ' + data.prediction.toLowerCase();
                    display.innerHTML = data.prediction + '<span class="confidence">Độ tin cậy: ' + data.confidence + '%</span>';
                    
                    document.querySelector('.progress-bar').style.width = data.confidence + '%';
                    document.getElementById('md5Info').textContent = 'MD5: ' + data.md5_hash;
                })
                .catch(err => console.error('Error:', err));
        }
        
        // Add result
        function addResult(result) {
            fetch('/api/add_result', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({result: result})
            })
            .then(response => response.json())
            .then(() => {
                refreshPrediction();
                // Reload stats after a short delay
                setTimeout(() => location.reload(), 500);
            })
            .catch(err => console.error('Error:', err));
        }
        
        // Auto refresh every 30 seconds
        setInterval(refreshPrediction, 30000);
    </script>
</body>
</html>
"""

# ============================================================
# ROUTES API
# ============================================================
@app.route('/')
def index():
    """Trang chủ"""
    # Lấy dự đoán
    prediction = prediction_engine.predict_next()
    stats = prediction_engine.get_statistics()
    
    # Lấy kết quả gần đây
    recent = list(prediction_engine.history)[-10:] if prediction_engine.history else []
    
    # Phân tích
    analysis = prediction_engine.analyze_patterns() or {}
    
    # Format data
    data = {
        'prediction': prediction['prediction'],
        'prediction_class': prediction['prediction'].lower(),
        'confidence': round(prediction['confidence'], 1),
        'md5_hash': prediction['md5_hash'],
        'stats': stats or {'total': 0, 'tai_count': 0, 'xiu_count': 0, 'tai_rate': 0, 'xiu_rate': 0, 'recent_tai': 0, 'recent_xiu': 0},
        'recent_results': recent,
        'analysis': {
            'trend': analysis.get('trend', 'Chưa đủ dữ liệu'),
            'cycle': analysis.get('cycle', {}).get('pattern', 'Chưa xác định') if analysis.get('cycle') else 'Chưa xác định',
            'patterns': analysis.get('patterns', [{}])[0].get('pattern', 'Đang phân tích') if analysis.get('patterns') else 'Đang phân tích'
        }
    }
    
    return render_template_string(HTML_TEMPLATE, **data)

@app.route('/api/predict')
def api_predict():
    """API dự đoán"""
    prediction = prediction_engine.predict_next()
    return jsonify({
        'prediction': prediction['prediction'],
        'confidence': round(prediction['confidence'], 1),
        'md5_hash': prediction['md5_hash'],
        'scores': prediction['scores']
    })

@app.route('/api/add_result', methods=['POST'])
def api_add_result():
    """Thêm kết quả"""
    data = request.json
    result = data.get('result')
    
    if result not in ['Tài', 'Xỉu']:
        return jsonify({'error': 'Invalid result'}), 400
    
    # Lưu vào database
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    md5_hash = hashlib.md5((result + str(time.time())).encode()).hexdigest()
    c.execute("INSERT INTO history (result, md5_hash) VALUES (?, ?)", (result, md5_hash))
    conn.commit()
    conn.close()
    
    # Cập nhật engine
    prediction_engine.add_result(result)
    
    return jsonify({'success': True})

@app.route('/api/stats')
def api_stats():
    """API thống kê"""
    stats = prediction_engine.get_statistics()
    return jsonify(stats or {})

@app.route('/api/history')
def api_history():
    """API lịch sử"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT result, timestamp FROM history ORDER BY timestamp DESC LIMIT 50")
    rows = c.fetchall()
    conn.close()
    
    return jsonify([{'result': r[0], 'timestamp': r[1]} for r in rows])

@app.route('/api/reset', methods=['POST'])
def api_reset():
    """Reset dữ liệu (chỉ admin)"""
    # Kiểm tra admin đơn giản
    # Trong thực tế nên có xác thực
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM history")
    c.execute("DELETE FROM predictions")
    conn.commit()
    conn.close()
    
    prediction_engine.history.clear()
    
    return jsonify({'success': True})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

# ============================================================
# BOT TELEGRAM
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = f"""
🎯 *{BOT_NAME}*

Chào mừng bạn đến với hệ thống dự đoán Tài Xỉu siêu chính xác!

📊 *Các lệnh:*
/predict - Dự đoán kết quả tiếp theo
/stats - Xem thống kê chi tiết
/history - Xem 10 kết quả gần nhất
/add_tai - Thêm kết quả Tài
/add_xiu - Thêm kết quả Xỉu
/patterns - Xem mẫu cầu

🔮 *Web Dashboard:* {request.url_root}
    """
    bot.reply_to(message, welcome_text, parse_mode='Markdown')

@bot.message_handler(commands=['predict'])
def predict_command(message):
    prediction = prediction_engine.predict_next()
    response = f"""
🎯 *DỰ ĐOÁN TIẾP THEO*

📊 *Kết quả:* *{prediction['prediction']}*
🎯 *Độ tin cậy:* {prediction['confidence']:.1f}%

🔐 *MD5 Hash:* `{prediction['md5_hash']}`

📈 *Phân tích:*
- MD5: {prediction['md5_pred']}
- Điểm số: Tài {prediction['scores']['Tài']:.2f} | Xỉu {prediction['scores']['Xỉu']:.2f}
    """
    bot.reply_to(message, response, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def stats_command(message):
    stats = prediction_engine.get_statistics()
    if not stats:
        bot.reply_to(message, "❌ Chưa có dữ liệu thống kê!")
        return
    
    response = f"""
📊 *THỐNG KÊ CHI TIẾT*

📌 *Tổng số:* {stats['total']}
🟠 *Tài:* {stats['tai_count']} ({stats['tai_rate']:.1f}%)
🔵 *Xỉu:* {stats['xiu_count']} ({stats['xiu_rate']:.1f}%)

📈 *100 kết quả gần đây:*
- Tài: {stats['recent_tai']}
- Xỉu: {stats['recent_xiu']}
- Tỷ lệ: {stats['recent_rate']:.1f}%

🔄 *Kết quả cuối:* {stats['last_result']}
    """
    bot.reply_to(message, response, parse_mode='Markdown')

@bot.message_handler(commands=['history'])
def history_command(message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT result, timestamp FROM history ORDER BY timestamp DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        bot.reply_to(message, "❌ Chưa có lịch sử!")
        return
    
    history_text = "📜 *10 KẾT QUẢ GẦN NHẤT*\n\n"
    for i, (result, timestamp) in enumerate(reversed(rows)):
        emoji = "🟠" if result == "Tài" else "🔵"
        time_str = timestamp[:19] if timestamp else "N/A"
        history_text += f"{i+1}. {emoji} *{result}* - {time_str}\n"
    
    bot.reply_to(message, history_text, parse_mode='Markdown')

@bot.message_handler(commands=['patterns'])
def patterns_command(message):
    patterns = prediction_engine.find_patterns()
    if not patterns:
        bot.reply_to(message, "❌ Chưa đủ dữ liệu để phân tích mẫu cầu!")
        return
    
    response = "🔄 *MẪU CẦU PHÁT HIỆN*\n\n"
    for p in patterns[:5]:
        response += f"📌 Mẫu: `{p['pattern']}`\n"
        response += f"   - Độ dài: {p['length']}\n"
        response += f"   - Xuất hiện: {p['count']} lần\n"
        response += f"   - Dự đoán tiếp: {p['next']}\n\n"
    
    bot.reply_to(message, response, parse_mode='Markdown')

@bot.message_handler(commands=['add_tai'])
def add_tai(message):
    add_result_command(message, 'Tài')

@bot.message_handler(commands=['add_xiu'])
def add_xiu(message):
    add_result_command(message, 'Xỉu')

def add_result_command(message, result):
    md5_hash = hashlib.md5((result + str(time.time())).encode()).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (result, md5_hash) VALUES (?, ?)", (result, md5_hash))
    conn.commit()
    conn.close()
    
    prediction_engine.add_result(result)
    
    bot.reply_to(message, f"✅ Đã thêm kết quả *{result}* thành công!\n🔐 MD5: `{md5_hash}`", parse_mode='Markdown')

# ============================================================
# MAIN
# ============================================================
def run_bot():
    """Chạy bot trong thread riêng"""
    logger.info("🚀 Khởi động Telegram Bot...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"❌ Bot error: {e}")

if __name__ == '__main__':
    # Chạy bot trong thread riêng
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Chạy Flask
    logger.info(f"🌐 Web server chạy tại: http://0.0.0.0:{PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
