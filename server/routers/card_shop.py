"""
卡密自动售货系统 — API 路由
认证 / 充值 / 商品 / 下单 / 管理后台
"""
import hashlib
import os
import random
import secrets
import smtplib
import sqlite3
import string
import threading
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

router = APIRouter()

DB_PATH = Path(__file__).parent.parent.parent / "data" / "card_shop.db"

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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, created_at)"
    )

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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_ver ON email_verifications(email, created_at)"
    )

    # 迁移：给 users 表加 email 和 email_verified 字段（兼容旧库）
    for col, dflt in [("email", "''"), ("email_verified", "0")]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT {dflt}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    conn.commit()


# ═══════════════════════════════════════════════════════════════
# Auth helpers
# ═══════════════════════════════════════════════════════════════

def _hash(password: str, salt: str = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(8)
    h = hashlib.sha256((password + salt).encode()).hexdigest()
    return h, salt


def get_user(request: Request) -> Optional[dict]:
    """从请求头解析当前用户（作为 FastAPI 依赖使用）"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    db = get_db()
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


# ═══════════════════════════════════════════════════════════════
# Request/Response models
# ═══════════════════════════════════════════════════════════════

class RegisterReq(BaseModel):
    username: str
    password: str
    email: str = ""

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

class GenerateCardsReq(BaseModel):
    amount: float
    count: int
    batch: str = ""

class ProductReq(BaseModel):
    category_id: int = 0
    name: str
    description: str = ""
    price: float = 0

class ImportInventoryReq(BaseModel):
    product_id: int
    data: str  # 每行: 账号----密码----恢复邮箱

class UpdateConfigReq(BaseModel):
    key: str
    value: str


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
        msg["From"] = f"{SITE_NAME} <{SMTP_USER}>"
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

    # IP 限制：每小时每 IP 最多发 10 次
    ip = request.client.host if request.client else "unknown"
    hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    ip_count = db.execute(
        "SELECT COUNT(*) as cnt FROM email_verifications WHERE created_at > ?", (hour_ago,)
    ).fetchone()  # 用 created_at 里的 ip 不方便，加个简单全局限制
    # 简单全局限流
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

@router.post("/shop/auth/register")
async def auth_register(req: RegisterReq, request: Request):
    """用户注册 — 需先验证邮箱，注册时传入已验证的邮箱"""
    db = get_db()
    username = req.username.strip()
    email = req.email.strip().lower()

    if len(username) < 2 or len(username) > 20:
        raise HTTPException(status_code=400, detail="用户名2-20个字符")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="密码至少8位")
    # 密码复杂度：大写、小写、数字、符号，至少3种
    types = 0
    if any(c.islower() for c in req.password): types += 1
    if any(c.isupper() for c in req.password): types += 1
    if any(c.isdigit() for c in req.password): types += 1
    if any(not c.isalnum() for c in req.password): types += 1
    if types < 3:
        raise HTTPException(status_code=400, detail="密码需包含大写字母、小写字母、数字、符号中至少3种")

    # 检查邮箱是否已验证
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="请先验证邮箱")

    verified = db.execute(
        "SELECT id FROM email_verifications WHERE email=? AND used=1 ORDER BY id DESC LIMIT 1",
        (email,),
    ).fetchone()
    if not verified:
        raise HTTPException(status_code=400, detail="该邮箱未通过验证，请先获取验证码")

    # 检查邮箱是否已被其他用户绑定
    existing_email = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing_email:
        raise HTTPException(status_code=400, detail="该邮箱已被注册")

    # IP 注册限制
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
        db.execute(
            "INSERT INTO users (username, password_hash, salt, email, email_verified, balance) VALUES (?, ?, ?, ?, 1, 0)",
            (username, ph, salt, email),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="用户名已存在")

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
    ph, _ = _hash(req.password, user["salt"])
    if ph != user["password_hash"]:
        _record_login_attempt(db, ip, username, False)
        db.commit()
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 3. 登录成功
    _record_login_attempt(db, ip, username, True)

    # 生成 token
    token = secrets.token_hex(32)
    expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO user_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
        (user["id"], token, expires),
    )
    # 清理过期 token
    db.execute("DELETE FROM user_tokens WHERE expires_at < datetime('now', 'localtime')")
    db.commit()

    return {
        "status": "ok",
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user.get("email", ""),
            "balance": user["balance"],
            "is_admin": bool(user["is_admin"]),
        },
    }


@router.get("/shop/auth/me")
async def auth_me(request: Request):
    """获取当前用户信息"""
    user = require_user(request)
    return {
        "status": "ok",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user.get("email", ""),
            "balance": user["balance"],
            "is_admin": bool(user["is_admin"]),
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

    ph_check, _ = _hash(req.old_password, user["salt"])
    if ph_check != user["password_hash"]:
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
    """下单购买（自动扣余额 + 自动发货）"""
    user = require_user(request)
    db = get_db()

    # 查商品
    product = db.execute(
        "SELECT * FROM products WHERE id=? AND is_active=1", (req.product_id,)
    ).fetchone()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在或已下架")
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="购买数量至少为1")

    # 查库存
    available = db.execute(
        "SELECT COUNT(*) as cnt FROM inventory WHERE product_id=? AND status='available'",
        (req.product_id,),
    ).fetchone()["cnt"]
    if available < req.quantity:
        raise HTTPException(status_code=400, detail=f"库存不足，仅剩 {available} 件")

    total = product["price"] * req.quantity

    # 重新查用户余额
    user_row = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    if user_row["balance"] < total:
        raise HTTPException(
            status_code=400,
            detail=f"余额不足，需要 {total:.2f} 元，当前余额 {user_row['balance']:.2f} 元",
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 扣余额
    db.execute("UPDATE users SET balance=balance-? WHERE id=?", (total, user["id"]))

    # 锁定库存
    items = db.execute(
        "SELECT id FROM inventory WHERE product_id=? AND status='available' ORDER BY id LIMIT ?",
        (req.product_id, req.quantity),
    ).fetchall()

    # 创建订单
    cursor = db.execute(
        """INSERT INTO orders (user_id, product_id, product_name, quantity, unit_price, total_price, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'paid', ?)""",
        (user["id"], product["id"], product["name"], req.quantity,
         product["price"], total, now),
    )
    order_id = cursor.lastrowid

    # 发货：更新库存 + 创建明细
    delivered = []
    for item in items:
        db.execute(
            "UPDATE inventory SET status='sold', order_id=?, sold_at=? WHERE id=?",
            (order_id, now, item["id"]),
        )
        db.execute(
            "INSERT INTO order_items (order_id, inventory_id) VALUES (?, ?)",
            (order_id, item["id"]),
        )
        inv = db.execute(
            "SELECT account, password, recovery_email FROM inventory WHERE id=?",
            (item["id"],),
        ).fetchone()
        delivered.append(dict(inv))

    # 更新商品库存计数
    db.execute(
        "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory WHERE product_id=? AND status='available') WHERE id=?",
        (product["id"], product["id"]),
    )
    db.commit()

    new_balance = db.execute(
        "SELECT balance FROM users WHERE id=?", (user["id"],)
    ).fetchone()["balance"]

    return {
        "status": "ok",
        "message": f"购买成功！共 {req.quantity} 件，消费 {total:.2f} 元",
        "order": {
            "id": order_id,
            "product_name": product["name"],
            "quantity": req.quantity,
            "total_price": total,
            "created_at": now,
        },
        "cards": delivered,  # 返回卡密！
        "balance": new_balance,
    }


@router.get("/shop/orders")
async def list_my_orders(request: Request):
    """我的订单"""
    user = require_user(request)
    db = get_db()
    rows = db.execute(
        """SELECT o.*,
                  (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) as item_count
           FROM orders o WHERE o.user_id=?
           ORDER BY o.created_at DESC LIMIT 100""",
        (user["id"],),
    ).fetchall()
    return {"status": "ok", "orders": [dict(r) for r in rows]}


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
    """管理后台概览"""
    require_admin(request)
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")

    # 今日订单
    today_orders = db.execute(
        "SELECT COUNT(*) as cnt, COALESCE(SUM(total_price), 0) as total FROM orders WHERE date(created_at)=?",
        (today,),
    ).fetchone()

    # 总用户数
    total_users = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]

    # 待审核充值（此方案不需要审核，但保留统计）
    total_cards = db.execute("SELECT COUNT(*) as cnt FROM recharge_cards").fetchone()["cnt"]
    unused_cards = db.execute(
        "SELECT COUNT(*) as cnt FROM recharge_cards WHERE status='unused'"
    ).fetchone()["cnt"]

    # 今日充值
    today_recharge = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM card_usage WHERE date(used_at)=?",
        (today,),
    ).fetchone()["total"]

    # 库存告急（< 5 件）
    low_stock = db.execute(
        """SELECT p.name, COUNT(i.id) as cnt
           FROM products p
           LEFT JOIN inventory i ON p.id=i.product_id AND i.status='available'
           WHERE p.is_active=1
           GROUP BY p.id HAVING cnt < 5"""
    ).fetchall()

    return {
        "status": "ok",
        "dashboard": {
            "today_orders": today_orders["cnt"],
            "today_revenue": today_orders["total"],
            "today_recharge": today_recharge,
            "total_users": total_users,
            "total_cards": total_cards,
            "unused_cards": unused_cards,
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
        card_no = f"C{secrets.token_hex(4).upper()}"
        card_pw = secrets.token_hex(4).upper()
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
    db = get_db()
    cursor = db.execute(
        "INSERT INTO products (category_id, name, description, price, is_active) VALUES (?, ?, ?, ?, 1)",
        (req.category_id if req.category_id > 0 else None, req.name, req.description, req.price),
    )
    db.commit()
    return {"status": "ok", "product_id": cursor.lastrowid, "message": "商品已创建"}


@router.put("/shop/admin/products/{product_id}")
async def admin_update_product(product_id: int, req: ProductReq, request: Request):
    """编辑商品"""
    require_admin(request)
    db = get_db()
    db.execute(
        "UPDATE products SET category_id=?, name=?, description=?, price=? WHERE id=?",
        (req.category_id if req.category_id > 0 else None, req.name, req.description, req.price, product_id),
    )
    db.execute(
        "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory WHERE product_id=? AND status='available') WHERE id=?",
        (product_id, product_id),
    )
    db.commit()
    return {"status": "ok", "message": "商品已更新"}


@router.delete("/shop/admin/products/{product_id}")
async def admin_delete_product(product_id: int, request: Request):
    """下架商品"""
    require_admin(request)
    db = get_db()
    db.execute("UPDATE products SET is_active=0 WHERE id=?", (product_id,))
    db.commit()
    return {"status": "ok", "message": "商品已下架"}


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


@router.get("/shop/admin/inventory")
async def admin_list_inventory(
    request: Request,
    product_id: int = 0,
    status: str = "",
    page: int = 1,
    page_size: int = 50,
):
    """库存列表"""
    require_admin(request)
    db = get_db()

    where = []
    params = []
    if product_id > 0:
        where.append("product_id=?")
        params.append(product_id)
    if status:
        where.append("status=?")
        params.append(status)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) as cnt FROM inventory {where_clause}", params).fetchone()["cnt"]

    offset = (page - 1) * page_size
    rows = db.execute(
        f"SELECT * FROM inventory {where_clause} ORDER BY id LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()

    return {"status": "ok", "total": total, "page": page, "items": [dict(r) for r in rows]}


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
