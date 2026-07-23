"""
卡密自动售货系统 — API 路由
认证 / 充值 / 商品 / 下单 / 管理后台
"""
import hashlib
import html
import os
import random
import re
import secrets
import smtplib
import sqlite3
import string
import threading
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from functools import wraps
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

DB_PATH = Path(__file__).parent / "data" / "card_shop.db"

_local = threading.local()


def get_db() -> sqlite3.Connection:
    """线程本地数据库连接"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _ensure_tables(_local.conn)
    return _local.conn


def _ensure_tables(conn: sqlite3.Connection):
    """自动创建缺失的表（迁移用）"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            username TEXT NOT NULL,
            success INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, created_at)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_ver ON email_verifications(email, created_at)")

    # ── 迁移：users 扩展字段 ──
    for col, dflt in [("email", "''"), ("email_verified", "0"),
                       ("vip_level", "'normal'"), ("invite_code", "''"),
                       ("invited_by", "0"), ("total_spent", "0")]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT {dflt}")
        except sqlite3.OperationalError:
            pass

    # ── 迁移：balance_version ──
    try:
        conn.execute("ALTER TABLE users ADD COLUMN balance_version INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE user_tokens ADD COLUMN balance_version INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # ── 迁移：products 扩展字段 ──
    try:
        conn.execute("ALTER TABLE products ADD COLUMN description_html TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    # ── 迁移：orders 扩展字段 ──
    for col, dflt in [("promo_code", "''"), ("discount_amount", "0"), ("discount_detail", "''")]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT DEFAULT {dflt}")
        except sqlite3.OperationalError:
            pass

    # ── 迁移：order_no 字段 ──
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN order_no TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # 为已有订单补生成 order_no
    rows = conn.execute("SELECT id, user_id, created_at FROM orders WHERE order_no='' OR order_no IS NULL").fetchall()
    for r in rows:
        ts = (r["created_at"] or "").replace("-","").replace(":","").replace(" ","")[:14]
        uid = str(r["user_id"]).zfill(4)
        suffix = secrets.token_hex(3).upper()
        order_no = f"{ts}{uid}{suffix}"
        conn.execute("UPDATE orders SET order_no=? WHERE id=?", (order_no, r["id"]))

    # ── 新表：VIP 等级 ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vip_levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level_name TEXT UNIQUE NOT NULL,
            invite_count_required INTEGER DEFAULT 0,
            badge TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 种子数据
    for lvl, req, badge in [("normal", 0, ""), ("vip1", 1, "🥉"), ("vip2", 5, "🥈"), ("svip", 20, "🥇")]:
        conn.execute(
            "INSERT OR IGNORE INTO vip_levels (level_name, invite_count_required, badge) VALUES (?, ?, ?)",
            (lvl, req, badge))

    # ── 新表：VIP 定价 ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_vip_prices (
            product_id INTEGER REFERENCES products(id),
            vip_level TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (product_id, vip_level)
        )
    """)

    # ── 新表：数量折扣 ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quantity_discounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            min_quantity INTEGER NOT NULL,
            discount_percent REAL NOT NULL,
            discount_mode TEXT DEFAULT 'single',
            is_active INTEGER DEFAULT 1,
            start_time TEXT DEFAULT '',
            end_time TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # ── 新表：优惠码 ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            discount_type TEXT DEFAULT 'percent',
            discount_value REAL NOT NULL,
            target_product_id INTEGER DEFAULT 0,
            target_vip_level TEXT DEFAULT '',
            min_order_amount REAL DEFAULT 0,
            max_uses INTEGER DEFAULT 0,
            used_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            start_time TEXT DEFAULT '',
            end_time TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # ── 新表：邀请码记录 ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invite_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            owner_id INTEGER REFERENCES users(id),
            used_count INTEGER DEFAULT 0,
            max_uses INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # ── 新表：站点配置 ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS site_config (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)
    # 种子配置
    for k, v in [("qq_contact", ""), ("qq_contact_text", "联系客服"), ("site_notice", "")]:
        conn.execute("INSERT OR IGNORE INTO site_config (key, value) VALUES (?, ?)", (k, v))

    # ── 新表：管理员审计日志 ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            target_type TEXT DEFAULT '',
            target_id INTEGER DEFAULT 0,
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_admin ON admin_audit_log(admin_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_target ON admin_audit_log(target_type, target_id)")

    # 为没有 invite_code 的老用户自动生成
    rows = conn.execute("SELECT id FROM users WHERE invite_code='' OR invite_code IS NULL").fetchall()
    for r in rows:
        code = f"INV{secrets.token_hex(6).upper()}"
        conn.execute("UPDATE users SET invite_code=? WHERE id=?", (code, r["id"]))
        conn.execute("INSERT OR IGNORE INTO invite_codes (code, owner_id) VALUES (?, ?)", (code, r["id"]))

    conn.commit()


# ═══════════════════════════════════════════════════════════════
# Security helpers
# ═══════════════════════════════════════════════════════════════

_ALLOWED_TAGS = {'p', 'br', 'b', 'i', 'u', 'strong', 'em', 'a', 'img', 'ul', 'ol', 'li', 'span', 'div', 'h3', 'h4'}
_ALLOWED_ATTRS = {'href', 'src', 'alt', 'style', 'class', 'title', 'width', 'height'}

def _sanitize_html(text: str) -> str:
    """净化 HTML：只保留安全标签和属性，去除 script/iframe/事件处理等。"""
    if not text or not text.strip():
        return ""
    # 1. 去掉危险标签
    text = re.sub(r'<\s*(script|iframe|object|embed|form|input|link|meta|style|svg|math)[^>]*>.*?<\s*/\s*\1\s*>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<\s*(script|iframe|object|embed|form|input|link|meta|style|svg|math)\b[^>]*/?>', '', text, flags=re.IGNORECASE)
    # 2. 去掉 on* 事件属性
    text = re.sub(r'\s+on\w+\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+on\w+\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)
    # 3. 去掉 javascript: / data: 协议
    text = re.sub(r'''\b(href|src|action)\s*=\s*["']\s*(javascript|data|vbscript):''', r'\1="about:blank"', text, flags=re.IGNORECASE)
    return text.strip()


# ═══════════════════════════════════════════════════════════════
# Auth helpers
# ═══════════════════════════════════════════════════════════════

def _hash(password: str, salt: str = None) -> tuple[str, str]:
    """pbkdf2_hmac 密码哈希。不传 salt 则自动生成。返回 (hash_hex, salt_hex)"""
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 200000, dklen=32)
    return key.hex(), salt


def _verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    """验证密码，兼容旧版 SHA256 哈希。"""
    # 新版 pbkdf2：salt 是 32 字符 hex
    if len(stored_salt) == 32:
        key, _ = _hash(password, stored_salt)
        return key == stored_hash
    # 旧版 plain SHA256（salt 为 16 字符 hex）
    h = hashlib.sha256((password + stored_salt).encode()).hexdigest()
    return h == stored_hash


def _needs_password_upgrade(salt: str) -> bool:
    """检查是否旧版 SHA256 salt（16 字符），需要升级到 pbkdf2"""
    return len(salt) == 16


def get_user(request: Request) -> Optional[dict]:
    """从请求头解析当前用户（作为 FastAPI 依赖使用）"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    db = get_db()
    row = None
    try:
        row = db.execute(
            "SELECT u.*, t.balance_version as token_balance_version FROM users u JOIN user_tokens t ON u.id=t.user_id "
            "WHERE t.token=? AND t.expires_at > datetime('now', 'localtime')",
            (token,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Fallback for DB without balance_version migration yet
        row = db.execute(
            "SELECT u.* FROM users u JOIN user_tokens t ON u.id=t.user_id "
            "WHERE t.token=? AND t.expires_at > datetime('now', 'localtime')",
            (token,),
        ).fetchone()
    return dict(row) if row else None


def require_user(request: Request) -> dict:
    """要求登录"""
    user = get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_admin(request: Request) -> dict:
    """要求管理员"""
    user = require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _audit_log(db, admin_id: int, action: str, target_type: str = "", target_id: int = 0, detail: str = ""):
    """记录管理员操作"""
    db.execute(
        "INSERT INTO admin_audit_log (admin_id, action, target_type, target_id, detail) VALUES (?, ?, ?, ?, ?)",
        (admin_id, action, target_type, target_id, detail),
    )


# ═══════════════════════════════════════════════════════════════
# Request/Response models
# ═══════════════════════════════════════════════════════════════

class RegisterReq(BaseModel):
    username: str
    password: str
    email: str = ""
    invite_code: str = ""

class LoginReq(BaseModel):
    username: str
    password: str

class SendCodeReq(BaseModel):
    email: str

class VerifyEmailReq(BaseModel):
    email: str
    code: str

class RedeemReq(BaseModel):
    card_no: str
    card_password: str

class CreateOrderReq(BaseModel):
    product_id: int
    quantity: int = 1
    promo_code: str = ""

class GenerateCardsReq(BaseModel):
    amount: float
    count: int
    batch: str = ""

class ProductReq(BaseModel):
    category_id: int = 0
    name: str
    description: str = ""
    description_html: str = ""
    price: float = 0

class ImportInventoryReq(BaseModel):
    product_id: int
    data: str

class UpdateConfigReq(BaseModel):
    key: str
    value: str

# ── 新模型 ──
class ValidatePromoReq(BaseModel):
    code: str
    order_total: float
    product_id: int = 0

class PromoCodeReq(BaseModel):
    code: str
    discount_type: str = "percent"
    discount_value: float
    target_product_id: int = 0
    target_vip_level: str = ""
    min_order_amount: float = 0
    max_uses: int = 0
    start_time: str = ""
    end_time: str = ""

class QtyDiscountReq(BaseModel):
    product_id: int
    min_quantity: int
    discount_percent: float
    discount_mode: str = "single"
    start_time: str = ""
    end_time: str = ""

class VipPriceReq(BaseModel):
    product_id: int
    prices: dict  # {"vip1": 10.0, "vip2": 8.0, "svip": 5.0}

class UserUpdateReq(BaseModel):
    vip_level: str = ""
    password: str = ""

class BatchInventoryReq(BaseModel):
    ids: list[int]
    action: str  # "delete" | "set_status"
    status: str = ""  # for set_status

class BatchUserBalanceReq(BaseModel):
    ids: list[int]
    action: str = "set"  # "set" | "add" | "subtract"
    amount: float = 0
    reason: str = ""

class UserBalanceReq(BaseModel):
    amount: float
    reason: str = ""


# ═══════════════════════════════════════════════════════════════
# 登录防爆破
# ═══════════════════════════════════════════════════════════════

# 限制参数
IP_MAX_FAILS = 5          # 同 IP 15 分钟内最大失败次数
IP_WINDOW_MIN = 15
USER_MAX_FAILS = 10       # 同用户名 30 分钟内最大失败次数 → 锁号
USER_WINDOW_MIN = 30

# 内存缓存：IP → (fail_count, first_fail_time, blocked_until)
_ip_fail_cache: dict[str, tuple[int, float, float]] = {}
_email_ip_cache: dict[str, tuple[int, float, float]] = {}
_cache_lock = threading.Lock()


def _check_login_rate_limit(db: sqlite3.Connection, ip: str, username: str) -> Optional[str]:
    """检查登录频率限制，返回 None 表示通过，否则返回错误信息。"""

    now = time.time()

    # ── 1. 内存层：IP 频控 ──
    with _cache_lock:
        if ip in _ip_fail_cache:
            cnt, first, blocked = _ip_fail_cache[ip]
            if blocked > now:
                remain = int(blocked - now)
                return f"登录过于频繁，IP已被临时限制，请 {remain} 秒后重试"
            if now - first > IP_WINDOW_MIN * 60:
                # 窗口过期，重置
                del _ip_fail_cache[ip]

    # ── 2. DB 层：用户名频控 ──
    cutoff = (datetime.now() - timedelta(minutes=USER_WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM login_attempts "
        "WHERE username=? AND success=0 AND created_at > ?",
        (username, cutoff),
    ).fetchone()
    if row and row["cnt"] >= USER_MAX_FAILS:
        return f"该账号已被临时锁定（{USER_WINDOW_MIN} 分钟内失败 {USER_MAX_FAILS} 次），请稍后再试"

    return None  # 通过


def _record_login_attempt(db: sqlite3.Connection, ip: str, username: str, success: bool):
    """记录登录尝试。同时清理超时的 IP 缓存记录。"""
    now = time.time()

    db.execute(
        "INSERT INTO login_attempts (ip, username, success) VALUES (?, ?, ?)",
        (ip, username, 1 if success else 0),
    )

    # 成功登录 → 清除该 IP 和用户名的失败记录（重置计数）
    if success:
        db.execute("DELETE FROM login_attempts WHERE ip=? AND success=0", (ip,))
        db.execute("DELETE FROM login_attempts WHERE username=? AND success=0", (username,))
        with _cache_lock:
            _ip_fail_cache.pop(ip, None)
    else:
        with _cache_lock:
            if ip in _ip_fail_cache:
                cnt, first, _ = _ip_fail_cache[ip]
                _ip_fail_cache[ip] = (cnt + 1, first, now + IP_WINDOW_MIN * 60 if cnt + 1 >= IP_MAX_FAILS else 0)
            else:
                _ip_fail_cache[ip] = (1, now, 0)

    # 定期清理旧记录（约 1/50 概率触发）
    if random.random() < 0.02:
        old = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("DELETE FROM login_attempts WHERE created_at < ?", (old,))
        # 清理内存中过期的 IP
        with _cache_lock:
            expired = [k for k, v in _ip_fail_cache.items() if now - v[1] > 3600]
            for k in expired:
                del _ip_fail_cache[k]


# ═══════════════════════════════════════════════════════════════
# 邮箱发送
# ═══════════════════════════════════════════════════════════════

# SMTP 配置 — 优先从环境变量读取，否则使用 QQ 邮箱默认值
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")       # 发件邮箱地址，如 123456@qq.com
SMTP_PASS = os.environ.get("SMTP_PASS", "")       # QQ邮箱需用授权码，不是登录密码
SITE_NAME = os.environ.get("SITE_NAME", "卡密商城")

# 验证码有效期 10 分钟
CODE_EXPIRE_MIN = 10
# 同一邮箱 60 秒内只能发一次
CODE_COOLDOWN_SEC = 60


def _send_email(to_email: str, subject: str, html_body: str) -> str:
    """发送邮件。成功返回空字符串，失败返回错误信息。"""
    if not SMTP_USER or not SMTP_PASS:
        return "SMTP 未配置，请联系管理员"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((SITE_NAME, SMTP_USER))
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
            server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())
        server.quit()
        return ""
    except Exception as e:
        error = str(e)
        # 隐藏密码等敏感信息
        if "535" in error or "authentication" in error.lower():
            return "邮箱验证失败，请检查 SMTP 配置（QQ邮箱需使用授权码）"
        if "timeout" in error.lower() or "refused" in error.lower():
            return "邮件服务器连接失败，请检查 SMTP_HOST 和 SMTP_PORT"
        return f"邮件发送失败: {error[:100]}"


@router.post("/shop/auth/send_code")
async def send_verification_code(req: SendCodeReq, request: Request):
    """发送邮箱验证码"""
    db = get_db()
    email = req.email.strip().lower()

    # 基本校验
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    # 检查是否已注册
    existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        raise HTTPException(status_code=400, detail="该邮箱已被注册")

    # 冷却检查：60 秒内不能重复发送
    recent = db.execute(
        "SELECT created_at FROM email_verifications WHERE email=? "
        "ORDER BY created_at DESC LIMIT 1", (email,)
    ).fetchone()
    if recent:
        last_time = datetime.strptime(recent["created_at"], "%Y-%m-%d %H:%M:%S")
        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed < CODE_COOLDOWN_SEC:
            raise HTTPException(status_code=429, detail=f"请 {int(CODE_COOLDOWN_SEC - elapsed)} 秒后再试")

    # IP 限制：每 IP 每小时最多 5 次请求
    ip = request.client.host if request.client else "unknown"
    with _cache_lock:
        if ip in _email_ip_cache:
            cnt, first, blocked = _email_ip_cache[ip]
            if blocked > time.time():
                remain = int(blocked - time.time())
                raise HTTPException(status_code=429, detail=f"请求过于频繁，请 {remain} 秒后重试")
            if time.time() - first > 3600:
                del _email_ip_cache[ip]

    now_ts = time.time()
    with _cache_lock:
        if ip in _email_ip_cache:
            cnt, first, _ = _email_ip_cache[ip]
            _email_ip_cache[ip] = (cnt + 1, first, now_ts + 3600 if cnt + 1 >= 5 else 0)
        else:
            _email_ip_cache[ip] = (1, now_ts, 0)

    # 全局限流
    hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    total_recent = db.execute(
        "SELECT COUNT(*) as cnt FROM email_verifications WHERE created_at > ?", (hour_ago,)
    ).fetchone()
    if total_recent and total_recent["cnt"] > 100:
        raise HTTPException(status_code=429, detail="系统繁忙，请稍后再试")

    # 生成 6 位数字验证码
    code = f"{random.randint(0, 999999):06d}"
    expires = (datetime.now() + timedelta(minutes=CODE_EXPIRE_MIN)).strftime("%Y-%m-%d %H:%M:%S")

    db.execute(
        "INSERT INTO email_verifications (email, code, expires_at) VALUES (?, ?, ?)",
        (email, code, expires),
    )
    db.commit()

    # 发送邮件
    html_body = f"""
    <div style="max-width:480px;margin:0 auto;padding:24px;font-family:'Microsoft YaHei',sans-serif;
                background:#161b22;color:#c9d1d9;border-radius:8px;border:1px solid #30363d;">
        <h2 style="color:#58a6ff;margin:0 0 16px;">{SITE_NAME} — 邮箱验证</h2>
        <p style="margin:0 0 12px;">您的验证码是：</p>
        <div style="background:#0d1117;padding:20px;text-align:center;border-radius:6px;margin:0 0 16px;">
            <span style="font-size:32px;font-weight:bold;color:#3fb950;letter-spacing:8px;">{code}</span>
        </div>
        <p style="color:#8b949e;font-size:13px;margin:0;">
            验证码 {CODE_EXPIRE_MIN} 分钟内有效。如非本人操作，请忽略此邮件。
        </p>
    </div>"""

    err = _send_email(email, f"{SITE_NAME} 邮箱验证码", html_body)
    if err:
        # 发送失败但验证码已入库 — 保留记录方便调试，但告诉用户
        raise HTTPException(status_code=503, detail=err)

    return {"status": "ok", "message": f"验证码已发送至 {email}，{CODE_EXPIRE_MIN} 分钟内有效"}


@router.post("/shop/auth/verify_email")
async def verify_email(req: VerifyEmailReq):
    """验证邮箱验证码"""
    db = get_db()
    email = req.email.strip().lower()
    code = req.code.strip()

    row = db.execute(
        "SELECT * FROM email_verifications WHERE email=? AND code=? AND used=0 "
        "AND expires_at > datetime('now', 'localtime') ORDER BY id DESC LIMIT 1",
        (email, code),
    ).fetchone()

    if not row:
        # 检查是不是已经用过了
        used = db.execute(
            "SELECT id FROM email_verifications WHERE email=? AND code=? AND used=1", (email, code)
        ).fetchone()
        if used:
            raise HTTPException(status_code=400, detail="验证码已使用")
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    # 标记为已使用
    db.execute("UPDATE email_verifications SET used=1 WHERE id=?", (row["id"],))
    db.commit()

    return {"status": "ok", "message": "邮箱验证通过", "email": email}


# ═══════════════════════════════════════════════════════════════
# 认证
# ═══════════════════════════════════════════════════════════════

def _check_and_upgrade_vip(db: sqlite3.Connection, user_id: int):
    """根据邀请人数自动升级 VIP"""
    count = db.execute(
        "SELECT COUNT(*) as cnt FROM users WHERE invited_by=?", (user_id,)
    ).fetchone()["cnt"]
    level = db.execute(
        "SELECT level_name FROM vip_levels WHERE invite_count_required<=? "
        "ORDER BY invite_count_required DESC LIMIT 1", (count,)
    ).fetchone()
    if level:
        db.execute("UPDATE users SET vip_level=? WHERE id=?", (level["level_name"], user_id))


@router.post("/shop/auth/register")
async def auth_register(req: RegisterReq, request: Request):
    """用户注册 — 需先验证邮箱，注册时传入已验证的邮箱"""
    db = get_db()
    username = req.username.strip()
    email = req.email.strip().lower()
    invite_code = req.invite_code.strip().upper() if req.invite_code else ""

    if len(username) < 2 or len(username) > 20:
        raise HTTPException(status_code=400, detail="用户名2-20个字符")
    if not re.match(r'^[一-鿿\w]+$', username):
        raise HTTPException(status_code=400, detail="用户名只能包含中英文、数字、下划线")
    if username.lower() in ("admin", "root", "system", "test", "api", "shop", "客服", "管理员"):
        raise HTTPException(status_code=400, detail="该用户名已被系统保留")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="密码至少8位")
    types = 0
    if any(c.islower() for c in req.password): types += 1
    if any(c.isupper() for c in req.password): types += 1
    if any(c.isdigit() for c in req.password): types += 1
    if any(not c.isalnum() for c in req.password): types += 1
    if types < 3:
        raise HTTPException(status_code=400, detail="密码需包含大写字母、小写字母、数字、符号中至少3种")

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="请先验证邮箱")

    verified = db.execute(
        "SELECT id FROM email_verifications WHERE email=? AND used=1 ORDER BY id DESC LIMIT 1",
        (email,),
    ).fetchone()
    if not verified:
        raise HTTPException(status_code=400, detail="该邮箱未通过验证，请先获取验证码")

    existing_email = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing_email:
        raise HTTPException(status_code=400, detail="该邮箱已被注册")

    # 邀请码处理
    invited_by_id = 0
    if invite_code:
        inviter = db.execute("SELECT id FROM users WHERE invite_code=?", (invite_code,)).fetchone()
        if not inviter:
            raise HTTPException(status_code=400, detail="邀请码无效")
        if inviter["id"] == 0:  # admin 的邀请码，或者无效
            pass
        else:
            invited_by_id = inviter["id"]

    ip = request.client.host if request.client else "unknown"
    today = datetime.now().strftime("%Y-%m-%d")
    reg_ip_rows = db.execute(
        "SELECT COUNT(*) as cnt FROM login_attempts WHERE ip=? AND success=1 AND created_at >= ?",
        (ip, today),
    ).fetchone()
    if reg_ip_rows and reg_ip_rows["cnt"] > 10:
        raise HTTPException(status_code=429, detail="该IP今日注册已达上限")

    ph, salt = _hash(req.password)
    try:
        cursor = db.execute(
            "INSERT INTO users (username, password_hash, salt, email, email_verified, balance, invited_by) "
            "VALUES (?, ?, ?, ?, 1, 0, ?)",
            (username, ph, salt, email, invited_by_id),
        )
        user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="用户名已存在")

    # 给新用户生成自己的邀请码
    own_code = f"INV{secrets.token_hex(4).upper()}"
    db.execute("UPDATE users SET invite_code=? WHERE id=?", (own_code, user_id))
    db.execute("INSERT OR IGNORE INTO invite_codes (code, owner_id) VALUES (?, ?)", (own_code, user_id))

    # 升级邀请人的 VIP
    if invited_by_id:
        db.execute("UPDATE invite_codes SET used_count=used_count+1 WHERE code=?", (invite_code,))
        _check_and_upgrade_vip(db, invited_by_id)

    db.commit()
    return {"status": "ok", "message": "注册成功，请登录"}


