from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
from threading import Lock, RLock, Thread, Event
from collections import deque
from datetime import datetime, timedelta
from functools import wraps
import logging
import time
import json
import os
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List
import queue as thread_queue
import secrets
import hashlib
import pymysql
import uuid
from google.cloud import pubsub_v1

# ============================================================================
# CONFIGURATION & SETUP
# ============================================================================

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # สำหรับ session
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Scheduler Token (ควรเก็บใน environment variable)
SCHEDULER_TOKEN = os.getenv('SCHEDULER_TOKEN', 'your-super-secret-scheduler-token-change-this')

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# AUTHENTICATION DECORATORS
# ============================================================================

def login_required(f):
    """Require user to be logged in"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'username' not in session:
            return jsonify({
                'error': 'Authentication required',
                'error_code': 'AUTH_REQUIRED'
            }), 401
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    """Require admin role"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'username' not in session:
            return jsonify({
                'error': 'Authentication required',
                'error_code': 'AUTH_REQUIRED'
            }), 401
        
        username = session['username']
        with users_lock:
            user = users_db.get(username)
        
        if not user or user.role != UserRole.ADMIN:
            return jsonify({
                'error': 'Admin access required',
                'error_code': 'ADMIN_REQUIRED'
            }), 403
        
        return f(*args, **kwargs)
    return wrapper

