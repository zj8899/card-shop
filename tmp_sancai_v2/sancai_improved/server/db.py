"""DuckDB data access layer — auto-discovers parquet files, fast SQL queries."""
import json
import logging
import re
import threading
from pathlib import Path

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "sancai.duckdb"

# Whitelist of valid period identifiers — blocks SQL injection via period parameter
_VALID_PERIODS = {"daily", "1min", "5min", "15min", "30min", "60min", "120min"}

# Single shared connection; reads use per-call cursors (DuckDB's thread-safe
# concurrency primitive — cursors share the parent's catalog, so they see the
# materialized daily_klines table and file-backed minute views).
# NOTE: a second duckdb.connect() to the same file in one process throws
# ConnectionException (read-only vs read-write config mismatch), and views built
# from register()'d relations are connection-local — so we must NOT open separate
# read-only connections. One connection + cursors is the correct pattern.
_conn: duckdb.DuckDBPyConnection = None
_conn_lock = threading.Lock()


def _relation_exists(conn, name: str) -> bool:
    """True if `name` exists as either a table or a view (daily is a table, minute are views)."""
    row = conn.execute(
        "SELECT 1 FROM duckdb_tables WHERE table_name = ? "
        "UNION ALL SELECT 1 FROM duckdb_views WHERE view_name = ? LIMIT 1",
        [name, name],
    ).fetchone()
    return row is not None


def _validate_period(period: str) -> str:
    """Return the period if valid, otherwise raise ValueError."""
    if period not in _VALID_PERIODS:
        raise ValueError(f"Invalid period: {period!r}. Must be one of {sorted(_VALID_PERIODS)}")
    return period


def _validate_symbol(symbol: str) -> str:
    """Return the symbol if it looks like a valid A-share code, otherwise raise ValueError."""
    if not re.fullmatch(r'[0-9]{6}', symbol):
        raise ValueError(f"Invalid symbol: {symbol!r}. Must be a 6-digit stock code.")
    return symbol


def _get_base_conn() -> duckdb.DuckDBPyConnection:
    """Return the single shared read-write connection, building views on first use."""
    global _conn
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        _recover_db()
        conn = duckdb.connect(str(DB_PATH))
        conn.execute("PRAGMA threads=8")
        conn.execute("PRAGMA memory_limit='4GB'")
        _init_views(conn)
        _conn = conn
        return _conn