@router.post("/shop/auth/login")
async def auth_login(req: LoginReq, request: Request):
    """用户登录 — 带防爆破保护"""
    db = get_db()
    username = req.username.strip()
    ip = request.client.host if request.client else "unknown"

    # 1. 检查速率限制
    limit_msg = _check_login_rate_limit(db, ip, username)
    if limit_msg:
        raise HTTPException(status_code=429, detail=limit_msg)

    # 2. 验证凭据
    row = db.execute(
        "SELECT * FROM users WHERE username=?", (username,)
    ).fetchone()

    if not row:
        _record_login_attempt(db, ip, username, False)
        db.commit()
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    user = dict(row)
    if not _verify_password(req.password, user["password_hash"], user["salt"]):
        _record_login_attempt(db, ip, username, False)
        db.commit()
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 自动升级旧版 SHA256 → pbkdf2
    if _needs_password_upgrade(user["salt"]):
        ph_new, salt_new = _hash(req.password)
        db.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?", (ph_new, salt_new, user["id"]))

    # 3. 登录成功
    _record_login_attempt(db, ip, username, True)

    # 生成 token
    token = secrets.token_hex(32)
    expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    bv = user.get("balance_version", 0) or 0
    db.execute(
        "INSERT INTO user_tokens (user_id, token, expires_at, balance_version) VALUES (?, ?, ?, ?)",
        (user["id"], token, expires, bv),
    )
    # 清理过期 token
    db.execute("DELETE FROM user_tokens WHERE expires_at < datetime('now', 'localtime')")
    db.commit()

    invite_count = db.execute(
        "SELECT COUNT(*) as cnt FROM users WHERE invited_by=?", (user["id"],)
    ).fetchone()["cnt"]
    return {
        "status": "ok",
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user.get("email", ""),
            "balance": user["balance"],
            "balance_version": bv,
            "is_admin": bool(user["is_admin"]),
            "vip_level": user.get("vip_level", "normal"),
            "invite_code": user.get("invite_code", ""),
            "invite_count": invite_count,
        },
    }