def scheduler_auth_required(f):
    """Require valid scheduler token"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        # ตรวจสอบ header จาก Cloud Scheduler
        scheduler_header = request.headers.get('X-Cloudscheduler')
        auth_header = request.headers.get('Authorization')
        
        # อนุญาตให้ผ่านถ้ามี X-Cloudscheduler header (จาก GCP)
        if scheduler_header:
            logger.info(f"Scheduler request from Cloud Scheduler: {scheduler_header}")
            return f(*args, **kwargs)
        
        # หรือตรวจสอบ Bearer Token
        if auth_header:
            try:
                token_type, token = auth_header.split(' ', 1)
                if token_type == 'Bearer' and token == SCHEDULER_TOKEN:
                    logger.info("Scheduler request with valid token")
                    return f(*args, **kwargs)
            except ValueError:
                pass
        
        logger.warning(f"Unauthorized scheduler request from {request.remote_addr}")
        return jsonify({
            'error': 'Unauthorized',
            'error_code': 'SCHEDULER_AUTH_REQUIRED'
        }), 401
    
    return wrapper

# ============================================================================
# ERROR HANDLING CLASSES
# ============================================================================

class ErrorCode(Enum):
    """Error codes for better error tracking"""
    VALIDATION_ERROR = "VALIDATION_ERROR"
    QUEUE_FULL = "QUEUE_FULL"
    QUEUE_EMPTY = "QUEUE_EMPTY"
    RATE_LIMIT = "RATE_LIMIT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ADMIN_REQUIRED = "ADMIN_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    SCHEDULER_AUTH_REQUIRED = "SCHEDULER_AUTH_REQUIRED"

class QueueException(Exception):
    """Base exception for queue operations"""
    def __init__(self, message: str, error_code: ErrorCode, status_code: int = 400):
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        super().__init__(self.message)

@dataclass
class ErrorResponse:
    """Structured error response"""
    error: str
    error_code: str
    timestamp: str
    details: Optional[Dict] = None
    
    def to_dict(self):
        return asdict(self)

# ============================================================================
# CIRCUIT BREAKER PATTERN
# ============================================================================

class CircuitBreaker:
    """Circuit breaker for preventing cascading failures"""
    def __init__(self, failure_threshold=5, timeout=60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = "CLOSED"
        self.lock = Lock()
    
    def call(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with self.lock:
                if self.state == "OPEN":
                    if time.time() - self.last_failure_time > self.timeout:
                        self.state = "HALF_OPEN"
                        logger.info("Circuit breaker: OPEN -> HALF_OPEN")
                    else:
                        raise QueueException(
                            "Service temporarily unavailable",
                            ErrorCode.SERVICE_UNAVAILABLE,
                            503
                        )
            
            try:
                result = func(*args, **kwargs)
                with self.lock:
                    if self.state == "HALF_OPEN":
                        self.state = "CLOSED"
                        self.failures = 0
                        logger.info("Circuit breaker: HALF_OPEN -> CLOSED")
                return result
            except Exception as e:
                with self.lock:
                    self.failures += 1
                    self.last_failure_time = time.time()
                    if self.failures >= self.failure_threshold:
                        self.state = "OPEN"
                        logger.error(f"Circuit breaker: CLOSED -> OPEN (failures: {self.failures})")
                raise
        return wrapper

circuit_breaker = CircuitBreaker()

# ============================================================================
# RATE LIMITING
# ============================================================================

class RateLimiter:
    """Token bucket rate limiter"""
    def __init__(self, max_requests=10, time_window=60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = {}
        self.lock = Lock()
    
    def is_allowed(self, identifier: str) -> bool:
        with self.lock:
            now = time.time()
            if identifier not in self.requests:
                self.requests[identifier] = []
            
            self.requests[identifier] = [
                req_time for req_time in self.requests[identifier]
                if now - req_time < self.time_window
            ]
            
            if len(self.requests[identifier]) < self.max_requests:
                self.requests[identifier].append(now)
                return True
            return False

rate_limiter = RateLimiter()

# ============================================================================
# PUB/SUB SYSTEM
# ============================================================================

class PubSubEvent(Enum):
    """Event types for pub/sub"""
    QUEUE_ADDED = "queue_added"
    QUEUE_CALLED = "queue_called"
    QUEUE_CLEARED = "queue_cleared"
    QUEUE_EXPIRED = "queue_expired"
    SCHEDULER_CLEARED = "scheduler_cleared"

class PubSubSystem:
    """ระบบ Pub/Sub ที่เชื่อมต่อกับ Google Cloud โดยตรง"""
    def __init__(self):
        # ดึงค่าจาก Environment Variables (ต้องไปตั้งค่าใน Cloud Run หรือ OS)
        self.project_id = os.getenv('GCP_PROJECT_ID')
        self.topic_id = os.getenv('GCP_TOPIC_ID')
        
        # สร้างตัวส่งข้อมูลไป GCP
        from google.cloud import pubsub_v1
        self.publisher = pubsub_v1.PublisherClient()
        self.topic_path = self.publisher.topic_path(self.project_id, self.topic_id)

    def publish(self, event_type: PubSubEvent, data: dict):
        """ฟังก์ชันเดิมที่เคยใช้ แต่เปลี่ยนการทำงานภายในให้ส่งขึ้น Cloud"""
        event_data = {
            'event': event_type.value,
            'data': data,
            'timestamp': datetime.now().isoformat()
        }
        
        # เตรียมข้อมูล
        message_json = json.dumps(event_data)
        message_bytes = message_json.encode("utf-8")
        
        try:
            # ยิงขึ้น GCP Pub/Sub
            future = self.publisher.publish(self.topic_path, message_bytes)
            logger.info(f"GCP Pub/Sub Sent: {event_type.value} (ID: {future.result()})")
        except Exception as e:
            logger.error(f"GCP Pub/Sub Error: {str(e)}")

    def subscribe(self, event_type: PubSubEvent):
        # ในระบบ Cloud จะไม่ใช้ subscribe() แบบสร้าง Queue ใน Memory แล้ว
        # จึงปล่อยว่างไว้เพื่อไม่ให้โค้ดส่วนอื่นที่เรียกฟังก์ชันนี้พัง
        return thread_queue.Queue()

 # pubsub = PubSubSystem()

# ============================================================================
# SCHEDULER LOGS
# ============================================================================

scheduler_logs = deque(maxlen=50)  # เก็บ log 50 รายการล่าสุด
scheduler_logs_lock = RLock()

def add_scheduler_log(action: str, details: dict):
    """Add scheduler action to log"""
    with scheduler_logs_lock:
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'details': details
        }
        scheduler_logs.append(log_entry)
        logger.info(f"[SCHEDULER] {action}: {details}")

# ============================================================================
# SCHEDULER
# ============================================================================

class Scheduler:
    """Background task scheduler"""
    def __init__(self):
        self.tasks = []
        self.running = False
        self.thread = None
        self.stop_event = Event()
    
    def add_task(self, func, interval: int, name: str):
        self.tasks.append({
            'func': func,
            'interval': interval,
            'name': name,
            'last_run': 0
        })
    
    def start(self):
        if not self.running:
            self.running = True
            self.stop_event.clear()
            self.thread = Thread(target=self._run, daemon=True)
            self.thread.start()
            logger.info("Scheduler started")
    
    def stop(self):
        self.running = False
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Scheduler stopped")
    
    def _run(self):
        while self.running and not self.stop_event.is_set():
            try:
                current_time = time.time()
                for task in self.tasks:
                    if current_time - task['last_run'] >= task['interval']:
                        try:
                            logger.info(f"Running scheduled task: {task['name']}")
                            task['func']()
                            task['last_run'] = current_time
                        except Exception as e:
                            logger.error(f"Error in scheduled task {task['name']}: {str(e)}")
            except Exception as e:
                logger.error(f"Scheduler error: {str(e)}")
            
            time.sleep(1)

scheduler = Scheduler()

# ============================================================================
# QUEUE DATA STRUCTURES
# ============================================================================

@dataclass
class QueueEntry:
    """Queue entry with enhanced metadata"""
    name: str
    timestamp: str
    id: int
    expires_at: str
    priority: int = 0
    phone: Optional[str] = None
    party_size: int = 1
    created_by: Optional[str] = None
    
    def to_dict(self):
        return asdict(self)
    
    def is_expired(self) -> bool:
        expires = datetime.fromisoformat(self.expires_at)
        return datetime.now() > expires

queue = deque()
lock = RLock()
history = deque(maxlen=100)
history_lock = RLock()

MAX_QUEUE_SIZE = 100
QUEUE_EXPIRY_MINUTES = 120
counter = 0
counter_lock = Lock()

# ============================================================================
# SCHEDULED TASKS
# ============================================================================

def cleanup_expired_queues():
    """Remove expired queue entries"""
    with lock:
        initial_size = len(queue)
        expired = []
        
        for entry in queue:
            if isinstance(entry, dict):
                continue
            if entry.is_expired():
                expired.append(entry)
        
        for entry in expired:
            queue.remove(entry)
            logger.info(f"Removed expired queue entry: {entry.name}")
            pubsub.publish(PubSubEvent.QUEUE_EXPIRED, entry.to_dict())
        
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired entries")

def log_queue_stats():
    """Log queue statistics"""
    with lock:
        queue_size = len(queue)
    with history_lock:
        history_size = len(history)
    
    logger.info(f"Queue stats - Current: {queue_size}, History: {history_size}")

# ============================================================================
# VALIDATION
# ============================================================================

def validate_name(name: str) -> str:
    if not name:
        raise QueueException("Name is required", ErrorCode.VALIDATION_ERROR)
    
    name = name.strip()
    if not name:
        raise QueueException("Name cannot be empty", ErrorCode.VALIDATION_ERROR)
    
    if len(name) > 100:
        raise QueueException("Name too long (max 100 characters)", ErrorCode.VALIDATION_ERROR)
    
    return name

def validate_party_size(party_size: int) -> int:
    if party_size < 1:
        raise QueueException("Party size must be at least 1", ErrorCode.VALIDATION_ERROR)
    if party_size > 20:
        raise QueueException("Party size too large (max 20)", ErrorCode.VALIDATION_ERROR)
    return party_size

# ============================================================================
# ERROR HANDLING DECORATOR
# ============================================================================

def handle_errors(f):
    """Decorator for consistent error handling"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except QueueException as e:
            error_response = ErrorResponse(
                error=e.message,
                error_code=e.error_code.value,
                timestamp=datetime.now().isoformat()
            )
            logger.warning(f"QueueException in {f.__name__}: {e.message}")
            return jsonify(error_response.to_dict()), e.status_code
        except ValueError as e:
            error_response = ErrorResponse(
                error=str(e),
                error_code=ErrorCode.VALIDATION_ERROR.value,
                timestamp=datetime.now().isoformat()
            )
            logger.error(f"ValueError in {f.__name__}: {str(e)}")
            return jsonify(error_response.to_dict()), 400
        except Exception as e:
            error_response = ErrorResponse(
                error="Internal server error",
                error_code=ErrorCode.INTERNAL_ERROR.value,
                timestamp=datetime.now().isoformat(),
                details={"message": str(e)} if app.debug else None
            )
            logger.error(f"Unexpected error in {f.__name__}: {str(e)}", exc_info=True)
            return jsonify(error_response.to_dict()), 500
    return wrapper

# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@app.route('/')
def home():
    """Redirect โดยเช็คจาก session role ที่เราเก็บไว้ตอน login"""
    if 'username' in session:
        role = session.get('role')
        if role == 'admin':
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('user_dashboard'))
    
    return render_template('login.html')

@app.route('/login', methods=['POST'])
@handle_errors
def login():
    """Login โดยเช็คจากตาราง users ใน Cloud SQL"""
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    if not username or not password:
        raise QueueException("กรุณากรอกข้อมูลให้ครบถ้วน", ErrorCode.VALIDATION_ERROR)
    
    db = get_db_connection()
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE username = %s AND is_active = TRUE", (username,))
            user_data = cursor.fetchone()
            
            if user_data:
                # นำรหัสที่กรอกมา Hash แล้วเทียบกับค่าใน Database
                pw_hash = hashlib.sha256(password.encode()).hexdigest()
                if user_data['password_hash'] == pw_hash:
                    # อัปเดตเวลาใช้งานล่าสุด
                    cursor.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (user_data['id'],))
                    db.commit()
                    
                    # เก็บข้อมูลใน Session
                    session.permanent = True
                    session['username'] = user_data['username']
                    session['role'] = user_data['role']
                    
                    return jsonify({
                        'success': True,
                        'user': {'username': user_data['username'], 'role': user_data['role']},
                        'redirect': '/admin' if user_data['role'] == 'admin' else '/user'
                    })
        
        raise QueueException("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", ErrorCode.INVALID_CREDENTIALS, 401)
    finally:
        if db: db.close()
            
