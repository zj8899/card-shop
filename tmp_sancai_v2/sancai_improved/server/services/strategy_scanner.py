"""策略驱动的全市场买点扫描器 (Strategy-driven Market Scanner).

Scans all A-share stocks with local daily K-line data and returns those
matching a given strategy's BUY signal on the latest bar.

Two computation paths:
  Path A (lightweight): strict / simple — pure numeric conditions, scans all
    5800+ stocks in ~2 seconds using vectorized MA/KDJ computation.
  Path B (school-based): individual schools / schools ensemble — uses
    school.generate_signals() per symbol, parallelized via ThreadPoolExecutor.
"""
from __future__ import annotations

import json
import logging
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"


# ═══════════════════════════════════════════════════════════════════════════════
# Symbol discovery
# ═══════════════════════════════════════════════════════════════════════════════

def get_available_symbols(data_dir: Path = None) -> list[str]:
    """Return all stock symbols that have daily parquet data."""
    if data_dir is None:
        data_dir = DATA_DIR
    daily_dir = data_dir / "daily"
    if not daily_dir.exists():
        return []
    return sorted(f.stem for f in daily_dir.glob("*.parquet"))


def _get_name_map() -> dict[str, str]:
    """Build a symbol→name mapping from local parquet metadata and config.

    ZERO akshare dependency — avoids py_mini_racer V8 crash.
    Uses metadata.db as primary source, parquet glob as fallback.
    """
    name_map = {}

    # 1) Try metadata.db (SQLite, no akshare)
    try:
        import sqlite3
        meta_path = PROJECT_ROOT / "data" / "metadata.db"
        if meta_path.exists():
            conn = sqlite3.connect(str(meta_path))
            cursor = conn.execute("SELECT code, name FROM stock_info")
            for row in cursor.fetchall():
                if row[0] and row[1]:
                    code = str(row[0]).strip().zfill(6)
                    name_map[code] = str(row[1]).strip()
            conn.close()
            if name_map:
                logger.info(f"Name map built from metadata.db: {len(name_map)} stocks")
                return name_map
    except Exception:
        logger.debug("metadata.db name lookup failed, trying alternatives")

    # 2) Scan parquet files directly — symbol = filename stem
    daily_dir = DATA_DIR / "daily"
    if daily_dir.exists():
        for fp in daily_dir.glob("*.parquet"):
            sym = fp.stem
            if sym not in name_map and not any(sym.startswith(p) for p in ("sh", "sz", "bj")):
                name_map[sym] = sym  # fallback: code = name

    # 3) Config universe (always available)
    try:
        from server.utils.config import get_config
        cfg = get_config()
        for entry in cfg.get("universe", []):
            sym = entry["symbol"]
            name_map[sym] = entry.get("name", sym)
    except Exception:
        pass

    return name_map


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════════

def _bulk_load_via_duckdb(symbols: list[str], tail_bars: int = 300,
                          live_bars: dict = None) -> dict[str, pd.DataFrame]:
    """Bulk load daily data for all symbols via DuckDB.

    If live_bars is provided, appends each symbol's live bar as a new row
    (in-memory, does NOT modify disk data). This lets strategies evaluate
    against today's real-time price instead of yesterday's close.
    """
    from server.db import get_db
    db = get_db()
    placeholders = ", ".join(["?" for _ in symbols])
    sql = f"""
        SELECT *, regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) AS _sym
        FROM daily_klines
        WHERE regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) IN ({placeholders})
        ORDER BY _sym, date
    """
    df_all = db.execute(sql, symbols).fetchdf()
    if df_all.empty:
        return {}

    result = {}
    for sym, grp in df_all.groupby("_sym"):
        df = grp.drop(columns=["_sym"])
        if len(df) > tail_bars:
            df = df.tail(tail_bars)
        df = df.reset_index(drop=True)

        # 追加实时 bar（内存操作，不改磁盘）
        if live_bars and sym in live_bars:
            bar = live_bars[sym]
            if bar.get("price", 0) > 0:
                today_str = str(bar.get("date", pd.Timestamp.now().strftime("%Y-%m-%d")))
                # 检查是否已有今天的数据（避免重复追加）
                last_date = str(df["date"].iloc[-1])[:10] if "date" in df.columns else ""
                if last_date != today_str:
                    volume = bar.get("volume", 0) or 100
                    new_row = pd.DataFrame([{
                        "date": today_str,
                        "open": bar.get("open", bar["price"]),
                        "high": bar.get("high", bar["price"]),
                        "low": bar.get("low", bar["price"]),
                        "close": bar["price"],
                        "volume": volume,
                        "amount": bar.get("amount", 0) or 0,
                        "pct_change": bar.get("change_pct", 0) or 0,
                    }])
                    df = pd.concat([df, new_row], ignore_index=True)
        result[sym] = df
    logger.info(f"DuckDB loaded {len(result)} symbols (tail_bars={tail_bars}, live_bars={bool(live_bars)})")
    return result


