"""
卡密商城 — VPS 部署数据库初始化
创建所有表 + 默认管理员账号
"""
import hashlib
import secrets
import sqlite3
import string
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "card_shop.db"
DEFAULT_ADMIN = "admin"
DEFAULT_ADMIN_PASS = "admin888"


def hash_password(password: str) -> tuple[str, str]:
    salt = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    h = hashlib.sha256((password + salt).encode()).hexdigest()
    return h, salt


def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 备份旧库
    if DB_PATH.exists():
        bak = DB_PATH.with_suffix(".db.bak")
        if bak.exists():
            bak.unlink()
        DB_PATH.rename(bak)
        print(f"  旧数据库已备份为 {bak.name}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            email_verified INTEGER DEFAULT 0,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            balance REAL DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS email_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER REFERENCES categories(id),
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL DEFAULT 0,
            stock INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER REFERENCES products(id),
            account TEXT NOT NULL,
            password TEXT NOT NULL,
            recovery_email TEXT DEFAULT '',
            status TEXT DEFAULT 'available',
            order_id INTEGER REFERENCES orders(id),
            sold_at TEXT
        );

        CREATE TABLE IF NOT EXISTS recharge_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_no TEXT UNIQUE NOT NULL,
            card_password TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'unused',
            used_by INTEGER REFERENCES users(id),
            used_at TEXT,
            batch TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS card_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER REFERENCES recharge_cards(id),
            user_id INTEGER REFERENCES users(id),
            amount REAL NOT NULL,
            used_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            product_id INTEGER REFERENCES products(id),
            product_name TEXT DEFAULT '',
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            total_price REAL NOT NULL,
            status TEXT DEFAULT 'paid',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER REFERENCES orders(id),
            inventory_id INTEGER REFERENCES inventory(id)
        );

        CREATE TABLE IF NOT EXISTS user_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            token TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            username TEXT NOT NULL,
            success INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory(product_id, status);
        CREATE INDEX IF NOT EXISTS idx_recharge_cards_no ON recharge_cards(card_no);
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
        CREATE INDEX IF NOT EXISTS idx_tokens_token ON user_tokens(token);
        CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, created_at);
        CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, created_at);
    """)

    # 默认分类
    conn.execute("INSERT OR IGNORE INTO categories (name, sort_order) VALUES ('默认分类', 0)")

    # 管理员
    h, salt = hash_password(DEFAULT_ADMIN_PASS)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, is_admin, balance) VALUES (?, ?, ?, 1, 0)",
            (DEFAULT_ADMIN, h, salt),
        )
        print(f"  管理员: {DEFAULT_ADMIN} / {DEFAULT_ADMIN_PASS}")
    except sqlite3.IntegrityError:
        print("  管理员已存在，跳过")

    conn.commit()
    conn.close()
    print(f"✓ 数据库初始化完成: {DB_PATH}")


if __name__ == "__main__":
    init()