@router.post("/shop/auth/logout")
async def auth_logout(request: Request):
    """用户登出 — 删除 token"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        db = get_db()
        db.execute("DELETE FROM user_tokens WHERE token=?", (token,))
        db.commit()
    return {"status": "ok", "message": "已登出"}


@router.get("/shop/auth/me")
async def auth_me(request: Request):
    """获取当前用户信息 — 自动同步余额版本号"""
    user = require_user(request)
    db = get_db()

    # 如果管理员改过余额，自动同步 token 版本号
    user_bv = user.get("balance_version") or 0
    token_bv = user.get("token_balance_version") or 0
    needs_refresh = token_bv is not None and (token_bv != user_bv)

    if needs_refresh:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if token:
            try:
                db.execute(
                    "UPDATE user_tokens SET balance_version=? WHERE token=?",
                    (user_bv, token),
                )
                db.commit()
            except sqlite3.OperationalError:
                pass  # column not yet migrated

    invite_count = db.execute(
        "SELECT COUNT(*) as cnt FROM users WHERE invited_by=?", (user["id"],)
    ).fetchone()["cnt"]
    return {
        "status": "ok",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user.get("email", ""),
            "balance": user["balance"],
            "balance_version": user.get("balance_version", 0) or 0,
            "is_admin": bool(user["is_admin"]),
            "vip_level": user.get("vip_level", "normal"),
            "invite_code": user.get("invite_code", ""),
            "invite_count": invite_count,
            "total_spent": float(user.get("total_spent", 0)),
        },
    }


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str


@router.post("/shop/auth/change_password")
async def change_password(req: ChangePasswordReq, request: Request):
    """修改密码"""
    user = require_user(request)
    db = get_db()

    if not _verify_password(req.old_password, user["password_hash"], user["salt"]):
        raise HTTPException(status_code=400, detail="原密码错误")

    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码至少8位")
    types = 0
    if any(c.islower() for c in req.new_password): types += 1
    if any(c.isupper() for c in req.new_password): types += 1
    if any(c.isdigit() for c in req.new_password): types += 1
    if any(not c.isalnum() for c in req.new_password): types += 1
    if types < 3:
        raise HTTPException(status_code=400, detail="新密码需包含大写字母、小写字母、数字、符号中至少3种")

    ph_new, salt_new = _hash(req.new_password)
    db.execute(
        "UPDATE users SET password_hash=?, salt=? WHERE id=?",
        (ph_new, salt_new, user["id"]),
    )
    db.execute("DELETE FROM user_tokens WHERE user_id=?", (user["id"],))
    db.commit()

    return {"status": "ok", "message": "密码修改成功，请重新登录"}


# ===============================================================
# 充值
# ===============================================================

@router.post("/shop/redeem")
async def redeem_card(req: RedeemReq, request: Request):
    """使用充值卡密"""
    user = require_user(request)
    db = get_db()

    card = db.execute(
        "SELECT * FROM recharge_cards WHERE card_no=? AND card_password=?",
        (req.card_no.strip(), req.card_password.strip()),
    ).fetchone()

    if not card:
        raise HTTPException(status_code=400, detail="卡密无效，请检查卡号和密码")
    if card["status"] != "unused":
        raise HTTPException(status_code=400, detail="此卡密已被使用")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 更新卡密状态
    db.execute(
        "UPDATE recharge_cards SET status='used', used_by=?, used_at=? WHERE id=?",
        (user["id"], now, card["id"]),
    )
    # 使用记录
    db.execute(
        "INSERT INTO card_usage (card_id, user_id, amount, used_at) VALUES (?, ?, ?, ?)",
        (card["id"], user["id"], card["amount"], now),
    )
    # 加余额
    db.execute(
        "UPDATE users SET balance=balance+? WHERE id=?",
        (card["amount"], user["id"]),
    )
    db.commit()

    new_balance = db.execute(
        "SELECT balance FROM users WHERE id=?", (user["id"],)
    ).fetchone()["balance"]

    return {
        "status": "ok",
        "message": f"充值成功！到账 {card['amount']:.2f} 元",
        "amount": card["amount"],
        "balance": new_balance,
    }


# ═══════════════════════════════════════════════════════════════
# 商品
# ═══════════════════════════════════════════════════════════════

@router.get("/shop/products")
async def list_products():
    """商品列表"""
    db = get_db()
    rows = db.execute(
        """SELECT p.*, c.name as category_name,
                  (SELECT COUNT(*) FROM inventory WHERE product_id=p.id AND status='available') as available
           FROM products p
           LEFT JOIN categories c ON p.category_id=c.id
           WHERE p.is_active=1
           ORDER BY p.sort_order, p.id"""
    ).fetchall()

    products = []
    for r in rows:
        d = dict(r)
        d["is_active"] = bool(d.get("is_active", 1))
        products.append(d)
    return {"status": "ok", "products": products}


@router.get("/shop/products/{product_id}")
async def get_product(product_id: int):
    """商品详情"""
    db = get_db()
    row = db.execute(
        """SELECT p.*, c.name as category_name,
                  (SELECT COUNT(*) FROM inventory WHERE product_id=p.id AND status='available') as available
           FROM products p
           LEFT JOIN categories c ON p.category_id=c.id
           WHERE p.id=?""",
        (product_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="商品不存在")
    d = dict(row)
    d["is_active"] = bool(d.get("is_active", 1))
    return {"status": "ok", "product": d}


@router.get("/shop/categories")
async def list_categories():
    """分类列表"""
    db = get_db()
    rows = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
    return {"status": "ok", "categories": [dict(r) for r in rows]}


# ═══════════════════════════════════════════════════════════════
# 订单
# ═══════════════════════════════════════════════════════════════

@router.post("/shop/orders")
async def create_order(req: CreateOrderReq, request: Request):
    """下单购买 — VIP固定价 / 普通用户可叠加折扣+优惠码"""
    user = require_user(request)
    db = get_db()

    # 检查余额是否被管理员修改过
    user_bv = (user.get("balance_version") or 0)
    token_bv = (user.get("token_balance_version") or 0)
    if token_bv != user_bv:
        raise HTTPException(status_code=409, detail="您的余额已被管理员调整，请刷新页面后重试")

    db.execute("BEGIN IMMEDIATE")
    try:

        product = db.execute(
            "SELECT * FROM products WHERE id=? AND is_active=1", (req.product_id,)
        ).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="商品不存在或已下架")
        if req.quantity < 1:
            raise HTTPException(status_code=400, detail="购买数量至少为1")

        available = db.execute(
            "SELECT COUNT(*) as cnt FROM inventory WHERE product_id=? AND status='available'",
            (req.product_id,),
        ).fetchone()["cnt"]
        if available < req.quantity:
            raise HTTPException(status_code=400, detail=f"库存不足，仅剩 {available} 件")

        user_row = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        vip_level = dict(user_row).get("vip_level", "normal")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        discount_detail = []
        unit_price = product["price"]
        total_discount = 0.0
        promo_code_used = ""

        # ── VIP 用户：固定价，无折扣 ──
        if vip_level and vip_level != "normal":
            vip_price_row = db.execute(
                "SELECT price FROM product_vip_prices WHERE product_id=? AND vip_level=?",
                (req.product_id, vip_level)
            ).fetchone()
            if vip_price_row and vip_price_row["price"] > 0:
                unit_price = vip_price_row["price"]
                discount_detail.append({"type": "vip", "level": vip_level, "unit_price": unit_price})
            total = unit_price * req.quantity
        else:
            total = product["price"] * req.quantity

            # 1. 数量折扣
            qty_row_single = db.execute(
                """SELECT * FROM quantity_discounts
                   WHERE product_id=? AND is_active=1 AND discount_mode='single'
                   AND min_quantity<=? AND (start_time='' OR start_time<=?)
                   AND (end_time='' OR end_time>=?)
                   ORDER BY min_quantity DESC LIMIT 1""",
                (req.product_id, req.quantity, now, now)
            ).fetchone()
            hist_qty = db.execute(
                "SELECT COALESCE(SUM(quantity),0) as total FROM orders WHERE user_id=? AND product_id=?",
                (user["id"], req.product_id)
            ).fetchone()["total"]
            total_qty = hist_qty + req.quantity
            qty_row_cumul = db.execute(
                """SELECT * FROM quantity_discounts
                   WHERE product_id=? AND is_active=1 AND discount_mode='cumulative'
                   AND min_quantity<=? AND (start_time='' OR start_time<=?)
                   AND (end_time='' OR end_time>=?)
                   ORDER BY min_quantity DESC LIMIT 1""",
                (req.product_id, total_qty, now, now)
            ).fetchone()

            best_qty_discount = 0.0
            qty_desc = ""
            if qty_row_single and qty_row_single["discount_percent"] > best_qty_discount:
                best_qty_discount = qty_row_single["discount_percent"]
                qty_desc = f"单次满{qty_row_single['min_quantity']}件享{best_qty_discount}%折扣"
            if qty_row_cumul and qty_row_cumul["discount_percent"] > best_qty_discount:
                best_qty_discount = qty_row_cumul["discount_percent"]
                qty_desc = f"累计满{qty_row_cumul['min_quantity']}件享{best_qty_discount}%折扣"

            if best_qty_discount > 0:
                qty_discount_amount = total * best_qty_discount / 100.0
                total -= qty_discount_amount
                total_discount += qty_discount_amount
                discount_detail.append({"type": "qty_discount", "percent": best_qty_discount,
                                         "amount": round(qty_discount_amount, 2), "desc": qty_desc})

            # 2. 优惠码
            if req.promo_code:
                promo = db.execute(
                    """SELECT * FROM promo_codes WHERE code=? AND is_active=1
                       AND (start_time='' OR start_time<=?) AND (end_time='' OR end_time>=?)""",
                    (req.promo_code.strip().upper(), now, now)
                ).fetchone()
                if not promo:
                    raise HTTPException(status_code=400, detail="优惠码无效或已过期")
                if promo["max_uses"] > 0 and promo["used_count"] >= promo["max_uses"]:
                    raise HTTPException(status_code=400, detail="优惠码已被用完")
                if promo["target_product_id"] > 0 and promo["target_product_id"] != req.product_id:
                    raise HTTPException(status_code=400, detail="此优惠码不适用于该商品")
                if total < promo["min_order_amount"]:
                    raise HTTPException(status_code=400,
                        detail=f"订单金额需满 {promo['min_order_amount']:.2f} 元才能使用此优惠码")
                if promo["discount_type"] == "percent":
                    promo_amount = total * promo["discount_value"] / 100.0
                else:
                    promo_amount = min(promo["discount_value"], total)
                total -= promo_amount
                total_discount += promo_amount
                discount_detail.append({"type": "promo", "code": promo["code"],
                                         "amount": round(promo_amount, 2)})
                db.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE id=?", (promo["id"],))
                promo_code_used = promo["code"]

        total = round(total, 2)
        if total < 0:
            total = 0

        if user_row["balance"] < total:
            raise HTTPException(status_code=400,
                detail=f"余额不足，需要 {total:.2f} 元，当前余额 {user_row['balance']:.2f} 元")

        # 扣余额
        db.execute("UPDATE users SET balance=balance-?, total_spent=total_spent+? WHERE id=?",
                   (total, total, user["id"]))

        # 锁库存（BEGIN IMMEDIATE 已锁定写操作，安全性由 SQLite 保证）
        items = db.execute(
            "SELECT id FROM inventory WHERE product_id=? AND status='available' ORDER BY id LIMIT ?",
            (req.product_id, req.quantity),
        ).fetchall()
        if len(items) < req.quantity:
            raise HTTPException(status_code=400, detail="库存不足（并发冲突），请重试")

        # 创建订单
        import json as _json
        detail_json = _json.dumps(discount_detail, ensure_ascii=False) if discount_detail else ""
        # 生成唯一订单号：时间戳 + 用户ID + 随机6位
        ts = now.replace("-", "").replace(":", "").replace(" ", "")
        uid_str = str(user["id"]).zfill(4)
        suffix = secrets.token_hex(3).upper()
        order_no = f"{ts}{uid_str}{suffix}"
        cursor = db.execute(
            """INSERT INTO orders (user_id, product_id, product_name, quantity, unit_price, total_price,
               discount_amount, discount_detail, promo_code, status, order_no, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'paid', ?, ?)""",
            (user["id"], product["id"], product["name"], req.quantity,
             unit_price, total, total_discount, detail_json, promo_code_used, order_no, now),
        )
        order_id = cursor.lastrowid

        # 发货
        delivered = []
        for item in items:
            db.execute("UPDATE inventory SET status='sold', order_id=?, sold_at=? WHERE id=?",
                       (order_id, now, item["id"]))
            db.execute("INSERT INTO order_items (order_id, inventory_id) VALUES (?, ?)",
                       (order_id, item["id"]))
            inv = db.execute(
                "SELECT account, password, recovery_email FROM inventory WHERE id=?", (item["id"],)
            ).fetchone()
            delivered.append(dict(inv))

        db.execute(
            "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory WHERE product_id=? AND status='available') WHERE id=?",
            (product["id"], product["id"]),
        )
        db.commit()

        new_balance = db.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()["balance"]

        return {
            "status": "ok",
            "message": f"购买成功！共 {req.quantity} 件，消费 {total:.2f} 元",
            "order": {"id": order_id, "order_no": order_no, "product_name": product["name"], "quantity": req.quantity,
                       "unit_price": unit_price, "total_price": total, "discount": total_discount,
                       "discount_detail": discount_detail, "created_at": now},
            "cards": delivered,
            "balance": new_balance,
        }
    except HTTPException:
        db.execute("ROLLBACK")
        raise
    except Exception:
        db.execute("ROLLBACK")
        raise


@router.get("/shop/orders")
async def list_my_orders(
    request: Request,
    user_id: int = 0,
    status: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
):
    """我的订单 / admin可查任意用户。支持 status 筛选、search 搜索"""
    req_user = require_user(request)
    db = get_db()
    uid = user_id if (user_id > 0 and req_user.get("is_admin")) else req_user["id"]

    where = ["o.user_id=?"]
    params = [uid]

    if status:
        where.append("o.status=?")
        params.append(status)

    if search:
        where.append("(o.order_no LIKE ? OR CAST(o.id AS TEXT) LIKE ? OR o.product_name LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])

    where_clause = " AND ".join(where)
    total = db.execute(
        f"SELECT COUNT(*) as cnt FROM orders o WHERE {where_clause}", params
    ).fetchone()["cnt"]

    offset = (page - 1) * page_size
    rows = db.execute(
        f"""SELECT o.*,
                  (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) as item_count
           FROM orders o WHERE {where_clause}
           ORDER BY o.created_at DESC LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()
    orders = []
    for row in rows:
        order = dict(row)
        items = db.execute(
            """SELECT i.account, i.password, i.recovery_email
               FROM order_items oi JOIN inventory i ON oi.inventory_id=i.id
               WHERE oi.order_id=?""", (row["id"],)
        ).fetchall()
        order["cards"] = [dict(it) for it in items]
        if order.get("discount_detail"):
            import json as _json
            try:
                order["discount_detail"] = _json.loads(order["discount_detail"])
            except:
                pass
        orders.append(order)
    return {"status": "ok", "orders": orders, "total": total, "page": page}