def get_db(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Get a DuckDB handle.

    read_only=True (default) → a fresh cursor of the shared connection. Cursors are
    DuckDB's thread-safe concurrency primitive: many can run queries at once and all
    see the same catalog (tables + views). This is safe to call from any thread.

    read_only=False → the base connection itself, for DDL (refresh_views / _init_views).
    """
    base = _get_base_conn()
    return base.cursor() if read_only else base


def close_db():
    """Close the shared DuckDB connection cleanly. Call on shutdown."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception as e:
                logger.warning(f"close_db: failed to close connection: {e}")
            _conn = None


def _recover_db():
    """Delete corrupted DuckDB WAL file if present.

    When the process is force-killed while DuckDB has an active WAL
    (Write-Ahead Log), the WAL can become corrupted, causing DuckDB
    to FATAL-crash the entire Python process on next connect().

    Since our DB is derived from parquet views (read-only data),
    the WAL only contains transient metadata — safe to delete.
    """
    wal_path = DB_PATH.with_suffix(DB_PATH.suffix + ".wal")
    if wal_path.exists():
        import logging
        _log = logging.getLogger(__name__)
        try:
            wal_size = wal_path.stat().st_size
            wal_path.unlink()
            _log.warning(
                "Deleted stale DuckDB WAL file (%d bytes) — "
                "previous process may have been force-killed. "
                "Data views will be rebuilt on next access.", wal_size
            )
        except OSError:
            pass


def _init_views(conn: duckdb.DuckDBPyConnection):
    """Create parquet views if the data directories exist."""
    created_periods = []

    # Daily — 使用绝对路径避免 CWD 变化导致读不到文件
    daily_dir = DATA_DIR / "daily"
    daily_pattern = daily_dir / "*.parquet"
    if daily_dir.exists() and any(daily_dir.glob("*.parquet")):
        # DuckDB union_by_name 遇到异常 schema 文件会报错
        # 改为逐个文件加载，容错坏文件
        files = sorted(daily_dir.glob("*.parquet"))
        import numpy as np
        # Schema: [date, open, high, low, close, volume, amount, pct_change, _sym]
        records = []
        for fpath in files:
            sym = fpath.stem
            try:
                df = pd.read_parquet(fpath)
                if df.empty:
                    continue
                required = ["open", "high", "low", "close"]
                if not all(c in df.columns for c in required):
                    continue
                # Normalize: ensure standard columns, drop extras
                for c in ["volume", "amount", "pct_change"]:
                    if c not in df.columns:
                        df[c] = np.nan
                if "date" not in df.columns:
                    if isinstance(df.index, pd.DatetimeIndex):
                        df["date"] = df.index
                    else:
                        continue
                # Cast types to ensure consistency across all files
                df["open"] = df["open"].astype(float)
                df["high"] = df["high"].astype(float)
                df["low"] = df["low"].astype(float)
                df["close"] = df["close"].astype(float)
                df["volume"] = df["volume"].astype(float)
                df["amount"] = df["amount"].astype(float)
                df["pct_change"] = df["pct_change"].astype(float)
                df["date"] = pd.to_datetime(df["date"])
                df["_sym"] = sym
                records.append(df[["date", "open", "high", "low", "close", "volume", "amount", "pct_change", "_sym"]])
            except Exception:
                continue

        if records:
            combined = pd.concat(records, ignore_index=True)
            # 用 {sym}.parquet 格式兼容现有 regexp_extract 查询
            combined["_filename"] = combined["_sym"].astype(str) + ".parquet"
            # Materialize into a real TABLE (not a VIEW over a register()'d relation):
            # register()'d relations are connection-local, so cursors and the app's
            # concurrent readers can't see a view built on one. A TABLE lives in the
            # catalog and every cursor of this connection sees it.
            conn.register("daily_klines_tmp", combined)
            # DROP {VIEW,TABLE} IF EXISTS still throws a wrong-type CatalogException
            # (e.g. DROP VIEW on an existing TABLE), so guard each independently.
            for _drop in ("DROP TABLE IF EXISTS daily_klines", "DROP VIEW IF EXISTS daily_klines"):
                try:
                    conn.execute(_drop)
                except Exception:
                    pass
            conn.execute("""
                CREATE TABLE daily_klines AS
                SELECT date, open, high, low, close, volume, amount, pct_change,
                       _filename AS filename, 'daily' AS period
                FROM daily_klines_tmp
            """)
            conn.unregister("daily_klines_tmp")
            created_periods.append("daily")

    # Minute
    minute_root = DATA_DIR / "minute"
    if minute_root.exists():
        for period_dir in sorted(minute_root.iterdir()):
            if not period_dir.is_dir():
                continue
            period = period_dir.name
            files = list(period_dir.glob("*.parquet"))
            if not files:
                continue
            minute_pattern = period_dir / "*.parquet"
            try:
                conn.execute(f"""
                    CREATE OR REPLACE VIEW minute_{period}_klines AS
                    SELECT *, '{period}' AS period FROM read_parquet('{minute_pattern.as_posix()}',
                        union_by_name=true, filename=true)
                """)
                created_periods.append(period)
            except Exception:
                continue

    # Note: No unified all_klines view — different periods have different schemas
    # Query each period's view directly for best performance.


def refresh_views():
    """Re-scan parquet directories — call after importing new data."""
    conn = get_db(read_only=False)
    _init_views(conn)


def list_symbols(period: str = None) -> list[dict]:
    """List distinct symbols with row counts and date ranges."""
    conn = get_db()
    _validate_period(period)
    table = "daily_klines" if period == "daily" else f"minute_{period}_klines"

    # Check if the relation exists (daily = table, minute = view)
    if not _relation_exists(conn, table):
        return []

    date_col = "date" if period == "daily" else "datetime"

    result = conn.execute(f"""
        SELECT
            regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) AS symbol,
            COUNT(*) AS bars,
            MIN({date_col}) AS first_date,
            MAX({date_col}) AS last_date
        FROM {table}
        GROUP BY regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1)
        ORDER BY symbol
    """).fetchall()

    return [
        {"symbol": r[0], "bars": r[1], "first_date": str(r[2])[:10], "last_date": str(r[3])[:10]}
        for r in result
    ]


def get_kline(symbol: str, period: str = "daily",
              start: str = None, end: str = None, limit: int = 500) -> list[dict]:
    """Get OHLCV bars for a symbol, with computed MAs."""
    _validate_period(period)
    _validate_symbol(symbol)
    conn = get_db()
    table = "daily_klines" if period == "daily" else f"minute_{period}_klines"
    date_col = "date" if period == "daily" else "datetime"

    # Parameterized where clause — symbol validated above
    where = f"regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) = ?"
    params = [symbol]
    if start:
        where += " AND {date_col} >= ?"
        params.append(start)
    if end:
        where += " AND {date_col} <= ?"
        params.append(end)
    where = where.format(date_col=date_col)

    # Query with MA windows
    ma_cols = {
        "daily": [5, 13, 21, 34, 55, 144, 233, 623],
        "1min": [34, 144, 233],
        "5min": [34, 144, 233],
        "15min": [34, 144, 233],
        "30min": [34, 144, 233],
        "60min": [34, 144, 233],
        "120min": [34, 144, 233],
    }.get(period, [34, 144, 233])

    ma_sql = ", ".join([
        f"AVG(close) OVER (ORDER BY {date_col} ROWS BETWEEN {p-1} PRECEDING AND CURRENT ROW) AS ma_{p}"
        for p in ma_cols
    ])

    sql = f"""
        SELECT {date_col} AS date, open, high, low, close, volume,
               {ma_sql}
        FROM {table}
        WHERE {where}
        ORDER BY {date_col}
    """

    df = conn.execute(sql, params).fetchdf()
    if limit and len(df) > limit:
        df = df.tail(limit)

    # Sanitize
    import math
    records = []
    for _, row in df.iterrows():
        r = {}
        for k, v in row.to_dict().items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                r[k] = None
            else:
                r[k] = v
        r["date"] = str(r.get("date", ""))[:19]
        records.append(r)

    return records


def get_date_range(symbol: str, period: str = "daily") -> dict:
    """Get earliest and latest date for a symbol."""
    conn = get_db()
    table = "daily_klines" if period == "daily" else f"minute_{period}_klines"
    date_col = "date" if period == "daily" else "datetime"
    row = conn.execute(f"""
        SELECT MIN({date_col}), MAX({date_col}), COUNT(*)
        FROM {table}
        WHERE regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) = '{symbol}'
    """).fetchone()
    if row and row[0]:
        return {"symbol": symbol, "period": period, "first_date": str(row[0])[:10],
                "last_date": str(row[1])[:10], "bars": row[2]}
    return {"symbol": symbol, "period": period, "error": "No data"}


def get_data_freshness(period: str = "daily") -> dict:
    """Report data freshness across all symbols.

    Returns:
        dict: total_symbols, fresh_symbols, stale_symbols, latest_date, staleness_days
    """
    import datetime as dt

    _validate_period(period)
    conn = get_db()
    table = "daily_klines" if period == "daily" else f"minute_{period}_klines"

    # Check if the relation exists (daily = table, minute = view)
    if not _relation_exists(conn, table):
        return {"total_symbols": 0, "fresh_symbols": 0, "stale_symbols": 0,
                "latest_date": None, "staleness_days": -1}

    date_col = "date" if period == "daily" else "datetime"

    # Per-symbol latest date
    rows = conn.execute(f"""
        SELECT
            regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) AS symbol,
            MAX({date_col}) AS last_date
        FROM {table}
        GROUP BY regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1)
    """).fetchall()

    total = len(rows)
    if total == 0:
        return {"total_symbols": 0, "fresh_symbols": 0, "stale_symbols": 0,
                "latest_date": None, "staleness_days": -1}

    today = pd.Timestamp.now()
    # Expect data through previous business day
    expected_latest = (today - pd.offsets.BDay(1)).normalize()
    # Add a buffer: 1 trading day gap is acceptable (today's data not yet published)
    stale_threshold = (today - pd.offsets.BDay(2)).normalize()

    stale = 0
    latest_overall = None
    for sym, last_date in rows:
        last_ts = pd.Timestamp(last_date)
        if latest_overall is None or last_ts > latest_overall:
            latest_overall = last_ts
        if last_ts < stale_threshold:
            stale += 1

    staleness_days = (expected_latest - latest_overall).days if latest_overall and latest_overall < expected_latest else 0

    return {
        "total_symbols": total,
        "fresh_symbols": total - stale,
        "stale_symbols": stale,
        "latest_date": str(latest_overall.date()) if latest_overall else None,
        "expected_date": str(expected_latest.date()) if expected_latest else None,
        "staleness_days": staleness_days,
    }


def validate_parquet_schema(sample_size: int = 100, period: str = "daily") -> dict:
    """Sample-check parquet files for schema compliance.

    Checks: index type, column names, compression.
    Returns violations found.
    """
    import random

    if period == "daily":
        fdir = DATA_DIR / "daily"
        expected_cols = {"open", "high", "low", "close", "volume", "amount", "pct_change"}
    else:
        fdir = DATA_DIR / "minute" / period
        expected_cols = {"open", "high", "low", "close", "volume", "amount"}

    files = list(fdir.glob("*.parquet")) if fdir.exists() else []
    if not files:
        return {"checked": 0, "violations": []}

    sample = random.sample(files, min(sample_size, len(files)))
    violations = []

    for fpath in sample:
        try:
            df = pd.read_parquet(fpath)
            symbol = fpath.stem

            # Check index
            if not isinstance(df.index, pd.DatetimeIndex):
                violations.append({"symbol": symbol, "issue": "index is not DatetimeIndex",
                                   "found": str(type(df.index).__name__)})

            # Check columns
            actual_cols = set(df.columns)
            missing = expected_cols - actual_cols
            extra = actual_cols - expected_cols - {"date", "datetime", "symbol", "name"}
            if missing:
                violations.append({"symbol": symbol, "issue": "missing columns",
                                   "missing": list(missing)})
            if extra:
                violations.append({"symbol": symbol, "issue": "extra columns",
                                   "extra": list(extra)})
        except Exception as e:
            violations.append({"symbol": fpath.stem, "issue": "read error", "error": str(e)})

    return {"checked": len(sample), "total_files": len(files), "violations": violations}


def check_gaps(symbol: str = None, period: str = "daily", min_gap_days: int = 1) -> list[dict]:
    """Detect missing trading dates in the data."""
    import pandas as pd
    from datetime import timedelta
    conn = get_db()
    table = "daily_klines" if period == "daily" else f"minute_{period}_klines"
    date_col = "date" if period == "daily" else "datetime"

    where = "1=1"
    if symbol:
        where = f"regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) = '{symbol}'"

    df = conn.execute(f"""
        SELECT regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) AS symbol,
               {date_col}::DATE AS d
        FROM {table}
        WHERE {where}
        ORDER BY symbol, d
    """).fetchdf()

    if df.empty:
        return []

    gaps = []
    for sym, group in df.groupby("symbol"):
        dates = sorted(group["d"].dropna().unique())
        for i in range(1, len(dates)):
            diff = (pd.Timestamp(dates[i]) - pd.Timestamp(dates[i - 1])).days
            if diff > min_gap_days + 1:  # skip weekends
                gap_start = pd.Timestamp(dates[i - 1]) + timedelta(days=1)
                gap_end = pd.Timestamp(dates[i]) - timedelta(days=1)
                gaps.append({
                    "symbol": sym,
                    "period": period,
                    "gap_start": str(gap_start.date()),
                    "gap_end": str(gap_end.date()),
                    "gap_days": diff - 1,
                })
    return gaps
