"""新闻事件持久化 — 融合 zhaojingsc 回访机制 + V4 5W1H + 资金对齐。

Tables:
  news_events        — 立案主表: 5W1H + 信源分级 + 关联标的 + 资金对齐快照
  event_checkpoints  — 三阶段回访: T+30m/T+2h/T+1d
  event_verdicts     — 结案报告: AI 合成的综合辩证结论

Thread-safe via threading.local() + WAL mode。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "news_events.db"

_local = threading.local()

# 三阶段回访
CHECKPOINTS = [
    ("T+30m", 30),
    ("T+2h", 120),
    ("T+1d", 1440),
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no         TEXT NOT NULL UNIQUE,
    news_id         TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    content_text    TEXT DEFAULT '',
    published_at    TEXT NOT NULL,

    -- 5W1H
    event_type      TEXT DEFAULT '未知',
    event_subtype   TEXT DEFAULT '',
    event_impact    REAL DEFAULT 0.0,
    sentiment_label TEXT DEFAULT '中性',
    sentiment_score REAL DEFAULT 0.0,
    entities_json   TEXT DEFAULT '[]',
    sectors_json    TEXT DEFAULT '[]',
    concepts_json   TEXT DEFAULT '[]',
    stock_symbols_json TEXT DEFAULT '[]',
    propagation_json TEXT DEFAULT '[]',
    summary_5w1h    TEXT DEFAULT '',

    -- 信源分级
    source_name     TEXT DEFAULT '',
    source_level    TEXT NOT NULL DEFAULT 'market_rumor_l3',
    source_label    TEXT NOT NULL DEFAULT '市场传闻三级',
    source_rule     TEXT DEFAULT '',
    credibility     REAL DEFAULT 50.0,

    -- 资金对齐快照 (立案时)
    cap_alignment_type  TEXT DEFAULT '',
    cap_alignment_score REAL DEFAULT 0.0,
    cap_phase           TEXT DEFAULT '',
    cap_score           REAL DEFAULT 50.0,
    cap_details_json    TEXT DEFAULT '[]',

    -- 价格基准快照 (立案时)
    baseline_prices_json TEXT DEFAULT '[]',

    -- 生命周期
    status          TEXT NOT NULL DEFAULT 'watching',
    final_verdict   TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_events_status ON news_events(status);
CREATE INDEX IF NOT EXISTS idx_events_published ON news_events(published_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON news_events(event_type);

CREATE TABLE IF NOT EXISTS event_checkpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES news_events(id),
    checkpoint      TEXT NOT NULL,
    due_at          TEXT NOT NULL,
    checked_at      TEXT,
    -- 价格回访
    up_count        INTEGER DEFAULT 0,
    down_count      INTEGER DEFAULT 0,
    flat_count      INTEGER DEFAULT 0,
    avg_pct_change  REAL DEFAULT 0.0,
    max_pct_change  REAL DEFAULT 0.0,
    min_pct_change  REAL DEFAULT 0.0,
    per_stock_json  TEXT DEFAULT '[]',
    -- 资金回访
    cap_phase       TEXT DEFAULT '',
    cap_score       REAL DEFAULT 50.0,
    cap_alignment   TEXT DEFAULT '',
    -- 判定
    verdict         TEXT DEFAULT '',
    verdict_text    TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_ckpt_event ON event_checkpoints(event_id);
CREATE INDEX IF NOT EXISTS idx_ckpt_due ON event_checkpoints(due_at, checked_at);

CREATE TABLE IF NOT EXISTS event_verdicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL UNIQUE REFERENCES news_events(id),
    case_no         TEXT NOT NULL,
    title           TEXT,
    published_at    TEXT,
    source_level    TEXT,
    event_type      TEXT,
    event_impact    REAL,
    sentiment_score REAL,
    -- 三阶段走势
    t30m_pct        REAL,
    t2h_pct         REAL,
    t1d_pct         REAL,
    cap_signals     TEXT DEFAULT '[]',
    -- AI 结案
    verdict_summary TEXT,
    market_reaction TEXT,
    risk_notes      TEXT,
    recommendation  TEXT,
    concluded_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_verdict_event ON event_verdicts(event_id);

CREATE TABLE IF NOT EXISTS event_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES news_events(id),
    news_id         TEXT NOT NULL,
    title           TEXT,
    content_text    TEXT DEFAULT '',
    source_name     TEXT DEFAULT '',
    source_label    TEXT DEFAULT '',
    source_level    TEXT DEFAULT '',
    published_at    TEXT,
    UNIQUE(news_id, event_id)
);
"""


