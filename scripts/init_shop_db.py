"""
初始化卡密商城数据库
- 创建所有表
- 从旧 MDB 二进制提取卡密数据导入
- 创建默认管理员账号
"""
import hashlib
import os
import random
import re
import sqlite3
import string
import sys
from datetime import datetime
from pathlib import Path

# 修复 Windows GBK 终端编码
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "card_shop.db"
MDB_PATH = Path(r"D:\大神机器人PC版 V4.2.6\大神机器人\data.mdb")
TRANSFER_MDB = Path(r"D:\大神机器人PC版 V4.2.6\大神机器人\Transfer.mdb")

# 默认管理员
DEFAULT_ADMIN = "admin"
DEFAULT_ADMIN_PASS = "admin888"


def hash_password(password: str) -> tuple[str, str]:
    """SHA256 + 随机盐"""
    salt = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    h = hashlib.sha256((password + salt).encode()).hexdigest()
    return h, salt


def create_tables(conn: sqlite3.Connection):
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
    conn.commit()
    print("✓ 数据库表创建完成")


def extract_cards_from_mdb(mdb_path: Path) -> list[dict]:
    """从 MDB 二进制文件中提取卡密数据。

    格式: email前缀@domain----密码----恢复邮箱
    域名作为分类，每个唯一邮箱作为一个商品单元。
    """
    if not mdb_path.exists():
        print(f"✗ MDB 文件不存在: {mdb_path}")
        return []

    with open(mdb_path, "rb") as f:
        data = f.read()

    # 正则匹配卡密格式
    pattern = rb'([a-z0-9]+@(?:zhouedu|qsedu|cardinalm|forexvista|silverfox)\.(?:us|asia))----([a-zA-Z0-9!@#$%^&*_]+)----([a-zA-Z0-9@.]+)'
    matches = re.findall(pattern, data)

    results = []
    seen = set()
    for match in matches:
        account = match[0].decode("ascii", errors="replace")
        password = match[1].decode("ascii", errors="replace")
        recovery = match[2].decode("ascii", errors="replace")

        # 去重
        key = (account, password, recovery)
        if key in seen:
            continue
        seen.add(key)

        # 提取域名
        domain = account.split("@")[1] if "@" in account else "unknown"

        results.append({
            "domain": domain,
            "account": account,
            "password": password,
            "recovery_email": recovery,
        })

    print(f"  从 {mdb_path.name} 提取到 {len(results)} 条卡密记录")
    return results


def import_cards(conn: sqlite3.Connection, cards: list[dict]):
    """将卡密按域名归类为商品，写入数据库。"""
    # 按域名分组
    by_domain: dict[str, list] = {}
    for c in cards:
        domain = c["domain"]
        if domain not in by_domain:
            by_domain[domain] = []
        by_domain[domain].append(c)

    # Domain → 分类名映射
    domain_names = {
        "zhouedu.us": "Zhou教育邮箱",
        "qsedu.us": "QS教育邮箱",
        "cardinalm.us": "Cardinal账号",
        "forexvista.asia": "ForexVista账号",
        "silverfox.asia": "SilverFox账号",
    }

    cursor = conn.cursor()

    for domain, items in sorted(by_domain.items()):
        name = domain_names.get(domain, f"{domain} 账号")
        count = len(items)

        # 创建分类（如果不存在）
        cursor.execute("SELECT id FROM categories WHERE name=?", (name,))
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO categories (name, sort_order) VALUES (?, ?)",
                (name, len(by_domain)),
            )
            category_id = cursor.lastrowid
        else:
            category_id = row[0]

        # 创建商品
        cursor.execute(
            "INSERT INTO products (category_id, name, price, stock, is_active, sort_order) VALUES (?, ?, ?, ?, 1, ?)",
            (category_id, name, 0, count, len(by_domain)),
        )
        product_id = cursor.lastrowid

        # 写入库存
        for item in items:
            cursor.execute(
                "INSERT INTO inventory (product_id, account, password, recovery_email, status) VALUES (?, ?, ?, ?, 'available')",
                (product_id, item["account"], item["password"], item["recovery_email"]),
            )

        print(f"  {name}: {count} 条 (product_id={product_id})")

    conn.commit()
    print(f"✓ 共导入 {len(cards)} 条卡密，{len(by_domain)} 个商品")


def create_admin(conn: sqlite3.Connection):
    """创建默认管理员账号"""
    h, salt = hash_password(DEFAULT_ADMIN_PASS)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, is_admin, balance) VALUES (?, ?, ?, 1, 0)",
            (DEFAULT_ADMIN, h, salt),
        )
        conn.commit()
        print(f"✓ 管理员账号: {DEFAULT_ADMIN} / {DEFAULT_ADMIN_PASS}")
    except sqlite3.IntegrityError:
        print(f"  管理员账号已存在，跳过")


def main():
    print("=" * 50)
    print("卡密商城数据库初始化")
    print("=" * 50)

    # 确保 data 目录存在
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

    # 如果数据库已存在，删除重建
    if DB_PATH.exists():
        bak = DB_PATH.with_suffix(".db.bak")
        if bak.exists():
            bak.unlink()
        DB_PATH.rename(bak)
        print(f"  旧数据库已备份为 {bak.name}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        # 1. 建表
        create_tables(conn)

        # 2. 从 MDB 提取数据
        print("\n提取旧数据...")
        all_cards = extract_cards_from_mdb(MDB_PATH)
        all_cards += extract_cards_from_mdb(TRANSFER_MDB)

        if all_cards:
            import_cards(conn, all_cards)
        else:
            print("  ⚠ 未提取到卡密数据，将创建空库（后续可通过管理后台导入）")
            # 创建默认分类
            conn.execute(
                "INSERT INTO categories (name, sort_order) VALUES ('默认分类', 0)"
            )
            conn.commit()

        # 3. 创建管理员
        print("\n创建管理员...")
        create_admin(conn)

        print(f"\n✓ 初始化完成！数据库: {DB_PATH}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