def _load_single_parquet(symbol: str, tail_bars: int = 300) -> Optional[pd.DataFrame]:
    """Load daily data for a single symbol from parquet (fallback)."""
    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        return None
    try:
        df = pd.read_parquet(fpath)
        if len(df) > tail_bars:
            df = df.tail(tail_bars)
        return df.reset_index(drop=True)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Indicator computation helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_kdj(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 n: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute KDJ (K, D, J) arrays — vectorized numpy implementation."""
    n = int(n)
    k = np.full(len(close), 50.0)
    d = np.full(len(close), 50.0)

    # Rolling max/min over window n
    if len(close) > n:
        # Use pandas for rolling max/min (fastest on numpy arrays)
        import pandas as _pd
        s_high = _pd.Series(high)
        s_low = _pd.Series(low)
        s_close = _pd.Series(close)
        hh = s_high.rolling(n, min_periods=1).max().values
        ll = s_low.rolling(n, min_periods=1).min().values

        for i in range(n, len(close)):
            denom = hh[i] - ll[i]
            rsv = ((close[i] - ll[i]) / denom * 100.0) if denom > 0 else 50.0
            k[i] = 2/3 * k[i-1] + 1/3 * rsv
            d[i] = 2/3 * d[i-1] + 1/3 * k[i]

    j = 3 * k - 2 * d
    return k, d, j


def _compute_mas(close: np.ndarray, periods: list[int]) -> dict[int, np.ndarray]:
    """Compute moving averages for given periods."""
    mas = {}
    for p in periods:
        mas[p] = pd.Series(close).rolling(window=p).mean().values
    return mas


def _ma_slope(mas: dict[int, np.ndarray], period: int, lookback: int = 10) -> float:
    """Compute MA slope over last N bars (pure numpy, no Rust FFI).

    Returns slope as fraction: (MA_now - MA_N_bars_ago) / MA_N_bars_ago.
    Positive = accelerating, Negative = decelerating.
    """
    ma = mas.get(period)
    if ma is None or len(ma) <= lookback:
        return 0.0
    now = ma[-1]
    past = ma[-lookback - 1]
    if np.isnan(now) or np.isnan(past) or past <= 0:
        return 0.0
    return (now - past) / past


# ═══════════════════════════════════════════════════════════════════════════════
# Stock quality filters
# ═══════════════════════════════════════════════════════════════════════════════

# Symbols known to be indices, not tradable stocks
_INDEX_PREFIXES = {"sh", "sz", "bj"}
_INDEX_SUFFIXES = {"399001", "399005", "399006", "399300", "000852", "000905"}


def _is_tradable_stock(symbol: str, name: str = "", price: float = 0) -> bool:
    """Filter out indices, delisted, and garbage stocks."""
    # Filter indices
    if any(symbol.startswith(p) for p in _INDEX_PREFIXES):
        return False
    # Filter known index codes stored as 6-digit
    if symbol in _INDEX_SUFFIXES:
        return False
    # Filter delisted / zombie stocks (price near zero)
    if price > 0 and price < 0.50:
        return False
    return True


def _is_quality_stock(name: str = "", price: float = 0) -> tuple[bool, str]:
    """Check stock quality. Returns (is_ok, reason).

    Rejects: ST/*ST/退市 stocks, ultra-low-price penny stocks.
    """
    if not name:
        return True, ""
    if "ST" in name.upper() or "*ST" in name or "退市" in name:
        return False, "ST或退市"
    return True, ""


def _classify_chan_buy_type(reason: str = "") -> str:
    """Parse chan_theory buy point type from reason string.

    Returns 'first', 'second', 'third', or 'unknown'.
    """
    if "一买" in reason or "first" in reason.lower():
        return "first"
    if "二买" in reason or "second" in reason.lower():
        return "second"
    if "三买" in reason or "third" in reason.lower():
        return "third"
    return "unknown"


BUY_TYPE_QUALITY = {"first": 3, "second": 2, "third": 1, "unknown": 0}

# ── Reason label shortener ──
_REASON_LABELS = {"strict": "BP1 抄底", "strict_reverse": "追涨突破", "simple": "KDJ超卖反弹"}
_CHAN_BUY_LABELS = {"first": "缠论一买", "second": "缠论二买", "third": "缠论三买", "unknown": "缠论买入"}
_SCHOOL_SHORT = {"ict": "ICT买入", "price_action": "价行买入", "wyckoff": "威科夫买入",
                 "morphology": "形态买入", "gann": "江恩买入", "wave_theory": "波浪买入", "dow_theory": "道氏买入"}


def _shorten_reason(mode: str, reason: str = "", buy_type: str = "") -> str:
    """Convert verbose strategy reason string to a compact label."""
    if mode in _REASON_LABELS:
        return _REASON_LABELS[mode]
    if mode == "schools":
        import re
        m = re.match(r"多流派共识买入\((\d+)/8\)", reason)
        return f"多流派共识({m.group(1)}/8)" if m else "多流派共识"
    if mode == "chan_theory":
        bt = buy_type or _classify_chan_buy_type(reason)
        return _CHAN_BUY_LABELS.get(bt, "缠论买入")
    if mode in _SCHOOL_SHORT:
        return _SCHOOL_SHORT[mode]
    return reason[:14] if reason else mode


def enrich_results_with_live_data(results: list[dict], max_symbols: int = 200) -> list[dict]:
    """Enrich scan results with live turnover data from Tencent API.

    Adds: turnover_rate, amount_wan (实时成交额万元), vol_ratio (量比)
    Only enriches first `max_symbols` results to avoid API rate limits.
    """
    if not results:
        return results
    try:
        from data_sources.tencent_quotes import tencent_quote
        syms = [r["symbol"] for r in results[:max_symbols] if r.get("symbol")]
        if not syms:
            return results
        live = tencent_quote(syms)
        for r in results:
            sym = r.get("symbol", "")
            # tencent_quote key is 6-digit; try both prefixed and unprefixed
            q = live.get(sym) or live.get(sym[2:] if sym.startswith(("sh","sz","bj")) else "")
            if q:
                r["turnover_rate"] = q.get("turnover_pct", 0) or 0
                r["amount_wan"] = q.get("amount_wan", 0) or 0
                r["vol_ratio"] = q.get("vol_ratio", 0) or 0
                # Also fill real-time price/change if available
                if q.get("price", 0) > 0:
                    r["live_price"] = q["price"]
                    r["change_pct"] = q.get("change_pct", r.get("change_pct", 0))
                r["name"] = q.get("name", r.get("name", ""))
    except Exception:
        pass
    return results


def _enrich_results_with_news(results: list[dict]) -> list[dict]:
    """Enrich scan results with recent news sentiment data.

    Adds for each result: news_count, news_sentiment_score, latest_news_title, news_risk_flag
    Queries news_event_db for recent news matching each stock symbol.
    Does NOT filter results — only annotates for frontend display.
    """
    if not results:
        return results
    try:
        from pathlib import Path
        from server.news_event_db import query_by_symbol
        from datetime import datetime, timedelta

        
        cutoff = (datetime.now() - timedelta(days=5)).isoformat()

        for r in results:
            sym = r.get("symbol", "")
            if not sym:
                continue
            # Query recent news for this stock
            try:
                news_items = query_by_symbol(sym, since=cutoff, limit=10)
            except Exception:
                news_items = []

            r["news_count"] = len(news_items)
            r["news_sentiment_score"] = 0.0
            r["latest_news_title"] = ""
            r["news_risk_flag"] = False

            if news_items:
                # Compute average sentiment from news items
                sentiments = []
                for n in news_items:
                    s = n.get("sentiment_score") or n.get("sentiment")
                    if s is not None:
                        try:
                            sentiments.append(float(s))
                        except (ValueError, TypeError):
                            pass
                if sentiments:
                    r["news_sentiment_score"] = round(sum(sentiments) / len(sentiments), 3)
                # Latest news title
                r["latest_news_title"] = news_items[0].get("title", "") or ""
                # Risk flag: extreme negative sentiment or high-impact negative news
                if r["news_sentiment_score"] < -0.5:
                    r["news_risk_flag"] = True
                # Also flag if any single news has high negative impact
                for n in news_items:
                    impact = n.get("impact", "")
                    if impact and impact.lower() in ("negative", "利空", "减持"):
                        r["news_risk_flag"] = True
                        break
    except ImportError:
        pass
    except Exception:
        logger.debug("News enrichment skipped (news_event_db may not be available)")
    return results


def _extract_volume_metrics(df: pd.DataFrame) -> dict:
    """Extract volume/amount/turnover metrics from the last bar of a DataFrame.

    Returns dict with: volume, amount, change_pct, volume_ratio_5d
    """
    metrics = {"volume": 0, "amount": 0.0, "change_pct": 0.0, "volume_ratio_5d": 0.0}
    try:
        close_vals = df["close"].values
        vol_vals = df["volume"].values
        if len(vol_vals) > 0:
            metrics["volume"] = int(vol_vals[-1]) if vol_vals[-1] > 0 else 0
        if "amount" in df.columns and len(df) > 0:
            amt = df["amount"].iloc[-1]
            metrics["amount"] = float(amt) if not pd.isna(amt) and amt > 0 else 0.0
        if "pct_change" in df.columns and len(df) > 0:
            pc = df["pct_change"].iloc[-1]
            metrics["change_pct"] = float(pc) if not pd.isna(pc) else 0.0
        # 5-day volume ratio: today_vol / avg(prev_5_days_vol)
        if len(vol_vals) >= 6:
            prev5_avg = np.mean(vol_vals[-6:-1])
            if prev5_avg > 0:
                metrics["volume_ratio_5d"] = round(float(vol_vals[-1] / prev5_avg), 2)
    except Exception:
        pass
    return metrics


# ═══════════════════════════════════════════════════════════════════════════════
# Path A: Strict 三才 BP1 scan
# ═══════════════════════════════════════════════════════════════════════════════

def scan_strict_buy(data: dict[str, pd.DataFrame],
                    rust_available: bool = True) -> list[dict]:
    """Scan all stocks for 三才 BP1 buy signals on the latest bar.

    BP1 conditions (all must be true):
      1. price < MA34
      2. low[i] < min(low[i-233:i])  — new 233-bar low
      3. MA144 slope < 0.002 (decelerating)
      4. price < MA233
      5. KDJ J < 20

    Optimized: pure numpy MA slope check (no Rust FFI per symbol).
    """
    results = []
    periods = [34, 144, 233]
    lookback = 233

    for symbol, df in data.items():
        try:
            if len(df) < lookback:
                continue

            close = df["close"].values
            low = df["low"].values
            high = df["high"].values
            price = close[-1]

            # Quick filters first (no computation needed)
            if np.isnan(price) or price <= 0:
                continue

            # Compute MAs (numpy rolling, fast)
            mas = _compute_mas(close, periods)
            ma34 = mas[34][-1]
            ma144 = mas[144][-1]
            ma233 = mas[233][-1]
            if np.isnan(ma34) or np.isnan(ma144) or np.isnan(ma233):
                continue

            # Cheap conditions first
            if not (price < ma34 and price < ma233):
                continue

            # New 233-bar low
            low_233 = low[max(0, len(low) - lookback - 1):-1]  # exclude current bar
            if len(low_233) == 0 or low[-1] >= np.min(low_233):
                continue

            # MA144 decelerating (pure numpy, no Rust FFI)
            slope144 = _ma_slope(mas, 144, 10)
            if slope144 >= 0.002:
                continue

            # Only compute KDJ if all other conditions pass
            _, _, j_vals = _compute_kdj(high, low, close)
            j_val = j_vals[-1]
            if np.isnan(j_val) or j_val >= 20:
                continue

            results.append({
                "symbol": symbol,
                "price": float(price),
                "reason": _shorten_reason("strict"),
                "conditions_met": ["价格<MA34", "创新低", "MA144减速", "价格<MA233", "KDJ超卖"],
                "confidence": 1.0,
                "ma34": float(ma34), "ma233": float(ma233), "kdj_j": float(j_val),
                **_extract_volume_metrics(df),
            })
        except Exception:
            continue

    results.sort(key=lambda r: r["kdj_j"])
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Path A: Strict Reverse 追涨突破 scan
# ═══════════════════════════════════════════════════════════════════════════════

def scan_strict_reverse_buy(data: dict[str, pd.DataFrame],
                             rust_available: bool = True) -> list[dict]:
    """Scan all stocks for 追涨突破 BUY signals — BP1完全反向.

    Conditions (all must be true):
      1. price > MA34
      2. high[i] > max(high[i-233:i])  — new 233-bar high
      3. MA144 slope > 0.005 (accelerating)
      4. price > MA233
      5. KDJ J > 80 (overbought)

    Optimized: pure numpy MA slope check (no Rust FFI per symbol).
    """
    results = []
    periods = [34, 144, 233]
    lookback = 233

    for symbol, df in data.items():
        try:
            if len(df) < lookback:
                continue

            close = df["close"].values
            high = df["high"].values
            low = df["low"].values
            price = close[-1]

            if np.isnan(price) or price <= 0:
                continue

            mas = _compute_mas(close, periods)
            ma34 = mas[34][-1]
            ma144 = mas[144][-1]
            ma233 = mas[233][-1]
            if np.isnan(ma34) or np.isnan(ma144) or np.isnan(ma233):
                continue

            # Cheap conditions first
            if not (price > ma34 and price > ma233):
                continue

            # New 233-bar high
            high_233 = high[max(0, len(high) - lookback - 1):-1]
            if len(high_233) == 0 or high[-1] <= np.max(high_233):
                continue

            # MA144 accelerating (pure numpy)
            slope144 = _ma_slope(mas, 144, 10)
            if slope144 <= 0.005:
                continue

            # KDJ only if all else passes
            _, _, j_vals = _compute_kdj(high, low, close)
            j_val = j_vals[-1]
            if np.isnan(j_val) or j_val <= 80:
                continue

            # ── Risk filters (追涨风控) ──
            risk_tags = []

            # 1. Volume confirmation: today_vol > 20-day avg * 1.2
            vol_arr = df["volume"].values
            avg_vol_20 = float(np.mean(vol_arr[-21:-1])) if len(vol_arr) >= 21 else vol_arr[-1]
            today_vol = float(vol_arr[-1])
            if today_vol < avg_vol_20 * 1.2:
                risk_tags.append("缩量突破")

            # 2. Intraday change limit: exclude if today's change > 7%
            change_pct = 0.0
            if "pct_change" in df.columns:
                change_pct = float(df["pct_change"].iloc[-1]) if not pd.isna(df["pct_change"].iloc[-1]) else 0.0
            if change_pct > 7.0:
                risk_tags.append("追高风险")
                continue  # Hard exclude: already too extended

            # 3. Trend alignment: MA34/MA144/MA233 must be in ascending order (MA34 > MA144 > MA233)
            # For extra confirmation of trend strength
            trend_score = 1.0
            if ma34 > ma144 > ma233:
                trend_score = 1.5  # Boost for clean uptrend
            elif ma34 > ma233:
                trend_score = 0.8  # Partial alignment
            else:
                trend_score = 0.5  # Weak alignment

            # Build confidence from trend_score and volume
            confidence = min(trend_score * 0.7 + min(today_vol / max(avg_vol_20, 1), 2.0) * 0.3, 1.0) if avg_vol_20 > 0 else trend_score

            results.append({
                "symbol": symbol,
                "price": float(price),
                "reason": _shorten_reason("strict_reverse"),
                "conditions_met": ["价格>MA34", "创新高", "MA144加速", "价格>MA233", "KDJ超买"],
                "confidence": round(confidence, 4),
                "ma34": float(ma34), "ma233": float(ma233), "kdj_j": float(j_val),
                "buy_type": "momentum",
                "risk_tags": risk_tags,
                **_extract_volume_metrics(df),
            })
        except Exception:
            continue

    results.sort(key=lambda r: -r["kdj_j"])
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Path A: Simple KDJ crossover scan
# ═══════════════════════════════════════════════════════════════════════════════

def scan_simple_buy(data: dict[str, pd.DataFrame]) -> list[dict]:
    """Scan all stocks for KDJ oversold bounce BUY signals.

    Entry conditions:
      1. KDJ K < 35 (oversold zone)
      2. K crosses above D (K_prev <= D_prev and K_now > D_now)
      3. Price > MA34
      4. K < 25 within last 5 bars (deep oversold confirmation)
    """
    results = []

    for symbol, df in data.items():
        try:
            if len(df) < 60:
                continue

            close = df["close"].values
            high = df["high"].values
            low = df["low"].values

            # Compute MAs
            mas = _compute_mas(close, [34])

            # Compute KDJ
            k_vals, d_vals, _ = _compute_kdj(high, low, close)

            # Last bar
            i = -1
            price = close[i]
            ma34 = mas[34][i]

            k_now = k_vals[i]
            d_now = d_vals[i]
            k_prev = k_vals[i - 1]
            d_prev = d_vals[i - 1]

            if np.isnan(k_now) or np.isnan(d_now) or np.isnan(k_prev) or np.isnan(d_prev):
                continue
            if np.isnan(ma34):
                continue

            # Check conditions
            oversold = k_now < 35
            k_cross_up = k_prev <= d_prev and k_now > d_now
            trend_ok = price > ma34

            # Deep oversold in last 5 bars
            deep_oversold = False
            for lookback in range(1, min(6, len(k_vals) - 1)):
                idx = len(k_vals) - 1 - lookback
                if idx >= 0 and not np.isnan(k_vals[idx]) and k_vals[idx] < 25:
                    deep_oversold = True
                    break

            if oversold and k_cross_up and trend_ok and deep_oversold:
                conditions_met = []
                if oversold: conditions_met.append(f"K<35(K={k_now:.1f})")
                if k_cross_up: conditions_met.append("K上穿D")
                if trend_ok: conditions_met.append(f"价格>MA34({ma34:.2f})")
                if deep_oversold: conditions_met.append("近5日深度超卖(K<25)")

                results.append({
                    "symbol": symbol,
                    "price": float(price),
                    "reason": _shorten_reason("simple"),
                    "conditions_met": conditions_met,
                    "confidence": min(1.0, (35 - k_now) / 20),
                    "kdj_k": float(k_now),
                    "kdj_d": float(d_now),
                    "ma34": float(ma34),
                    **_extract_volume_metrics(df),
                })
        except Exception:
            logger.warning(f"scan_simple: error processing {symbol}", exc_info=True)
            continue

    # Sort by confidence (lower K = stronger oversold signal)
    results.sort(key=lambda r: r["confidence"], reverse=True)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Path B: Single school scan
# ═══════════════════════════════════════════════════════════════════════════════

def _find_latest_data_date(data: dict[str, pd.DataFrame] = None) -> Optional[str]:
    """从已加载的数据中找出最新日期."""
    latest = None
    sources = []

    if data:
        # From bulk-loaded dict
        for df in data.values():
            if len(df) > 0:
                if "date" in df.columns:
                    d = str(df.iloc[-1]["date"])[:10]
                else:
                    d = str(df.index[-1])[:10] if hasattr(df.index[-1], "__str__") else ""
                if d and (latest is None or d > latest):
                    latest = d
    else:
        # Fallback: sample parquet files
        daily_dir = DATA_DIR / "daily"
        if daily_dir.exists():
            import random
            files = list(daily_dir.glob("*.parquet"))
            sample = random.sample(files, min(50, len(files))) if files else []
            for fp in sample:
                try:
                    df = pd.read_parquet(fp)
                    if len(df) > 0 and "date" in df.columns:
                        d = str(df.iloc[-1]["date"])[:10]
                        if latest is None or d > latest:
                            latest = d
                except Exception:
                    pass

    return latest


def _check_single_symbol_school(symbol: str, school_name: str,
                                school_config: dict = None,
                                tail_bars: int = 250,
                                name_map: dict = None,
                                buy_type_filter: str = "",
                                min_price: float = 0,
                                exclude_st: bool = True) -> Optional[dict]:
    """Check a single symbol for a school BUY signal. Called in worker threads.

    Args:
        buy_type_filter: for chan_theory - 'first', 'second', 'third', or '' (all)
        min_price: minimum stock price (skip cheaper stocks)
        exclude_st: exclude ST/delisted stocks
    """
    try:
        from backtest.schools import SCHOOLS

        cls = SCHOOLS.get(school_name)
        if cls is None:
            return None

        df = _load_single_parquet(symbol, tail_bars)
        if df is None or len(df) < 60:
            return None

        price_val = float(df["close"].values[-1])

        # ── Quality filters ──
        if min_price > 0 and price_val < min_price:
            return None

        name = (name_map or {}).get(symbol, "")
        if exclude_st:
            ok, bad_reason = _is_quality_stock(name, price_val)
            if not ok:
                return None

        if not _is_tradable_stock(symbol, name, price_val):
            return None

        school = cls()
        result = school.signal_check(df, school_config)

        signal = result.get("signal")
        if signal == "BUY":
            reason = result.get("reason", "买入信号")
            conditions_met = result.get("conditions_met", [])

            # ── Determine buy type and confidence for chan_theory ──
            confidence = 0.8
            buy_type = ""
            if school_name == "chan_theory":
                buy_type = _classify_chan_buy_type(reason)
                bt_quality = BUY_TYPE_QUALITY.get(buy_type, 0)
                # Filter by buy type if requested
                if buy_type_filter and buy_type != buy_type_filter:
                    return None
                # Assign confidence based on buy type quality
                confidence_map = {"first": 0.95, "second": 0.70, "third": 0.50}
                confidence = confidence_map.get(buy_type, 0.60)

                return {
                "symbol": symbol,
                "price": price_val,
                "reason": _shorten_reason(school_name, reason, buy_type),
                "conditions_met": conditions_met,
                "confidence": confidence,
                "buy_type": buy_type,
                **_extract_volume_metrics(df),
            }
        return None
    except Exception:
        logger.warning(f"School {school_name}: error on {symbol}", exc_info=True)
        return None


def scan_school_buy(symbols: list[str], school_name: str,
                    school_config: dict = None,
                    max_workers: int = 4,
                    timeout: float = 60.0,
                    name_map: dict = None,
                    buy_type_filter: str = "",
                    min_price: float = 0,
                    exclude_st: bool = True) -> dict:
    """Scan all symbols for a single school's BUY signal (Path B)."""
    results = []
    partial = False
    start = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_check_single_symbol_school, sym, school_name,
                          school_config, 250, name_map,
                          buy_type_filter, min_price, exclude_st): sym
            for sym in symbols
        }

        deadline = start + timeout
        for future in as_completed(futures, timeout=timeout):
            elapsed = time.time()
            if elapsed > deadline:
                partial = True
                break
            try:
                result = future.result(timeout=5)
                if result:
                    results.append(result)
            except Exception:
                continue

        # Check if we timed out
        if time.time() > deadline:
            partial = True
            # Cancel remaining futures
            for f in futures:
                f.cancel()

    results.sort(key=lambda r: r.get("confidence", 0), reverse=True)
    return {"results": results, "partial": partial,
            "elapsed_ms": int((time.time() - start) * 1000)}


# ═══════════════════════════════════════════════════════════════════════════════
# Path B: Schools ensemble scan (8流派共识)
# ═══════════════════════════════════════════════════════════════════════════════

def _check_single_symbol_ensemble(symbol: str, school_config: dict = None,
                                  tail_bars: int = 250) -> Optional[dict]:
    """Check a single symbol for ensemble (2+ school) BUY signal."""
    try:
        from backtest.schools import SCHOOLS

        df = _load_single_parquet(symbol, tail_bars)
        if df is None or len(df) < 60:
            return None

        buy_votes = []
        for school_name, cls in SCHOOLS.items():
            try:
                school = cls()
                result = school.signal_check(df, school_config)
                if result.get("signal") == "BUY":
                    buy_votes.append(school_name)
            except Exception:
                continue

        if len(buy_votes) >= 2:
            price_val = df["close"].values[-1]
            return {
                "symbol": symbol,
                "price": float(price_val),
                "reason": _shorten_reason("schools", f"多流派共识买入({len(buy_votes)}/8): {', '.join(buy_votes[:4])}"),
                "conditions_met": buy_votes,
                "confidence": min(1.0, len(buy_votes) / 4),
                **_extract_volume_metrics(df),
            }
        return None
    except Exception:
        return None


def scan_schools_ensemble_buy(symbols: list[str],
                               school_config: dict = None,
                               max_workers: int = 2,
                               timeout: float = 120.0) -> dict:
    """Scan all symbols for ensemble (8-school consensus) BUY signal."""
    results = []
    partial = False
    start = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_check_single_symbol_ensemble, sym, school_config, 250): sym
            for sym in symbols
        }

        deadline = start + timeout
        for future in as_completed(futures, timeout=timeout):
            if time.time() > deadline:
                partial = True
                break
            try:
                result = future.result(timeout=10)
                if result:
                    results.append(result)
            except Exception:
                continue

        if time.time() > deadline:
            partial = True
            for f in futures:
                f.cancel()

    results.sort(key=lambda r: r.get("confidence", 0), reverse=True)
    return {"results": results, "partial": partial,
            "elapsed_ms": int((time.time() - start) * 1000)}


# ═══════════════════════════════════════════════════════════════════════════════
# Per-symbol public check functions — single source of truth for all callers
# (used by monitor.py, backtest engine, and scan_market itself)
# ═══════════════════════════════════════════════════════════════════════════════

def check_single_strict(df: pd.DataFrame) -> Optional[dict]:
    """Check a single stock for 三才 BP1 buy signal on the last bar.

    Returns dict with symbol/price/reason/confidence/buy_type or None.
    This is the canonical BP1 implementation — DO NOT duplicate elsewhere.
    """
    from pathlib import Path as _Path
    if len(df) < 233:
        return None
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    i = len(close) - 1
    price = float(close[i])

    mas = {}
    for p in [34, 144, 233]:
        mas[p] = pd.Series(close).rolling(window=p).mean().values[i]
    ma34, ma144, ma233 = mas[34], mas[144], mas[233]

    k_vals, d_vals, j_vals = _compute_kdj(high, low, close)
    j_val = j_vals[i]
    if np.isnan(ma34) or np.isnan(ma144) or np.isnan(ma233) or np.isnan(j_val):
        return None

    below_ma34 = price < ma34
    new_low = low[i] < np.min(low[max(0, i - 233):i]) if i >= 233 else False
    below_ma233 = price < ma233
    kdj_oversold = j_val < 20

    # MA144 deceleration
    try:
        import sancai_core, json
        ohlcv = {"open": df["open"].tolist(), "high": high.tolist(),
                  "low": low.tolist(), "close": close.tolist(),
                  "volume": df["volume"].tolist()}
        trend_result = json.loads(sancai_core.analyze_trends(json.dumps(ohlcv), "daily"))
        trend_map = {}
        for t in trend_result.get("trends", []):
            trend_map[(t["index"], t["period"])] = t["state"]
        ma144_decel = trend_map.get((i, 144), "") == "decelerating"
    except Exception:
        if i >= 10 and not np.isnan(mas[144]) and mas[144] > 0:
            ma144_10 = pd.Series(close).rolling(144).mean().values[i - 10]
            ma144_decel = (ma144 - ma144_10) / ma144_10 < 0.002 if ma144_10 > 0 else False
        else:
            ma144_decel = False

    if below_ma34 and new_low and ma144_decel and below_ma233 and kdj_oversold:
        return {
            "price": price, "reason": _shorten_reason("strict"),
            "confidence": 1.0, "buy_type": "", "signal": "strict.BUY",
            "direction": "BUY",
            "conditions_met": ["价格<MA34", "新低", "MA144减速", "价格<MA233", "KDJ超卖"],
        }
    return None


def check_single_strict_reverse(df: pd.DataFrame) -> Optional[dict]:
    """BP1完全反向 — 追涨突破 (Momentum Breakout).

    五条件全部BP1取反:
      1. price > MA34  (BP1: price < MA34)
      2. new 233d high (BP1: new 233d low)
      3. MA144 accelerating (BP1: MA144 decelerating)
      4. price > MA233 (BP1: price < MA233)
      5. KDJ J > 80 (BP1: KDJ J < 20)

    逻辑: 当BP1在极端超卖抄底时，追涨突破在极端强势中追趋势延续。
    这不是卖点，是另一类买点——追涨/动量买点。
    """
    if len(df) < 233:
        return None
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    i = len(close) - 1
    price = float(close[i])

    mas = {}
    for p in [34, 144, 233]:
        mas[p] = pd.Series(close).rolling(window=p).mean().values[i]
    ma34, ma144, ma233 = mas[34], mas[144], mas[233]

    k_vals, d_vals, j_vals = _compute_kdj(high, low, close)
    j_val = j_vals[i]
    if np.isnan(ma34) or np.isnan(ma144) or np.isnan(ma233) or np.isnan(j_val):
        return None

    above_ma34 = price > ma34                          # BP1取反
    new_high = high[i] > np.max(high[max(0, i - 233):i]) if i >= 233 else False  # BP1 new_low 取反
    above_ma233 = price > ma233                         # BP1取反
    kdj_overbought = j_val > 80                         # BP1 J<20 取反

    # MA144 acceleration (BP1: deceleration 取反)
    try:
        import sancai_core, json as _json
        ohlcv = {"open": df["open"].tolist(), "high": high.tolist(),
                  "low": low.tolist(), "close": close.tolist(),
                  "volume": df["volume"].tolist()}
        trend_result = _json.loads(sancai_core.analyze_trends(_json.dumps(ohlcv), "daily"))
        trend_map = {}
        for t in trend_result.get("trends", []):
            trend_map[(t["index"], t["period"])] = t["state"]
        ma144_accel = trend_map.get((i, 144), "") == "accelerating"
    except Exception:
        if i >= 10 and not np.isnan(mas[144]) and mas[144] > 0:
            ma144_10 = pd.Series(close).rolling(144).mean().values[i - 10]
            slope = (ma144 - ma144_10) / ma144_10 if ma144_10 > 0 else 0
            ma144_accel = slope > 0.005  # > 0.5% growth (BP1: < 0.2%)
        else:
            ma144_accel = False

    if above_ma34 and new_high and ma144_accel and above_ma233 and kdj_overbought:
        return {
            "price": price,
            "reason": _shorten_reason("strict_reverse"),
            "confidence": 1.0, "buy_type": "", "signal": "strict_reverse.BUY",
            "direction": "BUY",
            "conditions_met": ["价格>MA34", "创新高", "MA144加速", "价格>MA233", "KDJ超买"],
        }
    return None


def check_single_simple(df: pd.DataFrame) -> Optional[dict]:
    """Check a single stock for KDJ oversold bounce buy signal.

    Returns dict or None. Canonical implementation — DO NOT duplicate.
    """
    if len(df) < 60:
        return None
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    i = len(close) - 1
    price = float(close[i])

    ma34 = pd.Series(close).rolling(window=34).mean().values[i]
    k_vals, d_vals, _ = _compute_kdj(high, low, close)

    k_now = k_vals[i]; d_now = d_vals[i]
    k_prev = k_vals[i - 1]; d_prev = d_vals[i - 1]
    if np.isnan(k_now) or np.isnan(d_now) or np.isnan(k_prev) or np.isnan(d_prev):
        return None
    if np.isnan(ma34):
        return None

    oversold = k_now < 35
    k_cross_up = k_prev <= d_prev and k_now > d_now
    trend_ok = price > ma34
    deep_oversold = any(not np.isnan(k_vals[j]) and k_vals[j] < 25
                       for j in range(max(0, i - 5), i))

    if oversold and k_cross_up and trend_ok and deep_oversold:
        return {
            "price": price,
            "reason": _shorten_reason("simple"),
            "confidence": min(1.0, (35 - k_now) / 20),
            "buy_type": "", "signal": "simple.BUY", "direction": "BUY",
            "conditions_met": [f"K<35(K={k_now:.1f})", "K上穿D", f"价格>MA34({ma34:.2f})", "近5日深度超卖"],
        }
    return None


def check_single_school(symbol: str, df: pd.DataFrame, school_name: str,
                        school_config: dict = None) -> Optional[dict]:
    """Check a single stock for a school's BUY/SELL signal (public version).

    Returns dict with signal/direction/price/reason/confidence/conditions_met or None.
    """
    try:
        from backtest.schools import SCHOOLS
        cls = SCHOOLS.get(school_name)
        if cls is None:
            return None
        school = cls()
        result = school.signal_check(df, school_config)
        sig = result.get("signal")
        if sig in ("BUY", "SELL"):
            direction = "BUY" if sig == "BUY" else "SELL"
            reason = result.get("reason", f"{school_name} {sig}")
            buy_type = _classify_chan_buy_type(reason) if school_name == "chan_theory" else ""
            conf = 0.95 if direction == "BUY" else 0.85
            # For chan_theory, use buy-type-based confidence
            if school_name == "chan_theory" and direction == "BUY":
                conf = {"first": 0.95, "second": 0.70, "third": 0.50}.get(buy_type, 0.60)
            return {
                "price": float(result.get("price", df["close"].values[-1])),
                "reason": _shorten_reason(school_name, reason, buy_type),
                "confidence": conf, "buy_type": buy_type,
                "signal": f"{school_name}.{sig}", "direction": direction,
                "conditions_met": result.get("conditions_met", []),
                **_extract_volume_metrics(df),
            }
        return None
    except Exception:
        logger.warning(f"check_single_school: error on {symbol}/{school_name}", exc_info=True)
        return None


def check_single_schools_ensemble(symbol: str, df: pd.DataFrame,
                                   school_config: dict = None) -> Optional[dict]:
    """Check for ensemble (8-school consensus) BUY/SELL signal."""
    try:
        from backtest.schools import SCHOOLS
        buy_votes = []; sell_votes = []
        for sname, scls in SCHOOLS.items():
            try:
                school = scls()
                r = school.signal_check(df, school_config)
                if r.get("signal") == "BUY":
                    buy_votes.append(sname)
                elif r.get("signal") == "SELL":
                    sell_votes.append(sname)
            except Exception:
                continue

        price = float(df["close"].values[-1])
        if len(buy_votes) >= 2:
            return {"price": price, "signal": f"schools.BUY({len(buy_votes)}/8)",
                    "direction": "BUY", "reason": _shorten_reason("schools", f"多流派共识买入({len(buy_votes)}/8): {', '.join(buy_votes[:4])}"),
                    "confidence": min(1.0, len(buy_votes) / 4), "buy_type": "",
                    "conditions_met": buy_votes,
                    **_extract_volume_metrics(df)}
        if len(sell_votes) >= 1:
            return {"price": price, "signal": f"schools.SELL({len(sell_votes)}/8)",
                    "direction": "SELL", "reason": f"流派卖出信号({len(sell_votes)}/8): {', '.join(sell_votes[:3])}",
                    "confidence": 0.8, "buy_type": "",
                    "conditions_met": sell_votes}
        return None
    except Exception:
        return None


# Mapping of mode → (check_function, needs_school_config)
_SINGLE_CHECK_DISPATCH = {
    "strict": (check_single_strict, False),
    "strict_reverse": (check_single_strict_reverse, False),
    "simple": (check_single_simple, False),
}


def check_buy_signal(df: pd.DataFrame, mode: str,
                     school_config: dict = None) -> Optional[dict]:
    """Single entry point: check if the last bar triggers a strategy BUY/SELL signal.

    This is THE canonical strategy signal check. All callers (monitor, scanner,
    backtest engine) should route through this or the underlying per-mode functions.

    Args:
        df: OHLCV DataFrame with at least [open, high, low, close, volume] columns
        mode: strategy mode (strict, simple, chan_theory, ict, schools, ...)
        school_config: optional school parameter overrides

    Returns:
        dict with price/reason/confidence/direction/buy_type/signal/conditions_met,
        or None if no signal.
    """
    if mode in _SINGLE_CHECK_DISPATCH:
        fn, _ = _SINGLE_CHECK_DISPATCH[mode]
        return fn(df)

    if mode in _SINGLE_SCHOOL_MODES:
        return check_single_school("", df, mode, school_config)

    if mode == "schools":
        return check_single_schools_ensemble("", df, school_config)

    if mode.startswith("user_"):
        return _check_single_user_strategy("", df, mode)

    return None


def _check_single_user_strategy(symbol: str, df: pd.DataFrame,
                                mode: str) -> Optional[dict]:
    """Check user strategy signal."""
    try:
        from backtest.strategies.registry import get_strategy
        from backtest.strategies.interface import BarContext

        cls = get_strategy(mode)
        if cls is None:
            return None
        strat = cls()
        try:
            df_ind = strat.populate_indicators(df.copy())
        except Exception:
            df_ind = df

        i = len(df_ind) - 1
        price = float(df_ind["close"].values[i])
        ctx = BarContext(symbol=symbol, index=i, price=price,
                         date_val=df_ind.iloc[i].get("date", i),
                         date_str=str(df_ind.iloc[i].get("date", ""))[:10],
                         in_position=False)
        signal = strat.populate_entry_signals(ctx)
        if signal and signal.type.value == "buy":
            return {"price": price, "signal": f"{mode}.BUY", "direction": "BUY",
                    "reason": signal.reason or f"{mode} 买入信号",
                    "confidence": signal.confidence, "buy_type": "", "conditions_met": []}
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

# Modes that use Path A (lightweight, fully in-process)
_LIGHTWEIGHT_MODES = {"strict", "strict_reverse", "simple"}

# Modes that use Path B (school-based, per-symbol)
_SINGLE_SCHOOL_MODES = {
    "chan_theory", "ict", "price_action", "wyckoff",
    "morphology", "gann", "wave_theory", "dow_theory",
}


def scan_market(mode: str = "strict",
                data_dir: Path = None,
                school_config: dict = None,
                max_workers: int = 4,
                tail_bars: int = 300,
                timeout_schools: float = 60.0,
                timeout_ensemble: float = 120.0,
                buy_type_filter: str = "",
                min_price: float = 0,
                exclude_st: bool = True,
                cancel_check=None,
                live_bars: dict = None) -> dict:
    """Main entry: scan entire market for strategy BUY signals.

    Args:
        mode: Strategy mode (strict, simple, schools, chan_theory, ict, ...)
        data_dir: Override data directory
        school_config: Dict of school parameter overrides
        max_workers: Thread pool size for school-based modes
        tail_bars: Number of recent bars to load per stock
        timeout_schools: Max seconds for single-school scan
        timeout_ensemble: Max seconds for ensemble scan
        buy_type_filter: For chan_theory - 'first', 'second', 'third', or '' (all)
        min_price: Minimum stock price filter (>0 to enable)
        exclude_st: Exclude ST/delisted stocks (default True)

    Returns:
        dict with keys: mode, scanned, matched, results, elapsed_ms, [partial]
    """
    start = time.time()
    symbols = get_available_symbols(data_dir)

    if not symbols:
        return {"mode": mode, "scanned": 0, "matched": 0, "results": [],
                "elapsed_ms": 0, "error": "No stock data found"}

    name_map = _get_name_map()
    results = []
    partial = False

    logger.info(f"Strategy scan start: mode={mode}, symbols={len(symbols)}")

    try:
        if mode in _LIGHTWEIGHT_MODES:
            # ── Path A: Lightweight numeric scan ──
            data = _bulk_load_via_duckdb(symbols, tail_bars, live_bars=live_bars)

            if not data:
                # Fallback: per-symbol parquet loading
                data = {}
                for sym in symbols:
                    df = _load_single_parquet(sym, tail_bars)
                    if df is not None:
                        data[sym] = df

            if not data:
                return {"mode": mode, "scanned": len(symbols), "matched": 0,
                        "results": [], "elapsed_ms": int((time.time() - start) * 1000),
                        "error": "Failed to load any data"}

            # Check Rust core availability
            try:
                import sancai_core
                rust_ok = True
            except ImportError:
                rust_ok = False

            if mode == "strict":
                results = scan_strict_buy(data, rust_available=rust_ok)
            elif mode == "strict_reverse":
                results = scan_strict_reverse_buy(data, rust_available=rust_ok)
            elif mode == "simple":
                results = scan_simple_buy(data)

            # Apply quality filters to lightweight results
            results = [
                r for r in results
                if (not min_price or r.get("price", 0) >= min_price)
                and (not exclude_st or _is_quality_stock(name_map.get(r["symbol"], ""), r.get("price", 0))[0])
                and _is_tradable_stock(r["symbol"], name_map.get(r["symbol"], ""), r.get("price", 0))
            ]

        elif mode == "schools":
            # ── Path B: Ensemble scan ──
            scan_result = scan_schools_ensemble_buy(
                symbols, school_config,
                max_workers=max_workers,
                timeout=timeout_ensemble
            )
            results = scan_result["results"]
            partial = scan_result.get("partial", False)

        elif mode in _SINGLE_SCHOOL_MODES:
            # ── Path B: Single school scan ──
            scan_result = scan_school_buy(
                symbols, mode, school_config,
                max_workers=max_workers,
                timeout=timeout_schools,
                name_map=name_map,
                buy_type_filter=buy_type_filter,
                min_price=min_price,
                exclude_st=exclude_st,
            )
            results = scan_result["results"]
            partial = scan_result.get("partial", False)

        elif mode.startswith("user_"):
            # ── Path C: User strategy scan ──
            results, partial = _scan_user_strategy(symbols, mode, tail_bars)

        else:
            return {"mode": mode, "scanned": 0, "matched": 0,
                    "results": [], "elapsed_ms": int((time.time() - start) * 1000),
                    "error": f"Unknown mode: {mode}"}

    except Exception as e:
        logger.error(f"Strategy scan failed for mode={mode}: {e}", exc_info=True)
        return {"mode": mode, "scanned": len(symbols), "matched": 0,
                "results": [], "elapsed_ms": int((time.time() - start) * 1000),
                "error": str(e)}

    # ── Cancel check: if user cancelled, discard results ──
    if cancel_check and cancel_check():
        logger.info(f"Strategy scan cancelled by user: mode={mode}")
        return {"mode": mode, "scanned": len(symbols), "matched": 0,
                "results": [], "elapsed_ms": int((time.time() - start) * 1000),
                "cancelled": True}

    # Attach names
    for r in results:
        r["name"] = name_map.get(r["symbol"], r["symbol"])

    # ── News enrichment: cross-reference with recent news ──
    try:
        _enrich_results_with_news(results)
    except Exception:
        pass

    elapsed_ms = int((time.time() - start) * 1000)

    # ── 数据新鲜度检查 ──
    latest_data_date = _find_latest_data_date(data if mode in _LIGHTWEIGHT_MODES else None)
    staleness_days = 0
    if latest_data_date:
        try:
            staleness_days = (pd.Timestamp.now() - pd.Timestamp(latest_data_date)).days
        except Exception:
            pass

    result_dict = {
        "mode": mode,
        "scanned": len(symbols),
        "matched": len(results),
        "results": results,
        "elapsed_ms": elapsed_ms,
        "latest_data_date": latest_data_date or "",
        "staleness_days": staleness_days,
    }
    if partial:
        result_dict["partial"] = True

    logger.info(f"Strategy scan done: mode={mode}, scanned={len(symbols)}, "
                f"matched={len(results)}, elapsed={elapsed_ms}ms"
                + (" (partial)" if partial else ""))

    return result_dict


# ═══════════════════════════════════════════════════════════════════════════════
# Path C: User strategy scan
# ═══════════════════════════════════════════════════════════════════════════════

def _scan_user_strategy(symbols: list[str], mode: str,
                        tail_bars: int = None) -> tuple[list[dict], bool]:
    """Scan all symbols using a user-registered IStrategy.

    Returns (results, partial).
    """
    # User strategies often need long history (wave patterns, etc.) — use 800 bars minimum
    if tail_bars is None or tail_bars < 500:
        tail_bars = 800
    from backtest.strategies.registry import get_strategy
    from backtest.strategies.interface import BarContext

    cls = get_strategy(mode)
    if cls is None:
        logger.warning(f"User strategy '{mode}' not found in registry")
        return [], False

    results = []
    strat = cls()

    for sym in symbols:
        try:
            df = _load_single_parquet(sym, tail_bars)
            if df is None or len(df) < 60:
                continue

            # Pre-compute indicators
            try:
                df_ind = strat.populate_indicators(df.copy())
            except Exception:
                df_ind = df

            # Build BarContext for last bar
            i = len(df_ind) - 1
            price = float(df_ind["close"].values[i])
            date_val = df_ind.iloc[i].get("date", df_ind.iloc[i].name)

            ctx = BarContext(
                symbol=sym,
                index=i,
                price=price,
                date_val=date_val,
                date_str=str(date_val)[:10],
                in_position=False,
            )

            # Populate MAs from indicator columns if available
            for col in df_ind.columns:
                if col.startswith("ma") and col[2:].isdigit():
                    p = int(col[2:])
                    val = df_ind[col].values[i]
                    if not np.isnan(val):
                        ctx.mas[p] = float(val)

            # Simple trend
            ma34 = ctx.mas.get(34, price)
            if price > ma34 * 1.005:
                ctx.trend = "up"
            elif price < ma34 * 0.995:
                ctx.trend = "down"
            else:
                ctx.trend = "neutral"

            # Check entry signal
            signal = strat.populate_entry_signals(ctx)
            if signal and signal.type.value == "buy":
                results.append({
                    "symbol": sym,
                    "price": price,
                    "reason": signal.reason or f"{mode} 买入信号",
                    "conditions_met": [],
                    "confidence": signal.confidence,
                })
        except Exception:
            logger.warning(f"User strategy {mode}: error on {sym}", exc_info=True)
            continue

    results.sort(key=lambda r: r.get("confidence", 0), reverse=True)
    return results, False