@app.route('/logout', methods=['POST'])
def logout():
    """Logout endpoint"""
    username = session.get('username')
    session.clear()
    logger.info(f"User {username} logged out")
    
    return jsonify({
        'success': True,
        'message': 'Logged out successfully'
    })

@app.route('/check-auth')
def check_auth():
    """Check authentication status จาก session"""
    if 'username' in session:
        return jsonify({
            'authenticated': True,
            'user': {
                'username': session['username'],
                'role': session.get('role')
            }
        })
    return jsonify({'authenticated': False}), 401

@app.route('/signup', methods=['POST'])
@handle_errors
def signup():
    """ลงทะเบียนผู้ใช้ใหม่และบันทึกลง Cloud SQL"""
    data = request.json
    if not data:
        raise QueueException("ต้องกรอกข้อมูลให้ครบถ้วน", ErrorCode.INVALID_REQUEST)
    
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    if not username or not password:
        raise QueueException("กรุณากรอกชื่อผู้ใช้และรหัสผ่าน", ErrorCode.VALIDATION_ERROR)
    
    if len(password) < 6:
        raise QueueException("รหัสผ่านต้องมีความยาวอย่างน้อย 6 ตัวอักษร", ErrorCode.VALIDATION_ERROR)

    db = get_db_connection()
    if not db:
        raise QueueException("เชื่อมต่อฐานข้อมูลล้มเหลว", ErrorCode.INTERNAL_ERROR, 500)

    try:
        with db.cursor() as cursor:
            # 1. ตรวจสอบว่ามีชื่อผู้ใช้นี้อยู่แล้วหรือไม่
            cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cursor.fetchone():
                raise QueueException("ชื่อผู้ใช้นี้ถูกใช้งานแล้ว", ErrorCode.VALIDATION_ERROR)
            
            # 2. Hash รหัสผ่านก่อนบันทึก (SHA-256)
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            
            # 3. บันทึกข้อมูลลงตาราง users
            sql = "INSERT INTO users (username, password_hash, role, is_active) VALUES (%s, %s, 'user', 1)"
            cursor.execute(sql, (username, password_hash))
            
        db.commit()
        logger.info(f"User {username} signed up successfully")
        return jsonify({"success": True, "message": "ลงทะเบียนสำเร็จ กรุณาเข้าสู่ระบบ"})
    
    except QueueException:
        raise # ส่ง Exception ต่อไปที่ Error Handler
    except Exception as e:
        db.rollback()
        logger.error(f"Signup error: {e}")
        return jsonify({"success": False, "error": "เกิดข้อผิดพลาดในการลงทะเบียน"}), 500
    finally:
        if db:
            db.close()
            
@app.route('/signup-page')
def signup_page():
    """หน้าเพจสำหรับสมัครสมาชิก"""
    return render_template('signup.html')
# ============================================================================
# DASHBOARD ROUTES
# ============================================================================

@app.route('/user')
@login_required
def user_dashboard():
    """User dashboard"""
    return render_template('user_dashboard.html')

@app.route('/admin')
@admin_required
def admin_dashboard():
    """Admin dashboard"""
    return render_template('admin_dashboard.html')