@router.get("/shop/orders/{order_id}")
async def get_order_detail(order_id: int, request: Request):
    """订单详情（含卡密）"""
    user = require_user(request)
    db = get_db()

    order = db.execute(
        "SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user["id"])
    ).fetchone()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    items = db.execute(
        """SELECT i.account, i.password, i.recovery_email
           FROM order_items oi
           JOIN inventory i ON oi.inventory_id=i.id
           WHERE oi.order_id=?""",
        (order_id,),
    ).fetchall()

    return {
        "status": "ok",
        "order": dict(order),
        "cards": [dict(r) for r in items],
    }


# ═══════════════════════════════════════════════════════════════
# 管理后台
# ═══════════════════════════════════════════════════════════════

@router.get("/shop/admin/dashboard")
async def admin_dashboard(request: Request):
    """管理后台概览 — 今日统计 + 趋势 + 商品销量 + 余额概览"""
    require_admin(request)
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()

    # ── 今日概览 ──
    today_stats = db.execute(
        """SELECT COUNT(*) as orders, COALESCE(SUM(total_price), 0) as revenue,
                  COUNT(DISTINCT user_id) as consumers
           FROM orders WHERE date(created_at)=?""", (today,)
    ).fetchone()
    new_users_today = db.execute(
        "SELECT COUNT(*) as cnt FROM users WHERE date(created_at)=?", (today,)
    ).fetchone()["cnt"]
    today_recharge = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM card_usage WHERE date(used_at)=?", (today,)
    ).fetchone()["total"]

    # ── 商品销量（今日） ──
    product_sales = db.execute(
        """SELECT p.name, COUNT(o.id) as cnt, COALESCE(SUM(o.total_price), 0) as revenue
           FROM orders o JOIN products p ON o.product_id=p.id
           WHERE date(o.created_at)=? GROUP BY o.product_id ORDER BY cnt DESC""", (today,)
    ).fetchall()

    # ── 今日消费排行 ──
    top_consumers = db.execute(
        """SELECT u.username, COUNT(o.id) as orders, COALESCE(SUM(o.total_price), 0) as spent
           FROM orders o JOIN users u ON o.user_id=u.id
           WHERE date(o.created_at)=? GROUP BY o.user_id ORDER BY spent DESC LIMIT 20""", (today,)
    ).fetchall()

    # ── 余额概览 ──
    total_balance = db.execute("SELECT COALESCE(SUM(balance), 0) as t FROM users").fetchone()["t"]
    user_count = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    avg_balance = round(total_balance / user_count, 2) if user_count > 0 else 0
    # 余额分布
    tiers = {}
    for label, lo, hi in [("0", 0, 0), ("0.01-10", 0.01, 10), ("10-100", 10, 100),
                           ("100-500", 100, 500), ("500+", 500, 999999)]:
        if lo == 0 and hi == 0:
            cnt = db.execute("SELECT COUNT(*) as c FROM users WHERE balance=0").fetchone()["c"]
        else:
            cnt = db.execute("SELECT COUNT(*) as c FROM users WHERE balance>? AND balance<=?",
                             (lo, hi)).fetchone()["c"]
        tiers[label] = cnt

    # ── 近7天趋势 ──
    weekly_days, weekly_revenues, weekly_orders = [], [], []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        weekly_days.append(d[-5:])  # MM-DD
        row = db.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(total_price), 0) as r FROM orders WHERE date(created_at)=?",
            (d,)).fetchone()
        weekly_orders.append(row["c"])
        weekly_revenues.append(round(row["r"], 2))

    # ── 本月逐日 ──
    month_start = now.strftime("%Y-%m-01")
    monthly_days, monthly_revenues, monthly_orders = [], [], []
    d = datetime.strptime(month_start, "%Y-%m-%d")
    while d <= now:
        ds = d.strftime("%Y-%m-%d")
        monthly_days.append(ds[-5:])
        row = db.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(total_price), 0) as r FROM orders WHERE date(created_at)=?",
            (ds,)).fetchone()
        monthly_orders.append(row["c"])
        monthly_revenues.append(round(row["r"], 2))
        d += timedelta(days=1)

    # ── 逐月统计 ──
    yearly_months, yearly_revenues, yearly_orders = [], [], []
    for i in range(11, -1, -1):
        m = (now.month - i - 1) % 12 + 1
        y = now.year - (1 if i >= now.month else 0)
        label = f"{y}-{str(m).zfill(2)}"
        start = f"{y}-{str(m).zfill(2)}-01"
        if m == 12:
            end = f"{y + 1}-01-01"
        else:
            end = f"{y}-{str(m + 1).zfill(2)}-01"
        row = db.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(total_price), 0) as r FROM orders WHERE created_at>=? AND created_at<?",
            (start, end)).fetchone()
        yearly_months.append(label)
        yearly_orders.append(row["c"])
        yearly_revenues.append(round(row["r"], 2))

    # ── 库存告急 ──
    low_stock = db.execute(
        """SELECT p.name, COUNT(i.id) as cnt
           FROM products p LEFT JOIN inventory i ON p.id=i.product_id AND i.status='available'
           WHERE p.is_active=1 GROUP BY p.id HAVING cnt < 5"""
    ).fetchall()

    return {
        "status": "ok",
        "dashboard": {
            "today": {
                "orders": today_stats["orders"], "revenue": today_stats["revenue"],
                "consumers": today_stats["consumers"], "new_users": new_users_today,
                "recharge": today_recharge,
            },
            "product_sales": [{"name": r["name"], "count": r["cnt"], "revenue": r["revenue"]} for r in product_sales],
            "top_consumers": [{"username": r["username"], "orders": r["orders"], "spent": r["spent"]} for r in top_consumers],
            "balance_overview": {"total": total_balance, "avg": avg_balance, "users": user_count, "tiers": tiers},
            "weekly": {"days": weekly_days, "revenues": weekly_revenues, "orders": weekly_orders},
            "monthly_daily": {"days": monthly_days, "revenues": monthly_revenues, "orders": monthly_orders},
            "yearly_monthly": {"months": yearly_months, "revenues": yearly_revenues, "orders": yearly_orders},
            "low_stock": [dict(r) for r in low_stock],
        },
    }


