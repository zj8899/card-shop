"""T+1 模拟实盘引擎 — 策略独立账户 + 下单 + 竞价确认 + 盈亏 + 交易纪律。

数据模型: data/live_accounts.db (SQLite, WAL mode, thread-local 连接)
每个策略一个独立账户(各自资金池 + 仓位管理), 盈亏逻辑严格 T+1。
"""
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "live_accounts.db"
_local = threading.local()

# 仓位风格映射
POSITION_STYLES = {
    "strict":           {"max_single": 0.15, "max_positions": 5, "label": "保守"},
    "simple":           {"max_single": 0.15, "max_positions": 5, "label": "保守"},
    "strict_reverse":   {"max_single": 0.30, "max_positions": 3, "label": "激进"},
    "schools":          {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
    "chan_theory":      {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
    "ict":              {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
    "price_action":     {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
    "wyckoff":          {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
    "morphology":       {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
    "gann":             {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
    "wave_theory":      {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
    "dow_theory":       {"max_single": 0.20, "max_positions": 5, "label": "稳健"},
}


def get_db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY, name TEXT, type TEXT DEFAULT 'strategy',
        initial_capital REAL DEFAULT 10000, cash REAL, frozen REAL DEFAULT 0,
        position_value REAL DEFAULT 0, total_equity REAL, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS account_positions (
        account_id TEXT, symbol TEXT, shares INTEGER, avg_cost REAL,
        buy_date TEXT, current_price REAL, unrealized_pnl REAL, realized_pnl REAL DEFAULT 0,
        PRIMARY KEY (account_id, symbol)
    );
    CREATE TABLE IF NOT EXISTS account_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, date TEXT,
        symbol TEXT, side TEXT, price REAL, shares INTEGER, cost REAL,
        reason TEXT, pnl REAL, strategy_signal TEXT, plan_id TEXT
    );
    CREATE TABLE IF NOT EXISTS equity_log (
        account_id TEXT, date TEXT, cash REAL, position_value REAL,
        total_equity REAL, daily_pnl_pct REAL,
        PRIMARY KEY (account_id, date)
    );
    CREATE TABLE IF NOT EXISTS auction_confirm (
        account_id TEXT, date TEXT, symbol TEXT, auction_price REAL,
        gap_pct REAL, vol_ratio REAL, verdict TEXT, note TEXT,
        PRIMARY KEY (account_id, date, symbol)
    );
    CREATE TABLE IF NOT EXISTS strategy_discipline (
        account_id TEXT, updated_at TEXT, total_trades INTEGER, win_rate REAL,
        avg_return REAL, profit_factor REAL, sharpe_ratio REAL, best_condition TEXT,
        worst_condition TEXT, advice TEXT
    );
    """)
    db.commit()


def ensure_accounts(strategy_ids: list[tuple]) -> None:
    """确保每个策略都有独立账户(不存在则创建, 初始资金 10000)."""
    db = get_db()
    now = datetime.now().isoformat()
    for sid, name in strategy_ids:
        row = db.execute("SELECT 1 FROM accounts WHERE id=?", (sid,)).fetchone()
        if not row:
            capital = 10000.0
            db.execute("INSERT INTO accounts(id,name,cash,total_equity,created_at) VALUES(?,?,?,?,?)",
                       (sid, name, capital, capital, now))
            logger.info("Created account: %s (%s) capital=%s", sid, name, capital)
    db.commit()


def place_order(account_id: str, symbol: str, name: str, price: float,
                strategy_signal: str, plan_id: str, reason: str = "",
                max_single_pct: float = 0.20) -> Optional[dict]:
    """14:52 下单: 从对应账户扣款, 按仓位上限计算买入股数。

    Returns order dict or None (仓位已满/资金不足).
    """
    db = get_db()
    acc = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not acc:
        return None

    cash = acc["cash"]
    capital = acc["total_equity"] or acc["initial_capital"]

    # 仓位上限
    max_amount = capital * max_single_pct
    if cash < max_amount * 0.5:
        return None  # 资金不足, 不下单

    amount = min(cash * 0.8, max_amount)  # 最多用80%现金, 不乱满仓
    shares = int(amount / (price * 100)) * 100  # A股整手
    if shares < 100:
        return None

    cost = round(shares * price * (1 + 0.00025), 2)  # 含佣金
    if cash < cost:
        shares = int(shares * 0.8 / 100) * 100
        if shares < 100:
            return None
        cost = round(shares * price * (1 + 0.00025), 2)

    today = datetime.now().strftime("%Y-%m-%d")

    # 写交易记录
    db.execute(
        "INSERT INTO account_trades(account_id,date,symbol,side,price,shares,cost,reason,strategy_signal,plan_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (account_id, today, symbol, "buy", price, shares, cost, reason, strategy_signal, plan_id))
    # 更新持仓
    db.execute(
        "INSERT OR REPLACE INTO account_positions(account_id,symbol,shares,avg_cost,buy_date,current_price,unrealized_pnl) VALUES(?,?,?,?,?,?,0)",
        (account_id, symbol, shares, price, today, price))
    # 更新账户现金
    db.execute("UPDATE accounts SET cash=cash-?, frozen=0 WHERE id=?", (cost, account_id))
    db.commit()

    return {"account_id": account_id, "symbol": symbol, "name": name,
            "shares": shares, "price": price, "cost": round(cost, 2)}


def update_positions_market(price_map: dict[str, float]):
    """收盘后更新所有持仓的市值和未实现盈亏."""
    db = get_db()
    rows = db.execute("SELECT account_id, symbol, shares, avg_cost FROM account_positions").fetchall()
    for r in rows:
        p = price_map.get(r["symbol"], 0)
        if p <= 0:
            continue
        pnl = round((p - r["avg_cost"]) * r["shares"], 2)
        db.execute("UPDATE account_positions SET current_price=?, unrealized_pnl=? WHERE account_id=? AND symbol=?",
                   (p, pnl, r["account_id"], r["symbol"]))
    db.commit()


def record_daily_equity(price_map: dict[str, float]):
    """记录每日每个账户的权益日志."""
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    for acc in db.execute("SELECT * FROM accounts").fetchall():
        pos_rows = db.execute("SELECT * FROM account_positions WHERE account_id=?", (acc["id"],)).fetchall()
        pos_val = sum((price_map.get(r["symbol"], r["current_price"] or 0) * r["shares"])
                      for r in pos_rows)
        total = acc["cash"] + pos_val
        prev = db.execute("SELECT total_equity FROM equity_log WHERE account_id=? ORDER BY date DESC LIMIT 1",
                          (acc["id"],)).fetchone()
        daily_pnl = round((total - prev["total_equity"]) / prev["total_equity"] * 100, 2) if prev and prev["total_equity"] > 0 else 0
        db.execute(
            "INSERT OR REPLACE INTO equity_log(account_id,date,cash,position_value,total_equity,daily_pnl_pct) VALUES(?,?,?,?,?,?)",
            (acc["id"], today, round(acc["cash"], 2), round(pos_val, 2), round(total, 2), daily_pnl))
        # 更新账户快照
        db.execute("UPDATE accounts SET cash=?, position_value=?, total_equity=? WHERE id=?",
                   (round(acc["cash"], 2), round(pos_val, 2), round(total, 2), acc["id"]))
    db.commit()


def confirm_auction(account_id: str, symbol: str, auction_price: float,
                    gap_pct: float, vol_ratio: float, strategy_type: str) -> dict:
    """次日 9:25 竞价确认: 验证昨天的买入是否正确。"""
    if strategy_type in ("strict_reverse",):  # 追涨突破
        if gap_pct > 1 and vol_ratio > 1.2:
            verdict, note = "confirmed", f"高开{gap_pct:+.1f}% 放量{vol_ratio:.1f}x — 竞价确认追涨"
        elif gap_pct > 0:
            verdict, note = "neutral", f"小幅高开{gap_pct:+.1f}% — 竞价中性"
        else:
            verdict, note = "denied", f"低开{gap_pct:+.1f}% — 竞价否认"
    elif strategy_type in ("strict", "simple"):  # 超跌抄底
        if -3 < gap_pct < 0:
            verdict, note = "confirmed", f"小幅低开{gap_pct:+.1f}% — 可抄底"
        elif gap_pct > 1:
            verdict, note = "denied", f"高开{gap_pct:+.1f}% — 不抄底"
        else:
            verdict, note = "neutral", f"竞价中性{gap_pct:+.1f}%"
    else:
        if gap_pct > 2 and vol_ratio > 1.5:
            verdict, note = "confirmed", f"强势竞价{gap_pct:+.1f}% {vol_ratio:.1f}x"
        elif gap_pct < -2:
            verdict, note = "denied", f"大幅低开{gap_pct:+.1f}%"
        else:
            verdict, note = "neutral", f"正常竞价{gap_pct:+.1f}%"

    today = datetime.now().strftime("%Y-%m-%d")
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO auction_confirm(account_id,date,symbol,auction_price,gap_pct,vol_ratio,verdict,note) VALUES(?,?,?,?,?,?,?,?)",
        (account_id, today, symbol, auction_price, round(gap_pct, 2), round(vol_ratio, 2), verdict, note))
    db.commit()
    return {"verdict": verdict, "note": note}


def get_strategy_ranking(days: int = 30) -> list[dict]:
    """30 日策略业绩排行."""
    db = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    ranking = []
    for acc in db.execute("SELECT * FROM accounts WHERE type='strategy'").fetchall():
        trades = db.execute(
            "SELECT * FROM account_trades WHERE account_id=? AND date>=? AND side='sell'",
            (acc["id"], cutoff)).fetchall()
        wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
        total = len(trades)
        pnls = [t["pnl"] or 0 for t in trades]
        win_pnls = [p for p in pnls if p > 0]
        loss_pnls = [p for p in pnls if p <= 0]
        ranking.append({
            "account_id": acc["id"], "name": acc["name"],
            "initial_capital": round(acc["initial_capital"], 2),
            "total_equity": round(acc["total_equity"] or acc["initial_capital"], 2),
            "return_pct": round((acc["total_equity"] - acc["initial_capital"]) / acc["initial_capital"] * 100, 2) if acc["initial_capital"] > 0 else 0,
            "win_rate": round(wins / max(total, 1) * 100, 1),
            "total_trades": total,
            "profit_factor": round(abs(sum(win_pnls) / sum(loss_pnls)), 2) if sum(loss_pnls) != 0 else 0,
            "avg_return": round(sum(pnls) / max(total, 1), 2),
        })
    ranking.sort(key=lambda x: x["return_pct"], reverse=True)
    return ranking


def get_account_summary(account_id: str) -> dict:
    """单一账户全量摘要."""
    db = get_db()
    acc = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not acc:
        return {}
    trades = db.execute(
        "SELECT * FROM account_trades WHERE account_id=? ORDER BY date DESC LIMIT 50",
        (account_id,)).fetchall()
    positions = db.execute(
        "SELECT * FROM account_positions WHERE account_id=?", (account_id,)).fetchall()
    equity = db.execute(
        "SELECT * FROM equity_log WHERE account_id=? ORDER BY date DESC LIMIT 30",
        (account_id,)).fetchall()
    return {
        "account": dict(acc),
        "trades": [dict(t) for t in trades],
        "positions": [dict(p) for p in positions],
        "equity": [dict(e) for e in equity],
    }


def close_db():
    conn = getattr(_local, "conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


# 启动时初始化
init_db()