def _add_citation(db, event_id, news_id, title, content, source_name, source_label, source_level, published_at):
    """将一个重复来源添加为现有事件的引用。提高可信度，扩展股票覆盖面。"""
    try:
        db.execute("""
            INSERT OR IGNORE INTO event_sources (event_id, news_id, title, content_text,
                source_name, source_label, source_level, published_at)
            VALUES (?,?,?,?, ?,?,?,?)
        """, (event_id, news_id, title, content, source_name, source_label, source_level, published_at))
        # 提升主事件的 credibility（多源确认 = 更高可信度）
        db.execute("""
            UPDATE news_events SET credibility = MIN(100, credibility + 3),
                updated_at = datetime('now','localtime')
            WHERE id = ?
        """, (event_id,))
    except Exception as e:
        logger.debug(f"_add_citation failed: {e}")


def get_event_sources(event_id: int) -> list[dict]:
    """获取某个事件的所有引用来源（多源报道列表）。"""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM event_sources WHERE event_id=? ORDER BY published_at ASC",
        (event_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        _local.conn = conn
    return conn


def close_db():
    conn = getattr(_local, "conn", None)
    if conn: conn.close(); _local.conn = None


def _next_case_no() -> str:
    today = datetime.now().strftime("%Y%m%d")
    db = get_db()
    row = db.execute("SELECT COUNT(*) AS c FROM news_events WHERE case_no LIKE ?", (f"{today}-%",)).fetchone()
    return f"{today}-{(row['c'] if row else 0) + 1:03d}"


# ═══════════════════════════════════════════════════════════════
# 立案
# ═══════════════════════════════════════════════════════════════

def create_event(news_id: str, title: str, content_text: str,
                 published_at: str = None,
                 # 5W1H
                 event_type: str = "未知", event_subtype: str = "",
                 event_impact: float = 0.0,
                 sentiment_label: str = "中性", sentiment_score: float = 0.0,
                 entities: list = None, sectors: list = None,
                 concepts: list = None, stock_symbols: list = None,
                 propagation: list = None, summary_5w1h: str = "",
                 # 信源
                 source_name: str = "", source_level: str = "market_rumor_l3",
                 source_label: str = "市场传闻三级", source_rule: str = "",
                 credibility: float = 50.0,
                 # 资金对齐
                 cap_alignment_type: str = "", cap_alignment_score: float = 0.0,
                 cap_phase: str = "", cap_score: float = 50.0,
                 cap_details: list = None,
                 # 价格基准
                 baseline_prices: list = None) -> Optional[dict]:
    db = get_db()
    existing = db.execute("SELECT * FROM news_events WHERE news_id=?", (news_id,)).fetchone()
    if existing:
        return dict(existing)

    # ── 智能去重: 同事件多源聚合 ──
    from datetime import datetime as _dt, timedelta as _td
    if published_at and stock_symbols and event_type and event_type != "未知":
        try:
            pub_dt = _dt.fromisoformat(published_at) if published_at else _dt.now()
            window_start = (pub_dt - _td(hours=2)).isoformat()
            candidates = db.execute("""
                SELECT * FROM news_events
                WHERE event_type = ?
                  AND published_at >= ?
                  AND status = 'watching'
                ORDER BY published_at DESC
                LIMIT 5
            """, (event_type, window_start)).fetchall()

            for cand in candidates:
                cand_syms = set(json.loads(cand["stock_symbols_json"] or "[]"))
                new_syms = set(stock_symbols or [])
                overlap = cand_syms & new_syms
                if len(overlap) >= 2 and len(overlap) >= len(new_syms) * 0.5:
                    db.execute("""INSERT OR IGNORE INTO event_sources
                        (event_id, news_id, title, content_text, source_name, source_label, source_level, published_at)
                        VALUES (?,?,?,?, ?,?,?,?)""",
                        (cand["id"], news_id, title, content_text,
                         source_name, source_label, source_level, published_at))
                    db.execute("UPDATE news_events SET credibility = MIN(100, credibility + 3), updated_at = datetime('now','localtime') WHERE id = ?", (cand["id"],))
                    db.commit()
                    logger.info(f"Event clustered: {title[:30]} -> merged into {cand['case_no']}")
                    return dict(cand)
        except Exception as e:
            logger.warning(f"Dedup cluster check failed: {e}", exc_info=True)
    # ── 去重结束 ──

    published_at = published_at or datetime.now().isoformat()
    case_no = _next_case_no()

    try:
        cur = db.execute("""
            INSERT INTO news_events
                (case_no, news_id, title, content_text, published_at,
                 event_type, event_subtype, event_impact,
                 sentiment_label, sentiment_score,
                 entities_json, sectors_json, concepts_json,
                 stock_symbols_json, propagation_json, summary_5w1h,
                 source_name, source_level, source_label, source_rule, credibility,
                 cap_alignment_type, cap_alignment_score, cap_phase, cap_score,
                 cap_details_json, baseline_prices_json, status)
            VALUES (?,?,?,?,?, ?,?,?, ?,?, ?,?,?, ?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,'watching')
        """, (
            case_no, news_id, title, content_text, published_at,
            event_type, event_subtype, event_impact,
            sentiment_label, sentiment_score,
            json.dumps(entities or [], ensure_ascii=False),
            json.dumps(sectors or [], ensure_ascii=False),
            json.dumps(concepts or [], ensure_ascii=False),
            json.dumps(stock_symbols or [], ensure_ascii=False),
            json.dumps(propagation or [], ensure_ascii=False),
            summary_5w1h,
            source_name, source_level, source_label, source_rule, credibility,
            cap_alignment_type, cap_alignment_score, cap_phase, cap_score,
            json.dumps(cap_details or [], ensure_ascii=False),
            json.dumps(baseline_prices or [], ensure_ascii=False),
        ))
        event_id = cur.lastrowid

        pub_dt = datetime.fromisoformat(published_at) if published_at else datetime.now()
        for ckpt_name, minutes in CHECKPOINTS:
            due_at = (pub_dt + timedelta(minutes=minutes)).isoformat()
            db.execute("INSERT INTO event_checkpoints (event_id, checkpoint, due_at) VALUES (?,?,?)",
                       (event_id, ckpt_name, due_at))

        db.commit()
        logger.info(f"News event created: {case_no} ({title[:30]})")
        return dict(db.execute("SELECT * FROM news_events WHERE id=?", (event_id,)).fetchone())
    except sqlite3.IntegrityError:
        db.rollback()
        return dict(db.execute("SELECT * FROM news_events WHERE news_id=?", (news_id,)).fetchone())
    except Exception as e:
        db.rollback()
        logger.error(f"create_event failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# 回访
# ═══════════════════════════════════════════════════════════════

def get_due_checkpoints(now: datetime = None) -> list[dict]:
    now = now or datetime.now()
    db = get_db()
    rows = db.execute("""
        SELECT ckpt.*, e.title, e.stock_symbols_json, e.baseline_prices_json,
               e.published_at, e.case_no, e.event_type, e.sentiment_score,
               e.cap_alignment_type, e.cap_score
        FROM event_checkpoints ckpt
        JOIN news_events e ON e.id = ckpt.event_id
        WHERE ckpt.checked_at IS NULL AND ckpt.due_at <= ?
        ORDER BY ckpt.due_at ASC
        LIMIT 10
    """, (now.isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def save_checkpoint_result(checkpoint_id: int, per_stock: list,
                           up: int, down: int, flat: int,
                           avg_pct: float, max_pct: float, min_pct: float,
                           cap_phase: str = "", cap_score: float = 50.0,
                           cap_alignment: str = "",
                           verdict: str = "", verdict_text: str = "") -> None:
    db = get_db()
    db.execute("""
        UPDATE event_checkpoints SET
            checked_at=?, per_stock_json=?, up_count=?, down_count=?, flat_count=?,
            avg_pct_change=?, max_pct_change=?, min_pct_change=?,
            cap_phase=?, cap_score=?, cap_alignment=?,
            verdict=?, verdict_text=?
        WHERE id=?
    """, (
        datetime.now().isoformat(), json.dumps(per_stock, ensure_ascii=False),
        up, down, flat, avg_pct, max_pct, min_pct,
        cap_phase, cap_score, cap_alignment,
        verdict, verdict_text, checkpoint_id,
    ))
    db.commit()


def conclude_event(event_id: int) -> Optional[dict]:
    """T+1d 回访完成后结案，生成 AI 辩证结论存入 event_verdicts."""
    db = get_db()
    event = db.execute("SELECT * FROM news_events WHERE id=?", (event_id,)).fetchone()
    if not event: return None
    event = dict(event)

    ckpts = db.execute("SELECT * FROM event_checkpoints WHERE event_id=? ORDER BY due_at ASC",
                       (event_id,)).fetchall()
    ckpts = [dict(c) for c in ckpts]

    done = [c for c in ckpts if c["checked_at"]]
    if not done or done[-1]["checkpoint"] != "T+1d":
        return None  # 还没到结案阶段

    # 合成结案报告
    t30m = next((c for c in ckpts if c["checkpoint"] == "T+30m" and c["checked_at"]), None)
    t2h  = next((c for c in ckpts if c["checkpoint"] == "T+2h" and c["checked_at"]), None)
    t1d  = next((c for c in ckpts if c["checkpoint"] == "T+1d" and c["checked_at"]), None)

    verdict_summary, market_reaction, risk_notes, recommendation = _synthesize(event, t30m, t2h, t1d)

    try:
        db.execute("""
            INSERT OR REPLACE INTO event_verdicts
                (event_id, case_no, title, published_at, source_level,
                 event_type, event_impact, sentiment_score,
                 t30m_pct, t2h_pct, t1d_pct, cap_signals,
                 verdict_summary, market_reaction, risk_notes, recommendation)
            VALUES (?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?,?,?)
        """, (
            event_id, event["case_no"], event["title"], event["published_at"],
            event["source_level"], event["event_type"], event["event_impact"],
            event["sentiment_score"],
            round(t30m["avg_pct_change"], 2) if t30m else None,
            round(t2h["avg_pct_change"], 2) if t2h else None,
            round(t1d["avg_pct_change"], 2) if t1d else None,
            json.dumps([c.get("cap_alignment","") for c in done], ensure_ascii=False),
            verdict_summary, market_reaction, risk_notes, recommendation,
        ))

        verdict_text = f"{verdict_summary} {market_reaction} {risk_notes} {recommendation}"
        db.execute("UPDATE news_events SET status='concluded', final_verdict=?, updated_at=? WHERE id=?",
                   (verdict_text[:500], datetime.now().isoformat(), event_id))
        db.commit()
        return dict(db.execute("SELECT * FROM event_verdicts WHERE event_id=?", (event_id,)).fetchone())
    except Exception as e:
        db.rollback()
        logger.error(f"conclude_event failed: {e}")
        return None


def _synthesize(event: dict, t30m: dict, t2h: dict, t1d: dict) -> tuple:
    pcts = [c["avg_pct_change"] for c in [t30m, t2h, t1d] if c]
    caps = [c.get("cap_alignment","") for c in [t30m, t2h, t1d] if c]
    divergent_signals = [c for c in caps if "背离" in c]
    trend_up = len(pcts) >= 2 and pcts[-1] > pcts[0]

    label = event.get("source_label","未知")
    # Verdict summary
    if trend_up and pcts[-1] > 1.0:
        verdict = f"【{label}】消息市场反应持续走强。"
        reaction = f"T+30m: {pcts[0]:+.2f}% → T+1d: {pcts[-1]:+.2f}%，涨幅持续扩大，非一次性脉冲。"
        risk = "警惕短线过热后的回调风险。"
        rec = "若资金继续共振，可适度参与；设好止损。"
    elif trend_up:
        verdict = f"【{label}】消息温和发酵，中性偏正向。"
        reaction = f"T+30m: {pcts[0]:+.2f}% → T+1d: {pcts[-1]:+.2f}%，走势温和，市场逐步消化。"
        risk = "关注消息面后续变化。"
        rec = "可轻仓观察，等待情绪强化信号。"
    elif len(pcts) >= 2 and pcts[0] > 1.0 and pcts[-1] < 0:
        verdict = f"【{label}】短线冲高后回落，利好出尽特征。"
        reaction = f"T+30m 脉冲 +{pcts[0]:+.2f}%，但 T+1d 回落至 {pcts[-1]:+.2f}%。"
        risk = "追高被套风险高，该消息已被市场充分消化。"
        rec = "回避追高，等待回调企稳后再评估。"
    elif all(abs(p) < 0.5 for p in pcts):
        verdict = f"【{label}】消息市场反应平淡。"
        reaction = f"三检查点波动均小于0.5%，该消息已被price in或影响力不及预期。"
        risk = "无显著风险。"
        rec = "该消息不构成交易依据，维持原有策略。"
    elif divergent_signals:
        verdict = f"【{label}】消息与资金出现背离信号。"
        reaction = f"出现 {len(divergent_signals)} 次资金背离，市场实际行为与消息方向不一致。"
        risk = "背离可能是主力反向操作信号，需高度警惕。"
        rec = f"若为「背离-吸筹」，可能是布局良机；若为「背离-出货」，建议减仓。"
    else:
        verdict = f"【{label}】消息影响中性。"
        reaction = f"三检查点走势无明显方向性特征。"
        risk = "观望。"
        rec = "维持原有策略不变。"

    return verdict, reaction, risk, rec


# ═══════════════════════════════════════════════════════════════
# 查询
# ═══════════════════════════════════════════════════════════════

def list_recent(limit: int = 50, status: str = None, event_type: str = None) -> list[dict]:
    db = get_db()
    sql = "SELECT * FROM news_events WHERE 1=1"
    params = []
    if status:
        sql += " AND status=?"; params.append(status)
    if event_type:
        sql += " AND event_type=?"; params.append(event_type)
    sql += " ORDER BY published_at DESC LIMIT ?"; params.append(limit)
    events = [dict(r) for r in db.execute(sql, params).fetchall()]
    # 批量补充引用源数量
    for e in events:
        cnt = db.execute("SELECT COUNT(*) as c FROM event_sources WHERE event_id=?", (e["id"],)).fetchone()
        e["source_count"] = (cnt["c"] if cnt else 0) + 1  # +1 包括主源
    return events


def query_by_symbol(symbol: str, since: str = None, limit: int = 50) -> list[dict]:
    """Query news events that mention a given stock symbol.

    Matches against the stock_symbols_json column (JSON array of stock codes)
    using LIKE pattern matching.
    """
    db = get_db()
    sql = "SELECT * FROM news_events WHERE stock_symbols_json LIKE ?"
    params = [f'%"{symbol}"%']

    if since:
        sql += " AND published_at >= ?"
        params.append(since)

    sql += " ORDER BY published_at DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_event(event_id: int = None, case_no: str = None) -> Optional[dict]:
    db = get_db()
    if event_id:
        row = db.execute("SELECT * FROM news_events WHERE id=?", (event_id,)).fetchone()
    elif case_no:
        row = db.execute("SELECT * FROM news_events WHERE case_no=?", (case_no,)).fetchone()
    else:
        return None
    return dict(row) if row else None


def get_checkpoints(event_id: int) -> list[dict]:
    db = get_db()
    return [dict(r) for r in db.execute(
        "SELECT * FROM event_checkpoints WHERE event_id=? ORDER BY due_at ASC", (event_id,)).fetchall()]


def get_verdict(event_id: int) -> Optional[dict]:
    db = get_db()
    row = db.execute("SELECT * FROM event_verdicts WHERE event_id=?", (event_id,)).fetchone()
    return dict(row) if row else None


def get_insights(days: int = 30) -> dict:
    """统计洞察: 各事件类型的平均反应/背离概率/可信度分布."""
    db = get_db()
    rows = db.execute("""
        SELECT event_type, COUNT(*) as cnt,
               AVG(event_impact) as avg_impact,
               AVG(CASE WHEN cap_alignment_type LIKE '%背离%' THEN 1 ELSE 0 END) as divergence_rate,
               AVG(sentiment_score) as avg_sentiment,
               AVG(credibility) as avg_credibility
        FROM news_events
        WHERE published_at >= ?
        GROUP BY event_type
        ORDER BY cnt DESC
    """, ((datetime.now() - timedelta(days=days)).isoformat(),)).fetchall()
    return {
        "days": days,
        "total_events": sum(r["cnt"] for r in rows),
        "by_type": [dict(r) for r in rows],
    }