@router.post("/shop/admin/cards/generate")
async def admin_generate_cards(req: GenerateCardsReq, request: Request):
    """批量生成充值卡密"""
    require_admin(request)
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="面额必须大于0")
    if req.count < 1 or req.count > 10000:
        raise HTTPException(status_code=400, detail="数量1-10000之间")

    db = get_db()
    cards = []
    for i in range(req.count):
        card_no = f"C{secrets.token_hex(8).upper()}"
        card_pw = secrets.token_hex(8).upper()
        db.execute(
            "INSERT INTO recharge_cards (card_no, card_password, amount, status, batch) VALUES (?, ?, ?, 'unused', ?)",
            (card_no, card_pw, req.amount, req.batch or ""),
        )
        cards.append({"card_no": card_no, "card_password": card_pw, "amount": req.amount})

    db.commit()

    return {
        "status": "ok",
        "message": f"已生成 {req.count} 张 {req.amount:.2f} 元充值卡",
        "cards": cards,
    }


@router.get("/shop/admin/cards")
async def admin_list_cards(
    request: Request,
    status: str = "",
    batch: str = "",
    page: int = 1,
    page_size: int = 50,
):
    """充值卡列表"""
    require_admin(request)
    db = get_db()

    where = []
    params = []
    if status:
        where.append("status=?")
        params.append(status)
    if batch:
        where.append("batch=?")
        params.append(batch)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(
        f"SELECT COUNT(*) as cnt FROM recharge_cards {where_clause}", params
    ).fetchone()["cnt"]

    offset = (page - 1) * page_size
    rows = db.execute(
        f"""SELECT rc.*, u.username as used_by_name
            FROM recharge_cards rc
            LEFT JOIN users u ON rc.used_by=u.id
            {where_clause}
            ORDER BY rc.created_at DESC
            LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()

    return {
        "status": "ok",
        "total": total,
        "page": page,
        "cards": [dict(r) for r in rows],
    }


@router.get("/shop/admin/cards/export")
async def admin_export_cards(request: Request, batch: str = ""):
    """导出未使用的充值卡（纯文本，方便发卡）"""
    require_admin(request)
    db = get_db()
    if batch:
        rows = db.execute(
            "SELECT card_no, card_password, amount FROM recharge_cards WHERE status='unused' AND batch=? ORDER BY id",
            (batch,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT card_no, card_password, amount FROM recharge_cards WHERE status='unused' ORDER BY id"
        ).fetchall()

    lines = [f"卡号: {r['card_no']}  密码: {r['card_password']}  面额: {r['amount']}元" for r in rows]
    return {"status": "ok", "count": len(rows), "data": "\n".join(lines)}


@router.get("/shop/admin/orders")
async def admin_list_orders(request: Request, page: int = 1, page_size: int = 50):
    """全部订单"""
    require_admin(request)
    db = get_db()
    total = db.execute("SELECT COUNT(*) as cnt FROM orders").fetchone()["cnt"]
    offset = (page - 1) * page_size
    rows = db.execute(
        """SELECT o.*, u.username
           FROM orders o JOIN users u ON o.user_id=u.id
           ORDER BY o.created_at DESC LIMIT ? OFFSET ?""",
        (page_size, offset),
    ).fetchall()
    return {"status": "ok", "total": total, "page": page, "orders": [dict(r) for r in rows]}


@router.post("/shop/admin/products")
async def admin_create_product(req: ProductReq, request: Request):
    """新增商品"""
    require_admin(request)
    if req.price < 0:
        raise HTTPException(status_code=400, detail="价格不能为负数")
    db = get_db()
    cursor = db.execute(
        "INSERT INTO products (category_id, name, description, description_html, price, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (req.category_id if req.category_id > 0 else None, req.name, req.description,
         _sanitize_html(req.description_html), req.price),
    )
    db.commit()
    return {"status": "ok", "product_id": cursor.lastrowid, "message": "商品已创建"}


@router.put("/shop/admin/products/{product_id}")
async def admin_update_product(product_id: int, req: ProductReq, request: Request):
    """编辑商品"""
    require_admin(request)
    if req.price < 0:
        raise HTTPException(status_code=400, detail="价格不能为负数")
    db = get_db()
    db.execute(
        "UPDATE products SET category_id=?, name=?, description=?, description_html=?, price=? WHERE id=?",
        (req.category_id if req.category_id > 0 else None, req.name, req.description,
         _sanitize_html(req.description_html), req.price, product_id),
    )
    db.execute(
        "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory WHERE product_id=? AND status='available') WHERE id=?",
        (product_id, product_id),
    )
    db.commit()
    return {"status": "ok", "message": "商品已更新"}


@router.get("/shop/admin/products")
async def admin_list_products(request: Request):
    """管理端商品列表 — 包含已下架商品"""
    require_admin(request)
    db = get_db()
    rows = db.execute(
        """SELECT p.*, c.name as category_name,
                  (SELECT COUNT(*) FROM inventory WHERE product_id=p.id AND status='available') as available
           FROM products p
           LEFT JOIN categories c ON p.category_id=c.id
           ORDER BY p.is_active DESC, p.sort_order, p.id"""
    ).fetchall()
    prods = []
    for r in rows:
        d = dict(r)
        d["is_active"] = bool(d.get("is_active", 1))
        prods.append(d)
    return {"status": "ok", "products": prods}


@router.put("/shop/admin/products/{product_id}/toggle")
async def admin_toggle_product(product_id: int, request: Request):
    """切换商品上架/下架状态"""
    admin = require_admin(request)
    db = get_db()
    prod = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not prod:
        raise HTTPException(status_code=404, detail="商品不存在")
    new_state = 0 if prod["is_active"] else 1
    label = "上架" if new_state else "下架"
    db.execute("UPDATE products SET is_active=? WHERE id=?", (new_state, product_id))
    _audit_log(db, admin["id"], "toggle_product", "product", product_id,
               f"商品 {prod['name']} {label}")
    db.commit()
    return {"status": "ok", "message": f"商品已{label}", "is_active": bool(new_state)}


@router.delete("/shop/admin/products/{product_id}")
async def admin_delete_product(product_id: int, request: Request):
    """永久删除商品及所有库存（含已售订单关联）"""
    admin = require_admin(request)
    db = get_db()
    prod = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not prod:
        raise HTTPException(status_code=404, detail="商品不存在")

    # 统计关联库存
    inv_count = db.execute(
        "SELECT COUNT(*) as cnt FROM inventory WHERE product_id=?", (product_id,)
    ).fetchone()["cnt"]

    # 先解除外键关联
    db.execute(
        """DELETE FROM order_items WHERE inventory_id IN
           (SELECT id FROM inventory WHERE product_id=?)""", (product_id,)
    )
    # 断开 inventory → orders 引用
    db.execute("UPDATE inventory SET order_id=NULL WHERE product_id=?", (product_id,))
    # 断开 orders → product 引用（保留订单记录）
    db.execute("UPDATE orders SET product_id=NULL WHERE product_id=?", (product_id,))
    # 删除库存
    db.execute("DELETE FROM inventory WHERE product_id=?", (product_id,))
    # 删除VIP定价
    db.execute("DELETE FROM product_vip_prices WHERE product_id=?", (product_id,))
    # 删除数量折扣
    db.execute("DELETE FROM quantity_discounts WHERE product_id=?", (product_id,))
    # 删除商品
    db.execute("DELETE FROM products WHERE id=?", (product_id,))
    _audit_log(db, admin["id"], "delete_product", "product", product_id,
               f"永久删除商品 {prod['name']} 及其 {inv_count} 条库存")
    db.commit()
    return {"status": "ok", "message": f"已永久删除商品「{prod['name']}」及 {inv_count} 条库存"}


@router.post("/shop/admin/inventory/import")
async def admin_import_inventory(req: ImportInventoryReq, request: Request):
    """批量导入商品库存"""
    require_admin(request)
    db = get_db()

    # 验证商品存在
    product = db.execute("SELECT * FROM products WHERE id=?", (req.product_id,)).fetchone()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    count = 0
    errors = []
    for line in req.data.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("----")
        if len(parts) < 2:
            errors.append(f"格式错误（缺少----分隔符）: {line[:30]}...")
            continue
        account = parts[0].strip()
        password = parts[1].strip()
        recovery = parts[2].strip() if len(parts) > 2 else ""

        db.execute(
            "INSERT INTO inventory (product_id, account, password, recovery_email, status) VALUES (?, ?, ?, ?, 'available')",
            (req.product_id, account, password, recovery),
        )
        count += 1

    # 更新库存计数
    db.execute(
        "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory WHERE product_id=? AND status='available') WHERE id=?",
        (req.product_id, req.product_id),
    )
    db.commit()

    return {
        "status": "ok",
        "message": f"导入完成：成功 {count} 条",
        "count": count,
        "errors": errors,
    }


@router.post("/shop/admin/inventory/batch")
async def admin_batch_inventory(req: BatchInventoryReq, request: Request):
    """批量操作库存：删除 / 改状态"""
    admin = require_admin(request)
    db = get_db()
    if not req.ids:
        raise HTTPException(status_code=400, detail="请选择至少一条记录")
    if len(req.ids) > 100:
        raise HTTPException(status_code=400, detail="单次最多操作100条")

    placeholders = ",".join("?" for _ in req.ids)

    if req.action == "delete":
        # 先收集产品ID用于库存更新
        pid_rows = db.execute(
            f"SELECT DISTINCT product_id FROM inventory WHERE id IN ({placeholders})", req.ids
        ).fetchall()
        # 解除 order_items 外键约束
        db.execute(
            f"DELETE FROM order_items WHERE inventory_id IN ({placeholders})", req.ids
        )
        # 删除库存
        db.execute(f"DELETE FROM inventory WHERE id IN ({placeholders})", req.ids)
        # 更新库存计数
        for pr in pid_rows:
            db.execute(
                "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory WHERE product_id=? AND status='available') WHERE id=?",
                (pr["product_id"], pr["product_id"]),
            )
        _audit_log(db, admin["id"], "batch_delete_inventory", "inventory", 0,
                   f"Deleted {len(req.ids)} items: ids={req.ids[:10]}{'...' if len(req.ids) > 10 else ''}")
        db.commit()
        return {"status": "ok", "message": f"已删除 {len(req.ids)} 条库存"}

    elif req.action == "set_status":
        if req.status not in ("available", "sold"):
            raise HTTPException(status_code=400, detail="无效的状态")
        pid_rows = db.execute(
            f"SELECT DISTINCT product_id FROM inventory WHERE id IN ({placeholders})", req.ids
        ).fetchall()
        db.execute(
            f"UPDATE inventory SET status=? WHERE id IN ({placeholders})",
            [req.status] + req.ids,
        )
        for pr in pid_rows:
            db.execute(
                "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory WHERE product_id=? AND status='available') WHERE id=?",
                (pr["product_id"], pr["product_id"]),
            )
        _audit_log(db, admin["id"], "batch_set_inventory_status", "inventory", 0,
                   f"Set {len(req.ids)} items to {req.status}: ids={req.ids[:10]}{'...' if len(req.ids) > 10 else ''}")
        db.commit()
        return {"status": "ok", "message": f"已将 {len(req.ids)} 条标记为 {req.status}"}

    raise HTTPException(status_code=400, detail="无效的操作")


@router.get("/shop/admin/inventory")
async def admin_list_inventory(
    request: Request,
    product_id: int = 0,
    status: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
):
    """库存列表"""
    require_admin(request)
    db = get_db()

    # 裸表 where（用于 COUNT inventory）
    bare_where = []
    if product_id > 0:
        bare_where.append("product_id=?")
    if status:
        bare_where.append("status=?")
    if search:
        bare_where.append("(account LIKE ? OR password LIKE ? OR recovery_email LIKE ?)")
    bare_clause = ("WHERE " + " AND ".join(bare_where)) if bare_where else ""

    params = []
    if product_id > 0: params.append(product_id)
    if status: params.append(status)
    if search: params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

    total = db.execute(f"SELECT COUNT(*) as cnt FROM inventory {bare_clause}", params).fetchone()["cnt"]
    avail_count = db.execute(f"SELECT COUNT(*) as cnt FROM inventory {bare_clause}{' AND' if bare_where else 'WHERE'} status='available'", params).fetchone()["cnt"]
    sold_count = db.execute(f"SELECT COUNT(*) as cnt FROM inventory {bare_clause}{' AND' if bare_where else 'WHERE'} status='sold'", params).fetchone()["cnt"]

    # JOIN 查询用 i. 前缀（避免 product_id 歧义）
    join_where = []
    jp = []
    if product_id > 0:
        join_where.append("i.product_id=?")
        jp.append(product_id)
    if status:
        join_where.append("i.status=?")
        jp.append(status)
    if search:
        join_where.append("(i.account LIKE ? OR i.password LIKE ? OR i.recovery_email LIKE ?)")
        s = f"%{search}%"
        jp.extend([s, s, s])
    join_clause = ("WHERE " + " AND ".join(join_where)) if join_where else ""

    offset = (page - 1) * page_size
    rows = db.execute(
        f"""SELECT i.*, o.order_no, u.username as buyer_name
           FROM inventory i
           LEFT JOIN orders o ON i.order_id = o.id
           LEFT JOIN users u ON o.user_id = u.id
           {join_clause}
           ORDER BY i.id LIMIT ? OFFSET ?""",
        jp + [page_size, offset],
    ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        items.append(d)
    return {"status": "ok", "total": total, "sold": sold_count, "available": avail_count, "page": page, "items": items}


# ═══════════════════════════════════════════════════════════════
# 优惠码
# ═══════════════════════════════════════════════════════════════

@router.post("/shop/promo/validate")
async def validate_promo(req: ValidatePromoReq):
    """验证优惠码"""
    db = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    promo = db.execute(
        """SELECT * FROM promo_codes WHERE code=? AND is_active=1
           AND (start_time='' OR start_time<=?) AND (end_time='' OR end_time>=?)""",
        (req.code.strip().upper(), now, now)
    ).fetchone()
    if not promo:
        raise HTTPException(status_code=400, detail="优惠码无效或已过期")
    if promo["max_uses"] > 0 and promo["used_count"] >= promo["max_uses"]:
        raise HTTPException(status_code=400, detail="优惠码已被用完")
    if promo["target_product_id"] > 0 and promo["target_product_id"] != req.product_id:
        raise HTTPException(status_code=400, detail="此优惠码不适用于该商品")
    if req.order_total < promo["min_order_amount"]:
        raise HTTPException(status_code=400,
            detail=f"订单金额需满 {promo['min_order_amount']:.2f} 元")
    if promo["discount_type"] == "percent":
        discount = round(req.order_total * promo["discount_value"] / 100.0, 2)
    else:
        discount = min(promo["discount_value"], req.order_total)
    return {"status": "ok", "code": promo["code"], "discount_type": promo["discount_type"],
            "discount_value": promo["discount_value"], "discount_amount": discount,
            "final_total": round(req.order_total - discount, 2)}


@router.get("/shop/admin/promo_codes")
async def admin_list_promo_codes(request: Request):
    require_admin(request)
    db = get_db()
    rows = db.execute("SELECT * FROM promo_codes ORDER BY created_at DESC").fetchall()
    return {"status": "ok", "promo_codes": [dict(r) for r in rows]}


@router.post("/shop/admin/promo_codes")
async def admin_create_promo_code(req: PromoCodeReq, request: Request):
    require_admin(request)
    db = get_db()
    try:
        db.execute(
            """INSERT INTO promo_codes (code, discount_type, discount_value, target_product_id,
               target_vip_level, min_order_amount, max_uses, start_time, end_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.code.strip().upper(), req.discount_type, req.discount_value,
             req.target_product_id, req.target_vip_level, req.min_order_amount,
             req.max_uses, req.start_time, req.end_time))
        db.commit()
        return {"status": "ok", "message": "优惠码已创建"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="优惠码已存在")


