from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
from threading import Lock, RLock, Thread, Event
from collections import deque
from datetime import datetime, timedelta
from functools import wraps
import logging
import time
import json
from enum import Enum
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List
import queue as thread_queue
import secrets
import hashlib

# ============================================================================
# CONFIGURATION & SETUP
# ============================================================================

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # สำหรับ session
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# USER MANAGEMENT
# ============================================================================

class UserRole(Enum):
    """User roles"""
    USER = "user"
    ADMIN = "admin"

@dataclass
class User:
    """User model"""
    username: str
    password_hash: str
    role: UserRole
    created_at: str
    
    def check_password(self, password: str) -> bool:
        """Verify password"""
        return self.password_hash == self._hash_password(password)
    
    @staticmethod
    def _hash_password(password: str) -> str:
        """Hash password with SHA-256"""
        return hashlib.sha256(password.encode()).hexdigest()
    
    def to_dict(self):
        return {
            'username': self.username,
            'role': self.role.value,
            'created_at': self.created_at
        }

# In-memory user database (ในการใช้งานจริงควรใช้ database)
users_db = {}
users_lock = Lock()

def init_default_users():
    """Initialize default admin and test user"""
    with users_lock:
        # Admin account
        users_db['admin'] = User(
            username='admin',
            password_hash=User._hash_password('admin123'),  # เปลี่ยนรหัสผ่านในการใช้งานจริง!
            role=UserRole.ADMIN,
            created_at=datetime.now().isoformat()
        )
        
        # Test user account
        users_db['user'] = User(
            username='user',
            password_hash=User._hash_password('user123'),
            role=UserRole.USER,
            created_at=datetime.now().isoformat()
        )
    
    logger.info("Default users initialized (admin/admin123, user/user123)")

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

class PubSubSystem:
    """Simple pub/sub system for real-time notifications"""
    def __init__(self):
        self.subscribers = {}
        self.lock = Lock()
    
    def subscribe(self, event_type: PubSubEvent):
        subscriber_queue = thread_queue.Queue(maxsize=50)
        with self.lock:
            if event_type not in self.subscribers:
                self.subscribers[event_type] = []
            self.subscribers[event_type].append(subscriber_queue)
        return subscriber_queue
    
    def publish(self, event_type: PubSubEvent, data: dict):
        event_data = {
            'event': event_type.value,
            'data': data,
            'timestamp': datetime.now().isoformat()
        }
        
        with self.lock:
            if event_type in self.subscribers:
                dead_queues = []
                for subscriber_queue in self.subscribers[event_type]:
                    try:
                        subscriber_queue.put_nowait(event_data)
                    except thread_queue.Full:
                        dead_queues.append(subscriber_queue)
                
                for dead_queue in dead_queues:
                    self.subscribers[event_type].remove(dead_queue)
        
        logger.info(f"Published event: {event_type.value}")

pubsub = PubSubSystem()

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
    """Redirect to appropriate page based on login status"""
    if 'username' in session:
        username = session['username']
        with users_lock:
            user = users_db.get(username)
        
        if user and user.role == UserRole.ADMIN:
            return redirect(url_for('admin_dashboard'))
        else:
            return redirect(url_for('user_dashboard'))
    
    return render_template('login.html')