# ============================================================================
# CLOUD SCHEDULER ROUTES
# ============================================================================

@app.route('/scheduler/clear-queue', methods=['POST'])
@handle_errors
@scheduler_auth_required
def scheduler_clear_queue():
    """
    Cloud Scheduler endpoint: ล้างคิวทุกวัน
    ต้องมี header Authorization: Bearer TOKEN หรือ X-Cloudscheduler
    """
    try:
        with lock:
            cleared_count = len(queue)
            cleared_items = list(queue)  # เก็บข้อมูลก่อนล้าง
            queue.clear()
            
            logger.info(f"[SCHEDULER] Cleared {cleared_count} items from queue")
        
        # บันทึก log
        add_scheduler_log('daily_clear', {
            'cleared_count': cleared_count,
            'triggered_by': 'cloud_scheduler',
            'items': [
                item.to_dict() if isinstance(item, QueueEntry) else item
                for item in cleared_items
            ][:10]  # เก็บแค่ 10 รายการแรก
        })
        
        # ส่ง event
        pubsub.publish(PubSubEvent.SCHEDULER_CLEARED, {
            'cleared_count': cleared_count
        })
        
        return jsonify({
            "success": True,
            "message": "ล้างคิวสำเร็จ (Cloud Scheduler)",
            "cleared_count": cleared_count,
            "timestamp": datetime.now().isoformat()
        })
    
    except Exception as e:
        logger.error(f"Error in scheduler_clear_queue: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/scheduler/clear-old-history', methods=['POST'])
@handle_errors
@scheduler_auth_required
def scheduler_clear_old_history():
    """
    Cloud Scheduler endpoint: ล้างประวัติเก่าที่เกิน 30 วัน
    """
    try:
        cutoff_date = datetime.now() - timedelta(days=30)
        
        with history_lock:
            original_count = len(history)
            
            # กรองเฉพาะข้อมูลที่ใหม่กว่า 30 วัน
            filtered_history = []
            for item in history:
                try:
                    if 'timestamp' in item:
                        item_date = datetime.fromisoformat(item['timestamp'])
                        if item_date > cutoff_date:
                            filtered_history.append(item)
                except (ValueError, KeyError):
                    # ถ้า parse ไม่ได้ ให้เก็บไว้
                    filtered_history.append(item)
            
            history.clear()
            history.extend(filtered_history)
            
            removed_count = original_count - len(history)
            logger.info(f"[SCHEDULER] Cleared {removed_count} old history items")
        
        # บันทึก log
        add_scheduler_log('clear_old_history', {
            'removed_count': removed_count,
            'original_count': original_count,
            'remaining_count': len(filtered_history),
            'cutoff_date': cutoff_date.isoformat()
        })
        
        return jsonify({
            "success": True,
            "message": "ล้างประวัติเก่าสำเร็จ",
            "removed_count": removed_count,
            "remaining_count": len(filtered_history),
            "timestamp": datetime.now().isoformat()
        })
    
    except Exception as e:
        logger.error(f"Error in scheduler_clear_old_history: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/scheduler/stats', methods=['GET'])
@handle_errors
@scheduler_auth_required
def scheduler_stats():
    """ดูสถิติการทำงานของ scheduler"""
    try:
        with lock:
            current_queue_count = len(queue)
        
        with history_lock:
            total_history = len(history)
        
        with scheduler_logs_lock:
            logs = list(scheduler_logs)
            
            # นับจำนวนครั้งที่ clear
            clear_count = sum(1 for log in logs if log['action'] == 'daily_clear')
            history_clear_count = sum(1 for log in logs if log['action'] == 'clear_old_history')
            
            # หา log ล่าสุดของแต่ละประเภท
            last_clear = next(
                (log for log in reversed(logs) if log['action'] == 'daily_clear'),
                None
            )
            last_history_clear = next(
                (log for log in reversed(logs) if log['action'] == 'clear_old_history'),
                None
            )
        
        return jsonify({
            "success": True,
            "current_queue": current_queue_count,
            "total_history": total_history,
            "scheduler_stats": {
                "total_daily_clears": clear_count,
                "total_history_clears": history_clear_count,
                "last_daily_clear": last_clear,
                "last_history_clear": last_history_clear,
                "total_scheduler_actions": len(logs)
            },
            "timestamp": datetime.now().isoformat()
        })
    
    except Exception as e:
        logger.error(f"Error in scheduler_stats: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/scheduler/logs', methods=['GET'])
@handle_errors
@admin_required
def scheduler_logs_view():
    """ดู scheduler logs (Admin only)"""
    try:
        limit = request.args.get('limit', 20, type=int)
        
        with scheduler_logs_lock:
            logs = list(scheduler_logs)[-limit:]
        
        return jsonify({
            "success": True,
            "logs": logs,
            "total": len(logs),
            "timestamp": datetime.now().isoformat()
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/scheduler/test-clear', methods=['POST'])
@handle_errors
@admin_required
def test_scheduler_clear():
    """ทดสอบการล้างคิวด้วยตัวเอง (Admin only)"""
    try:
        # เรียกใช้ฟังก์ชันเดียวกับ scheduler
        with lock:
            cleared_count = len(queue)
            queue.clear()
        
        add_scheduler_log('manual_test_clear', {
            'cleared_count': cleared_count,
            'triggered_by': session.get('username'),
            'note': 'Manual test by admin'
        })
        
        return jsonify({
            "success": True,
            "message": "ทดสอบล้างคิวสำเร็จ",
            "cleared_count": cleared_count,
            "timestamp": datetime.now().isoformat()
        })
    
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

# ============================================================================
# QUEUE ROUTES
# ============================================================================

@app.route('/reserve', methods=['POST'])
@handle_errors
@login_required
@circuit_breaker.call
def reserve():
    """Reserve a spot in queue (User only)"""
    client_ip = request.remote_addr
    if not rate_limiter.is_allowed(client_ip):
        raise QueueException(
            "Too many requests. Please try again later.",
            ErrorCode.RATE_LIMIT,
            429
        )
    
    data = request.json
    if not data:
        raise QueueException("Request body is required", ErrorCode.INVALID_REQUEST)
    
    name = validate_name(data.get('name', ''))
    party_size = validate_party_size(data.get('party_size', 1))
    phone = data.get('phone', '').strip() if data.get('phone') else None
    priority = int(data.get('priority', 0))
    
    with lock:
        if len(queue) >= MAX_QUEUE_SIZE:
            raise QueueException(
                f"Queue is full (max {MAX_QUEUE_SIZE})",
                ErrorCode.QUEUE_FULL,
                503
            )
        
        global counter
        with counter_lock:
            counter += 1
            entry_id = counter
        
        entry = QueueEntry(
            name=name,
            timestamp=datetime.now().isoformat(),
            id=entry_id,
            expires_at=(datetime.now() + timedelta(minutes=QUEUE_EXPIRY_MINUTES)).isoformat(),
            priority=priority,
            phone=phone,
            party_size=party_size,
            created_by=session.get('username')
        )
        
        queue.append(entry)
        position = len(queue)
        
        logger.info(f"Added {name} to queue at position {position} by {session.get('username')}")
    
    pubsub.publish(PubSubEvent.QUEUE_ADDED, entry.to_dict())
    
    return jsonify({
        "success": True,
        "queue_position": position,
        "entry": entry.to_dict(),
        "message": f"คุณอยู่ลำดับที่ {position}",
        "estimated_wait_minutes": position * 5
    })

@app.route('/queue', methods=['GET'])
@handle_errors
@login_required
def show_queue():
    """ดึงข้อมูลคิวปัจจุบันจาก Cloud SQL"""
    db = get_db_connection()
    if not db:
        return jsonify({"success": False, "error": "Database connection failed"}), 500
        
    try:
        with db.cursor() as cursor:
            # ดึงเฉพาะคิวที่สถานะเป็น 'waiting' เรียงตามลำดับ position
            sql = "SELECT * FROM queue_entries WHERE status = 'waiting' ORDER BY position ASC"
            cursor.execute(sql)
            rows = cursor.fetchall()
            
            queue_list = []
            for row in rows:
                queue_list.append({
                    "id": row['id'],
                    "name": row['name'],
                    "phone": row['phone'],
                    "party_size": row['notes'], # แสดงข้อมูล party_size จาก notes ที่คุณเก็บไว้
                    "created_by": row['created_by'],
                    "timestamp": row['created_at'].isoformat()
                })
                
        return jsonify({
            "success": True,
            "queue": queue_list,
            "total": len(queue_list),
            "capacity": MAX_QUEUE_SIZE,
            "available_slots": MAX_QUEUE_SIZE - len(queue_list)
        })
    finally:
        if db:
            db.close()

@app.route('/next', methods=['POST'])
@handle_errors
@admin_required
@circuit_breaker.call
def next_queue():
    """เรียกคิวถัดไปจาก Cloud SQL (Admin เท่านั้น)"""
    db = get_db_connection()
    if not db:
        return jsonify({"success": False, "message": "Database connection failed"}), 500
    
    try:
        with db.cursor() as cursor:
            # 1. หาคิวที่สถานะเป็น 'waiting' ที่ลำดับน้อยที่สุด (มาก่อน)
            cursor.execute("""
                SELECT * FROM queue_entries 
                WHERE status = 'waiting' 
                ORDER BY position ASC LIMIT 1
            """)
            called = cursor.fetchone()
            
            if not called:
                return jsonify({
                    "success": True,
                    "called": None,
                    "message": "ไม่มีคนในคิว",
                    "remaining": 0
                })

            # 2. อัปเดตสถานะคิวในตารางหลักเป็น 'called'
            cursor.execute("UPDATE queue_entries SET status = 'called' WHERE id = %s", (called['id'],))
            
            # 3. บันทึกลงตารางประวัติ (queue_history) เพื่อให้หน้า Admin ดึงไปโชว์ได้
            history_sql = """
                INSERT INTO queue_history (queue_id, name, phone, notes, created_at, called_by)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(history_sql, (
                called['queue_id'], called['name'], called['phone'], 
                called['notes'], called['created_at'], session.get('username')
            ))
            
            db.commit() # ยืนยันการเปลี่ยนแปลงข้อมูล
            
            # 4. นับจำนวนคิวที่เหลือรออยู่
            cursor.execute("SELECT COUNT(*) as remaining FROM queue_entries WHERE status = 'waiting'")
            remaining = cursor.fetchone()['remaining']

        # เตรียมข้อมูลเพื่อส่ง Event แจ้งเตือนหน้าจอแบบ Real-time
        called_dict = {
            "id": called['id'],
            "name": called['name'],
            "party_size": called['notes'], # Mapping notes มาเป็น party_size ให้ตรงกับ Frontend
            "phone": called['phone'],
            "created_by": called['created_by']
        }
        
        # ส่งแจ้งเตือนผ่าน Pub/Sub (ถ้าเปิดใช้งาน)
        if 'pubsub' in globals():
            pubsub.publish(PubSubEvent.QUEUE_CALLED, called_dict)
        
        logger.info(f"Called: {called['name']} from SQL by {session.get('username')}")
        
        return jsonify({
            "success": True,
            "called": called_dict,
            "remaining": remaining,
            "message": f"เรียก: {called['name']}"
        })
    except Exception as e:
        if db: db.rollback()
        logger.error(f"Error in next_queue: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if db: db.close()
        
@app.route('/history', methods=['GET'])
@handle_errors
@login_required
def show_history():
    limit = request.args.get('limit', 10, type=int)
    db = get_db_connection()
    try:
        with db.cursor() as cursor:
            # ดึงประวัติล่าสุดตามจำนวน limit
            cursor.execute("SELECT * FROM queue_history ORDER BY called_at DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
            
            # ดึงจำนวนที่ให้บริการทั้งหมดวันนี้
            cursor.execute("SELECT COUNT(*) as total FROM queue_history WHERE DATE(called_at) = CURDATE()")
            total_today = cursor.fetchone()['total']
            
            history_list = []
            for row in rows:
                history_list.append({
                    "name": row['name'],
                    "party_size": row['notes'],
                    "called_at": row['called_at'].isoformat(),
                    "called_by": row['called_by']
                })
        return jsonify({"success": True, "history": history_list, "total": total_today})
    finally:
        if db: db.close()

@app.route('/clear', methods=['POST'])
@handle_errors
@admin_required
def clear_queue():
    db = get_db_connection()
    try:
        with db.cursor() as cursor:
            # เปลี่ยนสถานะคิวที่รออยู่ทั้งหมดเป็น 'cancelled'
            cursor.execute("UPDATE queue_entries SET status = 'cancelled' WHERE status = 'waiting'")
            cleared_count = cursor.rowcount
        db.commit()
        return jsonify({"success": True, "cleared": cleared_count})
    finally:
        if db: db.close()

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    with lock:
        queue_size = len(queue)
    
    return jsonify({
        "status": "healthy",
        "circuit_breaker": circuit_breaker.state,
        "queue_size": queue_size,
        "timestamp": datetime.now().isoformat()
    })

@app.route('/stats', methods=['GET'])
@handle_errors
@login_required
def stats():
    """Get system statistics"""
    with lock:
        queue_size = len(queue)
    with history_lock:
        history_size = len(history)
    
    return jsonify({
        "success": True,
        "stats": {
            "current_queue": queue_size,
            "history_count": history_size,
            "max_capacity": MAX_QUEUE_SIZE,
            "utilization": f"{(queue_size/MAX_QUEUE_SIZE)*100:.1f}%",
            "circuit_breaker_state": circuit_breaker.state,
            "uptime_seconds": time.time() - app.start_time
        }
    })

# ============================================================================
# REQUEST HOOKS
# ============================================================================

@app.before_request
def before_request():
    """Log incoming requests"""
    logger.debug(f"{request.method} {request.path} from {request.remote_addr}")

@app.after_request
def after_request(response):
    """Add CORS headers"""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# ============================================================================
# INITIALIZATION
# ============================================================================

def init_app():
    """Initialize application"""
    app.start_time = time.time()
    
    # Initialize users
    init_default_users()
    
    # Add scheduled tasks
    scheduler.add_task(cleanup_expired_queues, interval=300, name="cleanup_expired")
    scheduler.add_task(log_queue_stats, interval=600, name="log_stats")
    
    # Start scheduler
    scheduler.start()
    
    logger.info("Application initialized successfully")
    logger.info("=" * 60)
    logger.info("DEFAULT ACCOUNTS:")
    logger.info("Admin - username: admin, password: admin123")
    logger.info("User  - username: user, password: user123")
    logger.info("=" * 60)
    logger.info(f"SCHEDULER TOKEN: {SCHEDULER_TOKEN}")
    logger.info("=" * 60)

# ============================================================================
# database connecting
# ============================================================================

def get_db_connection():
    # 1. ดึงค่าจาก Environment Variables ที่เราตั้งไว้ใน Cloud Run
    db_user = os.environ.get('DB_USER')
    db_pass = os.environ.get('DB_PASS')
    db_name = os.environ.get('DB_NAME')
    instance_connection_name = os.environ.get('INSTANCE_CONNECTION_NAME')

    # 2. กำหนดตำแหน่งของ Unix Socket
    # บน Cloud Run จะอยู่ที่ /cloudsql/ ตามด้วย Connection Name เสมอ
    socket_path = f'/cloudsql/{instance_connection_name}'

    # 3. สร้างการเชื่อมต่อ
    try:
        conn = pymysql.connect(
            user=db_user,
            password=db_pass,
            db=db_name,
            unix_socket=socket_path,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )
        return conn
    except Exception as e:
        print(f"Error connecting to DB: {e}")
        return None
    
def get_data():
    db = get_db_connection()
    if db:
        with db.cursor() as cursor:
            cursor.execute("SELECT NOW() as now;") # ทดสอบดึงเวลาปัจจุบันจาก DB
            result = cursor.fetchone()
            print(f"Connected! Database time: {result['now']}")
        db.close()

@app.route('/add-queue', methods=['POST'])
def add_queue():
    name = request.form.get('name')
    phone = request.form.get('phone')
    notes = request.form.get('notes')
    
    db = get_db_connection()
    try:
        with db.cursor() as cursor:
            # 1. หาตำแหน่งคิวล่าสุด (เอา position ล่าสุด + 1)
            cursor.execute("SELECT MAX(position) as max_pos FROM queue_entries WHERE status = 'waiting'")
            result = cursor.fetchone()
            next_position = (result['max_pos'] or 0) + 1
            
            # 2. บันทึกลงตาราง queue_entries
            sql = """
                INSERT INTO queue_entries (queue_id, name, phone, notes, position, status)
                VALUES (%s, %s, %s, %s, %s, 'waiting')
            """
            new_id = str(uuid.uuid4())
            cursor.execute(sql, (new_id, name, phone, notes, next_position))
            
        db.commit()
        return {"status": "success", "queue_id": new_id, "position": next_position}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500
    finally:
        db.close()


if __name__ == '__main__':
    init_app()
    get_data()
    
    try:
        app.run(host="0.0.0.0", port=8080, threaded=True, debug=False)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        scheduler.stop()
    except Exception as e:
        logger.error(f"Application error: {str(e)}")
        scheduler.stop()
        raise