@router.put("/shop/admin/promo_codes/{code_id}")
async def admin_update_promo_code(code_id: int, req: PromoCodeReq, request: Request):
    require_admin(request)
    db = get_db()
    db.execute(
        """UPDATE promo_codes SET code=?, discount_type=?, discount_value=?, target_product_id=?,
           target_vip_level=?, min_order_amount=?, max_uses=?, start_time=?, end_time=?
           WHERE id=?""",
        (req.code.strip().upper(), req.discount_type, req.discount_value,
         req.target_product_id, req.target_vip_level, req.min_order_amount,
         req.max_uses, req.start_time, req.end_time, code_id))
    db.commit()
    return {"status": "ok", "message": "优惠码已更新"}


@router.delete("/shop/admin/promo_codes/{code_id}")
async def admin_delete_promo_code(code_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("UPDATE promo_codes SET is_active=0 WHERE id=?", (code_id,))
    db.commit()
    return {"status": "ok", "message": "优惠码已停用"}


# ═══════════════════════════════════════════════════════════════
# 数量折扣
# ═══════════════════════════════════════════════════════════════

@router.get("/shop/admin/quantity_discounts")
async def admin_list_qty_discounts(request: Request, product_id: int = 0):
    require_admin(request)
    db = get_db()
    if product_id > 0:
        rows = db.execute(
            """SELECT qd.*, p.name as product_name FROM quantity_discounts qd
               JOIN products p ON qd.product_id=p.id WHERE qd.product_id=? ORDER BY qd.min_quantity""",
            (product_id,)).fetchall()
    else:
        rows = db.execute(
            """SELECT qd.*, p.name as product_name FROM quantity_discounts qd
               JOIN products p ON qd.product_id=p.id ORDER BY p.id, qd.min_quantity""").fetchall()
    return {"status": "ok", "discounts": [dict(r) for r in rows]}


@router.post("/shop/admin/quantity_discounts")
async def admin_create_qty_discount(req: QtyDiscountReq, request: Request):
    require_admin(request)
    db = get_db()
    db.execute(
        """INSERT INTO quantity_discounts (product_id, min_quantity, discount_percent, discount_mode, start_time, end_time)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (req.product_id, req.min_quantity, req.discount_percent, req.discount_mode,
         req.start_time, req.end_time))
    db.commit()
    return {"status": "ok", "message": "数量折扣规则已创建"}


@router.put("/shop/admin/quantity_discounts/{discount_id}")
async def admin_update_qty_discount(discount_id: int, req: QtyDiscountReq, request: Request):
    require_admin(request)
    db = get_db()
    db.execute(
        """UPDATE quantity_discounts SET product_id=?, min_quantity=?, discount_percent=?,
           discount_mode=?, start_time=?, end_time=? WHERE id=?""",
        (req.product_id, req.min_quantity, req.discount_percent, req.discount_mode,
         req.start_time, req.end_time, discount_id))
    db.commit()
    return {"status": "ok", "message": "数量折扣已更新"}


@router.delete("/shop/admin/quantity_discounts/{discount_id}")
async def admin_delete_qty_discount(discount_id: int, request: Request):
    require_admin(request)
    db = get_db()
    db.execute("DELETE FROM quantity_discounts WHERE id=?", (discount_id,))
    db.commit()
    return {"status": "ok", "message": "已删除"}


@router.get("/shop/products/{product_id}/discount")
async def get_product_discounts(product_id: int, request: Request, quantity: int = 1):
    """查商品可用折扣（含单次+累计）"""
    db = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 单次
    single = db.execute(
        """SELECT * FROM quantity_discounts WHERE product_id=? AND is_active=1 AND discount_mode='single'
           AND min_quantity<=? AND (start_time='' OR start_time<=?) AND (end_time='' OR end_time>=?)
           ORDER BY min_quantity DESC LIMIT 1""",
        (product_id, quantity, now, now)).fetchone()
    # 累计 - 需要用户ID
    cumulative = None
    if request:
        user = get_user(request)
        if user:
            hist = db.execute(
                "SELECT COALESCE(SUM(quantity),0) as total FROM orders WHERE user_id=? AND product_id=?",
                (user["id"], product_id)).fetchone()["total"]
            cumulative = db.execute(
                """SELECT * FROM quantity_discounts WHERE product_id=? AND is_active=1 AND discount_mode='cumulative'
                   AND min_quantity<=? AND (start_time='' OR start_time<=?) AND (end_time='' OR end_time>=?)
                   ORDER BY min_quantity DESC LIMIT 1""",
                (product_id, hist + quantity, now, now)).fetchone()
    return {"status": "ok", "single": dict(single) if single else None,
            "cumulative": dict(cumulative) if cumulative else None}


# ═══════════════════════════════════════════════════════════════
# VIP 定价
# ═══════════════════════════════════════════════════════════════

@router.get("/shop/vip_price/{product_id}")
async def get_my_vip_price(product_id: int, request: Request):
    """查询当前用户对该商品的VIP价格（公开）"""
    user = get_user(request)
    if not user:
        return {"status": "ok", "vip_price": None, "is_vip": False}
    vl = user.get("vip_level", "normal")
    if vl == "normal":
        return {"status": "ok", "vip_price": None, "is_vip": False}
    db = get_db()
    row = db.execute(
        "SELECT price FROM product_vip_prices WHERE product_id=? AND vip_level=?",
        (product_id, vl)
    ).fetchone()
    if row and row["price"] > 0:
        return {"status": "ok", "vip_price": row["price"], "is_vip": True, "vip_level": vl}
    return {"status": "ok", "vip_price": None, "is_vip": True, "vip_level": vl}


@router.get("/shop/vip_prices")
async def get_all_my_vip_prices(request: Request):
    """查询当前用户所有VIP价格（首页批量用）"""
    user = get_user(request)
    if not user:
        return {"status": "ok", "prices": []}
    vl = user.get("vip_level", "normal")
    if vl == "normal":
        return {"status": "ok", "prices": []}
    db = get_db()
    rows = db.execute(
        "SELECT product_id, price FROM product_vip_prices WHERE vip_level=? AND price > 0",
        (vl,)
    ).fetchall()
    return {"status": "ok", "prices": [{"product_id": r["product_id"], "price": r["price"]} for r in rows]}


@router.get("/shop/admin/vip_prices")
async def admin_get_vip_prices(request: Request, product_id: int = 0):
    require_admin(request)
    db = get_db()
    if product_id > 0:
        rows = db.execute(
            "SELECT * FROM product_vip_prices WHERE product_id=?", (product_id,)).fetchall()
    else:
        rows = db.execute(
            """SELECT vp.*, p.name as product_name FROM product_vip_prices vp
               JOIN products p ON vp.product_id=p.id ORDER BY p.id, vp.vip_level""").fetchall()
    return {"status": "ok", "prices": [dict(r) for r in rows]}


@router.put("/shop/admin/vip_prices")
async def admin_set_vip_prices(req: VipPriceReq, request: Request):
    require_admin(request)
    db = get_db()
    for level, price in req.prices.items():
        db.execute(
            """INSERT OR REPLACE INTO product_vip_prices (product_id, vip_level, price)
               VALUES (?, ?, ?)""", (req.product_id, level, float(price)))
    db.commit()
    return {"status": "ok", "message": "VIP定价已保存"}


# ═══════════════════════════════════════════════════════════════
# 用户管理 (admin)
# ═══════════════════════════════════════════════════════════════

@router.get("/shop/admin/users")
async def admin_list_users(request: Request):
    require_admin(request)
    db = get_db()
    rows = db.execute(
        """SELECT u.*,
                  (SELECT COUNT(*) FROM users WHERE invited_by=u.id) as invite_count,
                  (SELECT COUNT(*) FROM orders WHERE user_id=u.id) as order_count,
                  (SELECT COALESCE(SUM(total_price),0) FROM orders WHERE user_id=u.id) as total_spent_calc
           FROM users u ORDER BY u.created_at DESC LIMIT 200""").fetchall()
    return {"status": "ok", "users": [dict(r) for r in rows]}


@router.get("/shop/admin/users/{user_id}")
async def admin_get_user(user_id: int, request: Request):
    require_admin(request)
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    u = dict(user)
    u["invite_count"] = db.execute(
        "SELECT COUNT(*) as cnt FROM users WHERE invited_by=?", (user_id,)).fetchone()["cnt"]
    return {"status": "ok", "user": u}


@router.put("/shop/admin/users/{user_id}")
async def admin_update_user(user_id: int, req: UserUpdateReq, request: Request):
    require_admin(request)
    db = get_db()
    if req.vip_level:
        db.execute("UPDATE users SET vip_level=? WHERE id=?", (req.vip_level, user_id))
    if req.password:
        ph, salt = _hash(req.password)
        db.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?", (ph, salt, user_id))
        db.execute("DELETE FROM user_tokens WHERE user_id=?", (user_id,))
    db.commit()
    return {"status": "ok", "message": "用户信息已更新"}


@router.post("/shop/admin/users/{user_id}/reset_password")
async def admin_reset_password(user_id: int, request: Request):
    """管理员重置用户密码为随机8位"""
    require_admin(request)
    db = get_db()
    new_pw = secrets.token_hex(8)  # 16位随机
    ph, salt = _hash(new_pw)
    db.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?", (ph, salt, user_id))
    db.execute("DELETE FROM user_tokens WHERE user_id=?", (user_id,))
    db.commit()
    return {"status": "ok", "message": f"密码已重置", "new_password": new_pw}


@router.post("/shop/admin/users/batch/balance")
async def admin_batch_user_balance(req: BatchUserBalanceReq, request: Request):
    """批量修改用户余额"""
    admin = require_admin(request)
    db = get_db()
    if not req.ids:
        raise HTTPException(status_code=400, detail="请选择至少一个用户")
    if len(req.ids) > 100:
        raise HTTPException(status_code=400, detail="单次最多操作100个用户")
    if req.action not in ("set", "add", "subtract"):
        raise HTTPException(status_code=400, detail="无效的操作类型")
    if req.amount < 0:
        raise HTTPException(status_code=400, detail="金额不能为负数")

    placeholders = ",".join("?" for _ in req.ids)
    action_label = {"set": "设为", "add": "增加", "subtract": "扣除"}[req.action]

    for uid in req.ids:
        if req.action == "set":
            db.execute("UPDATE users SET balance=?, balance_version=balance_version+1 WHERE id=?", (req.amount, uid))
        elif req.action == "add":
            db.execute("UPDATE users SET balance=balance+?, balance_version=balance_version+1 WHERE id=?", (req.amount, uid))
        elif req.action == "subtract":
            db.execute("UPDATE users SET balance=MAX(0, balance-?), balance_version=balance_version+1 WHERE id=?", (req.amount, uid))
        _audit_log(db, admin["id"], f"balance_{req.action}", "user", uid,
                   f"{action_label}余额 {req.amount} 元，原因: {req.reason or '无'}")

    db.commit()
    return {"status": "ok", "message": f"已为 {len(req.ids)} 个用户{action_label}余额 {req.amount} 元，请通知用户刷新页面"}


@router.put("/shop/admin/users/{user_id}/balance")
async def admin_set_user_balance(user_id: int, req: UserBalanceReq, request: Request):
    """单个用户修改余额"""
    admin = require_admin(request)
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_balance = max(0, req.amount)
    db.execute("UPDATE users SET balance=?, balance_version=balance_version+1 WHERE id=?", (new_balance, user_id))
    _audit_log(db, admin["id"], "balance_set", "user", user_id,
               f"设置余额为 {req.amount} 元 (原余额: {user['balance']})，原因: {req.reason or '无'}")
    db.commit()
    return {"status": "ok", "message": f"用户 {user['username']} 余额已更新为 {new_balance} 元，请通知用户刷新页面",
            "new_balance": new_balance}


@router.get("/shop/admin/audit_log")
async def admin_audit_log(request: Request, page: int = 1, page_size: int = 50):
    """查看审计日志"""
    require_admin(request)
    db = get_db()
    total = db.execute("SELECT COUNT(*) as cnt FROM admin_audit_log").fetchone()["cnt"]
    offset = (page - 1) * page_size
    rows = db.execute(
        """SELECT al.*, u.username as admin_name
           FROM admin_audit_log al LEFT JOIN users u ON al.admin_id=u.id
           ORDER BY al.created_at DESC LIMIT ? OFFSET ?""",
        (page_size, offset),
    ).fetchall()
    return {"status": "ok", "total": total, "page": page, "logs": [dict(r) for r in rows]}


# ═══════════════════════════════════════════════════════════════
# 用户端 — 充值/消费记录
# ═══════════════════════════════════════════════════════════════

@router.get("/shop/records")
async def my_records(request: Request, type: str = "all"):
    """我的充值/消费记录"""
    user = require_user(request)
    db = get_db()

    recharges = []
    consumptions = []

    if type in ("all", "recharge"):
        rows = db.execute(
            "SELECT amount, used_at FROM card_usage WHERE user_id=? ORDER BY used_at DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
        recharges = [dict(r) for r in rows]

    if type in ("all", "consume"):
        rows = db.execute(
            "SELECT product_name, total_price, created_at FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
        consumptions = [dict(r) for r in rows]

    return {"status": "ok", "recharges": recharges, "consumptions": consumptions}


# ═══════════════════════════════════════════════════════════════
# 健康检查 + 通知
# ═══════════════════════════════════════════════════════════════

_start_time = datetime.now()


@router.get("/shop/admin/health")
async def admin_health_check(request: Request):
    """健康检查：VPS状态、库存告警、近期异常"""
    require_admin(request)
    db = get_db()
    now = datetime.now()
    uptime = str(now - _start_time).split(".")[0]

    # 库存不足告警 (< 5)
    low_items = db.execute(
        """SELECT p.name, COUNT(i.id) as cnt
           FROM products p LEFT JOIN inventory i ON p.id=i.product_id AND i.status='available'
           WHERE p.is_active=1 GROUP BY p.id HAVING cnt < 5"""
    ).fetchall()
    low_stock = [{"name": r["name"], "count": r["cnt"]} for r in low_items]

    # 最近24小时新增订单
    yesterday = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    recent_orders = db.execute(
        "SELECT COUNT(*) as cnt, COALESCE(SUM(total_price), 0) as rev FROM orders WHERE created_at >= ?",
        (yesterday,)
    ).fetchone()

    # 未使用的充值卡余额
    unused_value = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as t FROM recharge_cards WHERE status='unused'"
    ).fetchone()["t"]

    return {
        "status": "ok",
        "uptime": uptime,
        "started_at": _start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "alerts": {
            "low_stock": low_stock,
            "low_stock_count": len(low_stock),
        },
        "last_24h": {
            "orders": recent_orders["cnt"],
            "revenue": recent_orders["rev"],
        },
        "unused_card_value": unused_value,
    }


@router.get("/shop/admin/daily_report")
async def daily_report(request: Request):
    """每日报告：各用户余额和消费汇总"""
    require_admin(request)
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")

    # 今日消费汇总
    today_summary = db.execute(
        """SELECT COUNT(*) as orders, COUNT(DISTINCT user_id) as users,
                  COALESCE(SUM(total_price), 0) as revenue
           FROM orders WHERE date(created_at)=?""", (today,)
    ).fetchone()

    # 用户余额概况
    balance_stats = db.execute(
        """SELECT COUNT(*) as total, COALESCE(SUM(balance), 0) as sum_balance,
                  COALESCE(AVG(balance), 0) as avg_balance,
                  COUNT(CASE WHEN balance > 0 THEN 1 END) as active_users,
                  COUNT(CASE WHEN balance = 0 THEN 1 END) as zero_users
           FROM users"""
    ).fetchone()

    # 今日充值
    today_recharge = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as t FROM card_usage WHERE date(used_at)=?", (today,)
    ).fetchone()["t"]

    # 今日消费用户明细
    users_today = db.execute(
        """SELECT u.username, u.balance, u.vip_level,
                  COALESCE(SUM(o.total_price), 0) as spent
           FROM orders o JOIN users u ON o.user_id=u.id
           WHERE date(o.created_at)=? GROUP BY o.user_id ORDER BY spent DESC""", (today,)
    ).fetchall()

    return {
        "status": "ok",
        "date": today,
        "today": {
            "orders": today_summary["orders"],
            "consumers": today_summary["users"],
            "revenue": today_summary["revenue"],
            "recharge": today_recharge,
        },
        "balance": {
            "total_users": balance_stats["total"],
            "total_balance": balance_stats["sum_balance"],
            "avg_balance": round(balance_stats["avg_balance"], 2),
            "active_users": balance_stats["active_users"],
            "zero_balance_users": balance_stats["zero_users"],
        },
        "consumers": [{"username": r["username"], "balance": r["balance"], "vip": r["vip_level"], "spent": r["spent"]} for r in users_today],
    }


# ═══════════════════════════════════════════════════════════════
# 站点配置（公开 + 管理）
# ═══════════════════════════════════════════════════════════════

@router.get("/shop/config")
async def get_site_config():
    """获取站点公开配置"""
    db = get_db()
    rows = db.execute("SELECT key, value FROM site_config").fetchall()
    cfg = {r["key"]: r["value"] for r in rows}
    return {"status": "ok", "config": cfg}


@router.get("/shop/admin/config")
async def admin_get_config(request: Request):
    """管理端获取全部配置"""
    require_admin(request)
    db = get_db()
    rows = db.execute("SELECT key, value FROM site_config").fetchall()
    cfg = {r["key"]: r["value"] for r in rows}
    return {"status": "ok", "config": cfg}


@router.put("/shop/admin/config")
async def admin_update_config(req: UpdateConfigReq, request: Request):
    """管理端更新配置"""
    require_admin(request)
    db = get_db()
    db.execute("INSERT OR REPLACE INTO site_config (key, value) VALUES (?, ?)", (req.key, req.value))
    db.commit()
    return {"status": "ok", "message": f"配置 {req.key} 已更新"}