@app.route('/login', methods=['POST'])
@handle_errors
def login():
    """Login endpoint"""
    data = request.json
    if not data:
        raise QueueException("Request body is required", ErrorCode.INVALID_REQUEST)
    
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    if not username or not password:
        raise QueueException("Username and password required", ErrorCode.VALIDATION_ERROR)
    
    with users_lock:
        user = users_db.get(username)
    
    if not user or not user.check_password(password):
        logger.warning(f"Failed login attempt for username: {username}")
        raise QueueException(
            "Invalid username or password",
            ErrorCode.INVALID_CREDENTIALS,
            401
        )
    
    # Set session
    session.permanent = True
    session['username'] = user.username
    session['role'] = user.role.value
    
    logger.info(f"User {username} logged in successfully")
    
    return jsonify({
        'success': True,
        'user': user.to_dict(),
        'redirect': '/admin' if user.role == UserRole.ADMIN else '/user'
    })

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
    """Check authentication status"""
    if 'username' in session:
        username = session['username']
        with users_lock:
            user = users_db.get(username)
        
        if user:
            return jsonify({
                'authenticated': True,
                'user': user.to_dict()
            })
    
    return jsonify({'authenticated': False}), 401

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
    """Get current queue status (All users)"""
    with lock:
        queue_list = [
            entry.to_dict() if isinstance(entry, QueueEntry) else entry
            for entry in queue
        ]
    
    return jsonify({
        "success": True,
        "queue": queue_list,
        "total": len(queue_list),
        "capacity": MAX_QUEUE_SIZE,
        "available_slots": MAX_QUEUE_SIZE - len(queue_list)
    })

@app.route('/next', methods=['POST'])
@handle_errors
@admin_required
@circuit_breaker.call
def next_queue():
    """Call next person in queue (Admin only)"""
    with lock:
        if not queue:
            return jsonify({
                "success": True,
                "called": None,
                "message": "ไม่มีคนในคิว",
                "remaining": 0
            })
        
        called = queue.popleft()
        called_dict = called.to_dict() if isinstance(called, QueueEntry) else called
        
        logger.info(f"Called: {called_dict.get('name')} by {session.get('username')}")
        
        with history_lock:
            history.append({
                **called_dict,
                'called_at': datetime.now().isoformat(),
                'called_by': session.get('username')
            })
        
        remaining = len(queue)
    
    pubsub.publish(PubSubEvent.QUEUE_CALLED, called_dict)
    
    return jsonify({
        "success": True,
        "called": called_dict,
        "remaining": remaining,
        "message": f"เรียก: {called_dict.get('name')}"
    })

@app.route('/history', methods=['GET'])
@handle_errors
@login_required
def show_history():
    """Get queue history (All users)"""
    limit = request.args.get('limit', 50, type=int)
    
    with history_lock:
        history_list = list(history)[-limit:]
    
    return jsonify({
        "success": True,
        "history": history_list,
        "total": len(history_list)
    })

@app.route('/clear', methods=['POST'])
@handle_errors
@admin_required
def clear_queue():
    """Clear the entire queue (Admin only)"""
    with lock:
        cleared_count = len(queue)
        queue.clear()
        logger.info(f"Cleared {cleared_count} items from queue by {session.get('username')}")
    
    pubsub.publish(PubSubEvent.QUEUE_CLEARED, {"cleared_count": cleared_count})
    
    return jsonify({
        "success": True,
        "message": "ล้างคิวเรียบร้อย",
        "cleared": cleared_count
    })

@app.route('/events')
@login_required
def events():
    """Server-Sent Events endpoint for real-time updates"""
    def event_stream():
        queues = [
            pubsub.subscribe(PubSubEvent.QUEUE_ADDED),
            pubsub.subscribe(PubSubEvent.QUEUE_CALLED),
            pubsub.subscribe(PubSubEvent.QUEUE_CLEARED),
            pubsub.subscribe(PubSubEvent.QUEUE_EXPIRED)
        ]
        
        try:
            while True:
                for q in queues:
                    try:
                        event = q.get(timeout=1)
                        yield f"data: {json.dumps(event)}\n\n"
                    except thread_queue.Empty:
                        continue
        except GeneratorExit:
            logger.info("Client disconnected from event stream")
    
    return Response(event_stream(), mimetype='text/event-stream')

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

if __name__ == '__main__':
    init_app()
    
    try:
        app.run(host="0.0.0.0", port=8080, threaded=True, debug=False)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        scheduler.stop()
    except Exception as e:
        logger.error(f"Application error: {str(e)}")
        scheduler.stop()
        raise
