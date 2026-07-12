"""Sancai multi-layer data endpoints.

Tiandao (macro timing), Didao (stock selection), Rendao (execution).
Each tier exposes: market, research, news, fundamental, announcements + unified timeline.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from .sancai_gua import assess_index, assess_stock, assess_execution, BAGUA, QIAN_YAO, KUN_YAO

router = APIRouter()

# Profile-based factor weights
PROFILE_WEIGHTS = {
    "conservative": {"fundamental": 0.6, "technical": 0.2, "sentiment": 0.2},
    "balanced": {"fundamental": 0.4, "technical": 0.4, "sentiment": 0.2},
    "aggressive": {"fundamental": 0.15, "technical": 0.5, "sentiment": 0.35},
}

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"
CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"

# In-memory cache: {key: {ts: datetime, data: dict}}
_CACHE: dict = {}
_CACHE_TTL = 300  # 5 minutes default


def _cached(key: str, ttl: int = None):
    """Check if cached value exists and is fresh."""
    ttl = ttl or _CACHE_TTL
    entry = _CACHE.get(key)
    if entry and (datetime.now() - entry["ts"]).seconds < ttl:
        return entry["data"]
    return None


def _cache_set(key: str, data: dict):
    _CACHE[key] = {"ts": datetime.now(), "data": data}


def _safe_akshare(fn, *args, **kwargs):
    """Call an akshare function safely, returning (df_or_None, error_str)."""
    try:
        import akshare as ak
        result = fn(ak, *args, **kwargs)
        if result is None:
            return None, "no data returned"
        if isinstance(result, pd.DataFrame) and result.empty:
            return None, "empty DataFrame"
        return result, None
    except Exception as e:
        return None, str(e)


def _symbol_prefix(symbol: str) -> str:
    """Convert 000001 → sz000001, 600519 → sh600519."""
    return ('sh' if symbol.startswith(('6', '9')) else 'sz') + symbol


def _market_code(symbol: str) -> str:
    """Market code: 0/3 → sz, 6 → sh."""
    return 'sz' if symbol.startswith(('0', '3')) else 'sh'


def _load_universe():
    import yaml
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("universe", [])


def _ok(data, source="akshare"):
    return {"status": "ok", "data": data, "ts": datetime.now().isoformat(), "source": source}


def _err(msg, source="akshare"):
    return {"status": "error", "data": None, "ts": datetime.now().isoformat(), "message": msg, "source": source}


# ═══════════════════════════════════════════
# 天道 (Tiandao) — Macro Market Timing
# ═══════════════════════════════════════════

TIANDAO_INDICES = {
    # ── A股五大指数 ──
    "sh000001":  {"name": "上证指数",    "group": "A股指数", "akshare_fn": "stock_zh_index_daily"},
    "sz399001":  {"name": "深证成指",    "group": "A股指数", "akshare_fn": "stock_zh_index_daily"},
    "sz399006":  {"name": "创业板指",    "group": "A股指数", "akshare_fn": "stock_zh_index_daily"},
    "sh000688":  {"name": "科创50",     "group": "A股指数", "akshare_fn": "stock_zh_index_daily"},
    "bj899050":  {"name": "北证50",     "group": "A股指数", "akshare_fn": "stock_zh_index_daily"},
    # ── 亚太其他重要指数 ──
    "hkHSI":     {"name": "恒生指数",    "group": "亚太指数", "akshare_fn": "hk_index_daily"},
    "jpN225":    {"name": "日经225",    "group": "亚太指数", "akshare_fn": "global_index"},
    "krKOSPI":   {"name": "韩国KOSPI",  "group": "亚太指数", "akshare_fn": "global_index"},
    # ── 汇率 ──
    "fxUSDCNH":  {"name": "离岸人民币",  "group": "汇率",   "akshare_fn": "fx_index"},
    # ── 期货品种 ──
    "fGC":       {"name": "黄金(COMEX)", "group": "期货商品", "akshare_fn": "futures_foreign"},
    "fCL":       {"name": "原油(WTI)",   "group": "期货商品", "akshare_fn": "futures_foreign"},
    "fDX":       {"name": "美元指数",    "group": "期货商品", "akshare_fn": "futures_foreign"},
    "fA50":      {"name": "富时A50",    "group": "期货商品", "akshare_fn": "futures_foreign"},
    "fTA":       {"name": "PTA(化工)",   "group": "期货商品", "akshare_fn": "futures_domestic"},
    "fJM":       {"name": "焦煤",       "group": "期货商品", "akshare_fn": "futures_domestic"},
    "fA":        {"name": "豆一(农产品)","group": "期货商品", "akshare_fn": "futures_domestic"},
    # ── 美股三大指数 ──
    "usDJI":     {"name": "道琼斯",     "group": "美股指数", "akshare_fn": "global_index"},
    "usSPX":     {"name": "标普500",    "group": "美股指数", "akshare_fn": "global_index"},
    "usIXIC":    {"name": "纳斯达克",   "group": "美股指数", "akshare_fn": "global_index"},
}

# Index code to akshare symbol mapping for different fetch functions
INDEX_AKSHARE_SYMBOLS = {
    "sh000001": "sh000001", "sz399001": "sz399001", "sz399006": "sz399006",
    "sh000688": "sh000688", "bj899050": "bj899050",
    "hkHSI": "HSI", "jpN225": "N225", "krKOSPI": "KOSPI",
    "fxUSDCNH": "USDCNH",
    "fGC": "GC", "fCL": "CL", "fDX": "DX", "fA50": "XINA50",
    "fTA": "TA", "fJM": "JM", "fA": "A",
    "usDJI": "DJI", "usSPX": "SPX", "usIXIC": "IXIC",
}

# Whether an index uses E-Market (Eastmoney) or Sina source
INDEX_SOURCE = {
    "sh000001": "sina", "sz399001": "sina", "sz399006": "sina",
    "sh000688": "sina", "bj899050": "em",
    "hkHSI": "em", "jpN225": "em", "krKOSPI": "em",
    "fxUSDCNH": "sina",
    "fGC": "sina", "fCL": "sina", "fDX": "sina", "fA50": "sina",
    "fTA": "sina", "fJM": "sina", "fA": "sina",
    "usDJI": "em", "usSPX": "em", "usIXIC": "em",
}


def _fetch_index_data(ak, code: str, days: int):
    """Fetch index data using the appropriate akshare function."""
    ak_symbol = INDEX_AKSHARE_SYMBOLS.get(code, code)
    fn_type = TIANDAO_INDICES[code]["akshare_fn"]

    if fn_type == "stock_zh_index_daily":
        return ak.stock_zh_index_daily(symbol=ak_symbol)
    elif fn_type == "hk_index_daily":
        # Eastmoney HK index
        return ak.stock_hk_index_daily_em(symbol=ak_symbol)
    elif fn_type == "global_index":
        # Use Eastmoney global index
        return ak.index_global_hist_em(symbol=ak_symbol)
    elif fn_type == "futures_foreign":
        # Sina foreign futures
        return ak.futures_foreign_hist(symbol=ak_symbol)
    elif fn_type == "futures_domestic":
        # Sina domestic futures
        return ak.futures_main_sina(symbol=ak_symbol)
    elif fn_type == "fx_index":
        # Currency index via Eastmoney
        return ak.currency_hist_em(symbol=ak_symbol)
    else:
        return ak.stock_zh_index_daily(symbol=ak_symbol)


def _normalize_index_df(df, code: str, days: int):
    """Normalize different akshare DataFrame formats to standard OHLCV columns."""
    if df is None or df.empty:
        return None

    cols = [c.lower() for c in df.columns]

    # Try to find date, open, close, high, low, volume columns
    date_col = None
    open_col = None; close_col = None
    high_col = None; low_col = None
    vol_col = None

    for c in df.columns:
        cl = c.lower()
        if cl in ("date", "日期", "time", "trade_date", "day"):
            date_col = c
        elif cl in ("open", "开盘", "开盘价"):
            open_col = c
        elif cl in ("close", "收盘", "收盘价", "最新价"):
            close_col = c
        elif cl in ("high", "最高", "最高价"):
            high_col = c
        elif cl in ("low", "最低", "最低价"):
            low_col = c
        elif cl in ("volume", "vol", "成交量", "成交额"):
            vol_col = c

    if date_col is None:
        return None

    records = []
    for _, row in df.sort_values(date_col).tail(days).iterrows():
        try:
            records.append({
                "date": str(row[date_col])[:10],
                "open": float(row[open_col]) if open_col and pd.notna(row.get(open_col)) else 0,
                "close": float(row[close_col]) if close_col and pd.notna(row.get(close_col)) else 0,
                "high": float(row[high_col]) if high_col and pd.notna(row.get(high_col)) else 0,
                "low": float(row[low_col]) if low_col and pd.notna(row.get(low_col)) else 0,
                "volume": float(row[vol_col]) if vol_col and pd.notna(row.get(vol_col)) else 0,
            })
        except (ValueError, TypeError, KeyError):
            continue

    return records if records else None


def _process_one_market_index(code, info, days):
    """Process a single market index (blocking). Runs in thread pool for parallelism."""
    name = info["name"]
    group = info["group"]
    df, err = _safe_akshare(lambda ak_inner, c=code, d=days: _fetch_index_data(ak_inner, c, d))
    if err or df is None:
        return code, {"name": name, "group": group, "data": [], "error": err or "no data"}

    records = _normalize_index_df(df, code, days)
    if records is None:
        return code, {"name": name, "group": group, "data": [], "error": "failed to normalize"}

    latest_close = records[-1]["close"] if records else 0
    prev_close = records[-2]["close"] if len(records) > 1 else latest_close
    change_pct = round((latest_close / prev_close - 1) * 100, 2) if prev_close else 0

    return code, {
        "name": name, "group": group,
        "data": records, "latest": latest_close, "change_pct": change_pct,
    }


def _process_one_index_gua(code, info, days):
    """Process a single index for gua/hexagram assessment (blocking)."""
    name = info["name"]
    group = info["group"]

    df, err = _safe_akshare(lambda ak_inner, c=code, d=days: _fetch_index_data(ak_inner, c, d))
    if err or df is None:
        return {
            "code": code, "name": name, "group": group,
            "error": err or "no data",
            "alignment": "数据不可用",
            "hexagram": "—", "symbol": "—", "yao_ci": "—",
            "advice": "—", "direction": "—",
            "_count": "cross",
        }

    records = _normalize_index_df(df, code, days)
    if records is None or len(records) < 60:
        return {
            "code": code, "name": name, "group": group,
            "error": "insufficient data",
            "alignment": "数据不足",
            "hexagram": "—", "symbol": "—", "yao_ci": "—",
            "advice": "—", "direction": "—",
            "_count": "cross",
        }

    close_arr = np.array([r["close"] for r in records], dtype=float)
    latest_price = close_arr[-1]
    gua = assess_index(close_arr, name, latest_price)

    alignment = gua.get("alignment", "震荡交织")
    if alignment == "多头排列":
        count_key = "multi"
    elif alignment == "空头排列":
        count_key = "bear"
    else:
        count_key = "cross"

    return {
        "code": code, "name": name, "group": group,
        "alignment": alignment,
        "price": gua.get("price"),
        "change_pct": gua.get("change_pct", 0),
        "hexagram": gua["hexagram"], "symbol": gua["symbol"],
        "nature": gua.get("nature", "—"), "meaning": gua.get("meaning", "—"),
        "color": gua.get("color", "#8b949e"),
        "yao_name": gua.get("yao_name", "—"),
        "yao_ci": gua["yao_ci"], "yao_meaning": gua.get("yao_meaning", "—"),
        "advice": gua.get("advice", gua.get("suggestion", "—")),
        "direction": gua["direction"],
        "confidence": gua.get("confidence", 0),
        "detail": gua.get("detail", ""),
        "ma34": gua.get("ma34"), "ma144": gua.get("ma144"), "ma233": gua.get("ma233"),
        "_count": count_key,
    }



@router.get("/tiandao/market")
async def tiandao_market(days: int = Query(60, ge=1, le=365)):
    """天道行情层: 多市场指数日线数据 (A股5大指数+亚太+期货)."""
    cache_key = f"tiandao_market_{days}"
    cached = _cached(cache_key, ttl=120)
    if cached:
        return _ok(cached, "cache")

    tasks = [asyncio.to_thread(_process_one_market_index, code, info, days)
             for code, info in TIANDAO_INDICES.items()]
    results_list = await asyncio.gather(*tasks)
    result = {code: data for code, data in results_list}

    _cache_set(cache_key, result)
    return _ok(result)


@router.get("/tiandao/indices")
async def tiandao_indices(days: int = Query(90, ge=60, le=365)):
    """天道指数监控: 所有指数的MA排列(34/144/233) + 爻卦象.

    Returns each index with:
      - MA alignment (多头排列/空头排列/震荡交织)
      - 卦象 (hexagram) + 爻辞 (yao statement)
      - 操作建议 (advice)
      - 运行状态 (running state from 五爻 imagery)
    """
    cache_key = f"tiandao_indices_{days}"
    cached = _cached(cache_key, ttl=120)
    if cached:
        return _ok(cached, "cache")

    # Fetch all indices in parallel via thread pool
    tasks = [asyncio.to_thread(_process_one_index_gua, code, info, days)
             for code, info in TIANDAO_INDICES.items()]
    raw_results = await asyncio.gather(*tasks)

    # Build final result list
    indices_result = []
    multi_count = 0
    bear_count = 0
    cross_count = 0

    for item in raw_results:
        count_key = item.pop("_count", "cross")
        if count_key == "multi":
            multi_count += 1
        elif count_key == "bear":
            bear_count += 1
        else:
            cross_count += 1
        indices_result.append(item)

    # Overall tiandao assessment based on index breadth
    total = len(indices_result)
    if multi_count > total * 0.5:
        tiandao_overall = "吉"
        overall_gua = BAGUA["qian"]
        overall_yao = QIAN_YAO["九五"]
        overall_meaning = "多数指数多头排列，天道大势向上"
    elif bear_count > total * 0.5:
        tiandao_overall = "凶"
        overall_gua = BAGUA["kun"]
        overall_yao = KUN_YAO["上六"]
        overall_meaning = "多数指数空头排列，天道大势向下"
    elif multi_count > bear_count:
        tiandao_overall = "平"
        overall_gua = BAGUA["zhen"]
        overall_yao = QIAN_YAO["九四"]
        overall_meaning = "多头略占优势，趋势有待确认"
    else:
        tiandao_overall = "平"
        overall_gua = BAGUA["gen"]
        overall_yao = KUN_YAO["六三"]
        overall_meaning = "空头略占优势，宜观望等待"

    result = {
        "indices": indices_result,
        "total": total,
        "multi_count": multi_count,
        "bear_count": bear_count,
        "cross_count": cross_count,
        "overall": {
            "assessment": tiandao_overall,
            "hexagram": overall_gua["name"],
            "symbol": overall_gua["symbol"],
            "yao_ci": overall_yao["yao_ci"],
            "meaning": overall_meaning,
            "advice": overall_yao["advice"],
        },
    }

    _cache_set(cache_key, result)
    return _ok(result)


@router.get("/tiandao/fundamental")
async def tiandao_fundamental():
    """天道基础数据层: 全市场PE/PB分位数."""
    cache_key = "tiandao_fundamental"
    cached = _cached(cache_key, ttl=600)  # 10min — PE changes slowly
    if cached:
        return _ok(cached, "cache")

    def _pe_for(index_name):
        df, err = _safe_akshare(lambda ak, n=index_name: ak.stock_index_pe_lg(symbol=n))
        if err:
            return None
        latest = df.iloc[-1]
        return {
            "date": str(latest["日期"])[:10],
            "pe_weighted": float(latest["加权动态市盈率"]) if "加权动态市盈率" in df.columns else None,
            "pe_median": float(latest["动态市盈率"]) if "动态市盈率" in df.columns else None,
            "pe_pct": float(latest["动态市盈率分位数"]) if "动态市盈率分位数" in df.columns else None,
            "pb_weighted": float(latest["加权动态市净率"]) if "加权动态市净率" in df.columns else None,
            "pb_median": float(latest["动态市净率"]) if "动态市净率" in df.columns else None,
            "pb_pct": float(latest["动态市净率分位数"]) if "动态市净率分位数" in df.columns else None,
        }

    result = {
        "沪深300": _pe_for("沪深300"),
        "上证50": _pe_for("上证50"),
        "中证500": _pe_for("中证500"),
    }
    _cache_set(cache_key, result)
    return _ok(result)


@router.get("/tiandao/research")
async def tiandao_research(days: int = Query(60, ge=1, le=365)):
    """天道研报层: 宏观策略研报."""
    cache_key = f"tiandao_research_{days}"
    cached = _cached(cache_key, ttl=1800)
    if cached:
        return _ok(cached, "cache")

    df, err = _safe_akshare(lambda ak: ak.stock_research_report_em(symbol="000001"))
    if err:
        return _err(err)

    # Filter: recent reports with macro/strategy keywords in title
    cutoff = datetime.now() - timedelta(days=days)
    macro_keywords = ["宏观", "策略", "市场", "经济", "政策", "行业", "季度", "年度", "A股"]
    col_map = {c: c for c in df.columns}
    title_col = df.columns[2] if len(df.columns) > 2 else None  # 研究报告名称
    org_col = df.columns[3] if len(df.columns) > 3 else None     # 研究机构名称
    rating_col = df.columns[4] if len(df.columns) > 4 else None  # 评级
    date_col = df.columns[5] if len(df.columns) > 5 else None    # 日期

    reports = []
    for _, row in df.iterrows():
        title = str(row.get(title_col, "")) if title_col else ""
        if not any(kw in title for kw in macro_keywords):
            continue
        try:
            d = pd.Timestamp(row[date_col]) if date_col else None
            if d and d < cutoff:
                continue
        except Exception:
            pass
        reports.append({
            "title": title,
            "org": str(row.get(org_col, "")) if org_col else "",
            "rating": str(row.get(rating_col, "")) if rating_col else "",
            "date": str(row.get(date_col, ""))[:10] if date_col else "",
        })
        if len(reports) >= 30:
            break

    result = {"reports": reports, "count": len(reports)}
    _cache_set(cache_key, result)
    return _ok(result)


@router.get("/tiandao/sectors")
async def tiandao_sectors():
    """天道板块层: 行业涨跌热力图数据."""
    cache_key = "tiandao_sectors"
    cached = _cached(cache_key, ttl=300)
    if cached:
        return _ok(cached, "cache")

    df, err = _safe_akshare(lambda ak: ak.stock_board_industry_spot_em())
    if err:
        return _err(err)

    # Parse item/value pairs into a dict
    raw = {}
    for _, row in df.iterrows():
        raw[str(row["item"])] = row["value"]

    result = {"raw": raw, "note": "行业板块实时数据 (item/value format from Eastmoney)"}
    _cache_set(cache_key, result)
    return _ok(result)


@router.get("/tiandao/timeline")
async def tiandao_timeline(days: int = Query(60, ge=1, le=365)):
    """天道统一时间轴: 聚合行情+研报+基本面事件."""
    cache_key = f"tiandao_timeline_{days}"
    cached = _cached(cache_key, ttl=120)
    if cached:
        return _ok(cached, "cache")

    events = []
    price_data = None

    # Fire all sub-fetches in parallel
    market_future = asyncio.ensure_future(tiandao_market(days=days))
    fund_future = asyncio.ensure_future(tiandao_fundamental())
    research_future = asyncio.ensure_future(tiandao_research(days=days))

    # 1. Market data (price line)
    market_result = await market_future
    if market_result["status"] == "ok":
        hs300 = market_result["data"].get("sh000300", {})
        price_data = {
            "dates": [d["date"] for d in hs300.get("data", [])],
            "close": [d["close"] for d in hs300.get("data", [])],
            "name": "沪深300",
        }
        # Add index change events
        data = hs300.get("data", [])
        for i, d in enumerate(data):
            if i > 0 and abs(d["change_pct"] if "change_pct" in d else 0) > 1.5:
                events.append({
                    "date": d["date"],
                    "layer": "market",
                    "title": f"沪深300 {'涨' if (d['close'] > data[i-1]['close']) else '跌'} {abs(d.get('pct_change', (d['close']/data[i-1]['close']-1)*100)):.1f}%",
                    "detail": f"收盘 {d['close']:.2f}, 成交量 {d.get('volume', 0):.0f}",
                    "importance": 2,
                })

    # 2. Fundamental events (PE thresholds)
    try:
        fund_resp = await fund_future
        if fund_resp["status"] == "ok":
            hs300_pe = fund_resp["data"].get("沪深300", {}) or {}
            pe_pct = hs300_pe.get("pe_pct")
            if pe_pct is not None:
                if pe_pct < 20:
                    events.append({"date": hs300_pe.get("date", ""), "layer": "fundamental",
                                   "title": f"沪深300 PE分位数仅{pe_pct:.1f}% — 极度低估", "detail": "", "importance": 3})
                elif pe_pct > 80:
                    events.append({"date": hs300_pe.get("date", ""), "layer": "fundamental",
                                   "title": f"沪深300 PE分位数达{pe_pct:.1f}% — 高估预警", "detail": "", "importance": 3})
    except Exception:
        pass

    # 3. Research report events
    try:
        research_resp = await research_future
        if research_resp["status"] == "ok":
            for r in research_resp["data"].get("reports", [])[:10]:
                events.append({
                    "date": r["date"], "layer": "research",
                    "title": f"[{r['org']}] {r['title'][:50]}",
                    "detail": f"评级: {r['rating']}", "importance": 1,
                })
    except Exception:
        pass

    # Sort by date descending
    events.sort(key=lambda e: e["date"], reverse=True)

    result = {"price_data": price_data, "events": events, "event_count": len(events)}
    _cache_set(cache_key, result)
    return _ok(result)


# ═══════════════════════════════════════════
# 地道 (Didao) — Stock Selection
# ═══════════════════════════════════════════

@router.get("/didao/market")
async def didao_market(symbol: str, days: int = Query(60, ge=1, le=365)):
    """地道行情层: 个股K线+MA+KDJ."""
    from server.routers.data import DATA_DIR
    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        return _err(f"No data for {symbol}")

    df = pd.read_parquet(fpath).sort_values("date").tail(days)

    try:
        import sancai_core
        ohlcv = {
            "open": df["open"].tolist(), "high": df["high"].tolist(),
            "low": df["low"].tolist(), "close": df["close"].tolist(),
            "volume": df["volume"].tolist(),
        }
        result_str = sancai_core.analyze_trends(json.dumps(ohlcv), "daily")
        indicators = json.loads(result_str)
    except Exception:
        close = df["close"].values
        indicators = {"mas": {}, "kdj": {"k": [], "d": [], "j": []}}
        for p in [5, 13, 21, 34, 55, 144, 233]:
            ma = pd.Series(close).rolling(window=p).mean()
            indicators["mas"][str(p)] = [None if pd.isna(v) else float(v) for v in ma]

    data = []
    for i, (_, row) in enumerate(df.iterrows()):
        rec = {
            "date": str(row["date"])[:10],
            "open": float(row["open"]), "close": float(row["close"]),
            "high": float(row["high"]), "low": float(row["low"]),
            "volume": float(row["volume"]),
        }
        for p_str, ma_vals in indicators.get("mas", {}).items():
            if i < len(ma_vals) and ma_vals[i] is not None:
                rec[f"ma_{p_str}"] = round(ma_vals[i], 4)
        data.append(rec)

    return _ok({"symbol": symbol, "data": data, "count": len(data)})


@router.get("/didao/fundamental")
async def didao_fundamental(symbol: str):
    """地道基础数据层: 财务指标."""
    cache_key = f"didao_fund_{symbol}"
    cached = _cached(cache_key, ttl=600)
    if cached:
        return _ok(cached, "cache")

    # Financial abstract
    fin_df, fin_err = _safe_akshare(lambda ak, s=symbol: ak.stock_financial_abstract(symbol=s))

    # Research reports for rating history
    research_df, res_err = _safe_akshare(lambda ak, s=symbol: ak.stock_research_report_em(symbol=s))

    # Fund flow
    market = _market_code(symbol)
    flow_df, flow_err = _safe_akshare(
        lambda ak, s=symbol, m=market: ak.stock_individual_fund_flow(stock=s, market=m)
    )

    # Parse financial abstract
    fundamentals = {}
    if fin_df is not None:
        # Filter key indicators
        key_indicators = ["基本每股收益", "加权平均净资产收益率", "营业收入", "净利润",
                          "归属于母公司所有者的净利润", "经营活动产生的现金流量净额",
                          "资产总计", "负债合计", "归属于母公司股东权益"]
        for _, row in fin_df.iterrows():
            indicator = str(row["指标"])
            if indicator in key_indicators:
                fundamentals[indicator] = {
                    "latest": float(row.iloc[2]) if len(row) > 2 and pd.notna(row.iloc[2]) else None,
                    "prev_year": float(row.iloc[6]) if len(row) > 6 and pd.notna(row.iloc[6]) else None,
                }

    # Parse recent ratings
    ratings = []
    if research_df is not None and len(research_df) > 0:
        org_col = research_df.columns[3] if len(research_df.columns) > 3 else None
        rating_col = research_df.columns[4] if len(research_df.columns) > 4 else None
        date_col = research_df.columns[5] if len(research_df.columns) > 5 else None
        for _, row in research_df.tail(10).iterrows():
            ratings.append({
                "org": str(row[org_col]) if org_col else "",
                "rating": str(row[rating_col]) if rating_col else "",
                "date": str(row[date_col])[:10] if date_col else "",
            })

    # Money flow summary
    flow_summary = None
    if flow_df is not None and len(flow_df) > 0:
        try:
            main_col = flow_df.columns[3]  # 主力净流入-净额
            recent_flow = pd.to_numeric(flow_df[main_col].tail(10), errors='coerce')
            flow_summary = {
                "recent_net": float(recent_flow.sum()),
                "positive_days": int((recent_flow > 0).sum()),
            }
        except Exception:
            pass

    result = {
        "symbol": symbol,
        "fundamentals": fundamentals,
        "ratings": ratings,
        "fund_flow": flow_summary,
        "errors": {"financial": fin_err, "research": res_err, "flow": flow_err},
    }
    _cache_set(cache_key, result)
    return _ok(result)


@router.get("/didao/research")
async def didao_research(symbol: str, days: int = Query(90, ge=1, le=365)):
    """地道研报层: 个股研报列表."""
    cache_key = f"didao_research_{symbol}_{days}"
    cached = _cached(cache_key, ttl=1800)
    if cached:
        return _ok(cached, "cache")

    df, err = _safe_akshare(lambda ak, s=symbol: ak.stock_research_report_em(symbol=s))
    if err:
        return _err(err)

    col_map = {c: c for c in df.columns}
    title_col = df.columns[2] if len(df.columns) > 2 else None
    org_col = df.columns[3] if len(df.columns) > 3 else None
    rating_col = df.columns[4] if len(df.columns) > 4 else None
    date_col = df.columns[5] if len(df.columns) > 5 else None
    target_col = df.columns[6] if len(df.columns) > 6 else None  # 目标价

    cutoff = datetime.now() - timedelta(days=days)
    reports = []
    for _, row in df.iterrows():
        try:
            d = pd.Timestamp(row[date_col]) if date_col else None
            if d and d < cutoff:
                continue
        except Exception:
            pass
        reports.append({
            "title": str(row[title_col])[:60] if title_col else "",
            "org": str(row[org_col]) if org_col else "",
            "rating": str(row[rating_col]) if rating_col else "",
            "target_price": str(row[target_col]) if target_col and pd.notna(row[target_col]) else "",
            "date": str(row[date_col])[:10] if date_col else "",
        })
        if len(reports) >= 20:
            break

    return _ok({"symbol": symbol, "reports": reports, "count": len(reports)})


@router.get("/didao/news")
async def didao_news(symbol: str, days: int = Query(60, ge=1, le=365)):
    """地道新闻层: 个股公告和新闻（通过 notice report 数据）."""
    cache_key = f"didao_news_{symbol}_{days}"
    cached = _cached(cache_key, ttl=900)
    if cached:
        return _ok(cached, "cache")

    # Use stock_notice_report as a source of company events
    prefix = _symbol_prefix(symbol)
    df, err = _safe_akshare(lambda ak, s=symbol: ak.stock_notice_report(symbol=s))
    if err:
        return _ok({
            "symbol": symbol,
            "events": [],
            "note": f"公告数据暂不可用 ({err[:50]})",
        })

    # stock_notice_report columns vary — try to extract structured data
    result_events = []
    try:
        for _, row in df.tail(30).iterrows():
            row_dict = row.to_dict()
            result_events.append({
                k: str(v)[:100] if v is not None else ""
                for k, v in row_dict.items()
            })
    except Exception:
        pass

    return _ok({"symbol": symbol, "events": result_events, "count": len(result_events)})


@router.get("/didao/announcements")
async def didao_announcements(symbol: str, days: int = Query(90, ge=1, le=365)):
    """地道公告层: 公司公告."""
    cache_key = f"didao_ann_{symbol}_{days}"
    cached = _cached(cache_key, ttl=1800)
    if cached:
        return _ok(cached, "cache")

    # Try individual notice report
    prefix = _symbol_prefix(symbol)
    # stock_individual_notice_report takes security type — try common types
    df = None
    err_msgs = []
    security_types = ["010303", "010301", "010401"]  # 深市/沪市 common types
    for sec in security_types:
        df, err = _safe_akshare(
            lambda ak, s=symbol, sec=sec: ak.stock_individual_notice_report(security=sec, symbol=s)
        )
        if df is not None:
            break
        if err:
            err_msgs.append(f"sec={sec}: {err[:40]}")

    if df is None:
        return _ok({
            "symbol": symbol, "announcements": [],
            "note": f"公告数据获取失败: {'; '.join(err_msgs[:2])}",
        })

    # Parse announcement data
    try:
        cols = list(df.columns)
        records = []
        for _, row in df.tail(20).iterrows():
            rec = {str(c)[:30]: str(row[c])[:120] if row[c] is not None else "" for c in cols[:6]}
            records.append(rec)
    except Exception as e:
        records = []

    return _ok({"symbol": symbol, "announcements": records, "count": len(records)})


@router.get("/didao/score")
async def didao_score(symbol: str, profile: str = Query("balanced", pattern="^(conservative|balanced|aggressive)$")):
    """地道评分: 多因子综合评分 + 爻卦象.

    profile 参数来自人道偏好，调整各因子权重:
      - conservative: 加重基本面(60%), 降低技术面(20%)
      - aggressive: 加重技术面(50%)+情绪(35%), 降低基本面(15%)
    """
    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        return _err(f"No data for {symbol}")

    df = pd.read_parquet(fpath).sort_values("date")
    close = df["close"].values
    volume = df["volume"].values
    latest_close = close[-1]

    weights = PROFILE_WEIGHTS.get(profile, PROFILE_WEIGHTS["balanced"])

    score = 50
    tech_score = 0
    fund_score = 0
    sent_score = 0

    # MA factors (技术面)
    if len(close) >= 55:
        ma21 = close[-21:].mean()
        ma34 = close[-34:].mean()
        ma55 = close[-55:].mean()
        if latest_close > ma21:
            tech_score += 15
        if latest_close > ma55:
            tech_score += 15
        if ma21 > ma34:
            tech_score += 10

    # Volume factor (技术面)
    if len(volume) >= 10:
        recent_vol = volume[-5:].mean()
        prior_vol = volume[-10:-5].mean()
        if prior_vol > 0 and recent_vol > prior_vol * 1.1:
            tech_score += 10

    fundamentals = {}
    # Fundamental factors
    try:
        fund_resp = await didao_fundamental(symbol)
        if fund_resp["status"] == "ok":
            fd = fund_resp["data"]
            fundamentals = fd.get("fundamentals", {})

            roe_data = fundamentals.get("加权平均净资产收益率", {}).get("latest")
            if roe_data is not None and roe_data > 15:
                fund_score += 10

            pe_data = fundamentals.get("归属于母公司所有者的净利润", {}).get("latest")
            if pe_data is not None and pe_data > 0:
                fund_score += 5

            ratings = fd.get("ratings", [])
            recent_buys = sum(1 for r in ratings if "买入" in r.get("rating", "") or "增持" in r.get("rating", ""))
            if recent_buys >= 3:
                fund_score += 10
            elif recent_buys >= 1:
                fund_score += 5

            # Money flow (情绪面)
            flow = fd.get("fund_flow") or {}
            if flow.get("positive_days", 0) >= 7:
                sent_score += 10
    except Exception:
        pass

    # Market PE percentile (基本面)
    try:
        fund_resp = await tiandao_fundamental()
        if fund_resp["status"] == "ok":
            pe_pct = fund_resp["data"].get("沪深300", {}).get("pe_pct")
            if pe_pct is not None and pe_pct < 30:
                fund_score += 5
    except Exception:
        pass

    # Weighted total
    score = 50 + (tech_score * weights["technical"] / 0.4 +
                  fund_score * weights["fundamental"] / 0.4 +
                  sent_score * weights["sentiment"] / 0.2) * 0.4
    score = min(max(round(score), 0), 100)

    assessment = "吉" if score >= 70 else ("凶" if score < 35 else "平")

    # ── 爻卦 assessment for 地道 ──
    gua = assess_stock(close, symbol, fundamentals)

    return _ok({
        "symbol": symbol,
        "profile": profile,
        "score": score,
        "assessment": assessment,
        "latest_price": float(latest_close),
        "factor_scores": {"technical": tech_score, "fundamental": fund_score, "sentiment": sent_score},
        "weights": weights,
        "gua": {
            "hexagram": gua["hexagram"],
            "symbol": gua["symbol"],
            "nature": gua["nature"],
            "meaning": gua["meaning"],
            "color": gua["color"],
            "yao_ci": gua["yao_ci"],
            "yao_meaning": gua["yao_meaning"],
            "advice": gua["advice"],
            "alignment": gua.get("alignment", ""),
            "detail": gua.get("detail", ""),
        },
    })


@router.get("/didao/timeline")
async def didao_timeline(symbol: str, days: int = Query(60, ge=1, le=365)):
    """地道统一时间轴: 聚合个股5层事件."""
    cache_key = f"didao_timeline_{symbol}_{days}"
    cached = _cached(cache_key, ttl=120)
    if cached:
        return _ok(cached, "cache")

    events = []
    price_data = None

    # Fire all sub-fetches in parallel
    mkt_future = asyncio.ensure_future(didao_market(symbol=symbol, days=days))
    res_future = asyncio.ensure_future(didao_research(symbol=symbol, days=days))
    ann_future = asyncio.ensure_future(didao_announcements(symbol=symbol, days=days))
    fund_future = asyncio.ensure_future(didao_fundamental(symbol))

    # Market data
    try:
        mkt = await mkt_future
        if mkt["status"] == "ok":
            d = mkt["data"]["data"]
            price_data = {
                "dates": [r["date"] for r in d],
                "close": [r["close"] for r in d],
                "open": [r["open"] for r in d],
                "high": [r["high"] for r in d],
                "low": [r["low"] for r in d],
                "name": symbol,
            }
            for i, r in enumerate(d):
                if i > 0:
                    pct = (r["close"] - d[i-1]["close"]) / d[i-1]["close"] * 100
                    if abs(pct) > 3:
                        events.append({
                            "date": r["date"], "layer": "market",
                            "title": f"{'大涨' if pct > 0 else '大跌'} {abs(pct):.1f}%",
                            "detail": f"收 {r['close']:.2f}", "importance": 2,
                        })
    except Exception:
        pass

    # Research
    try:
        res = await res_future
        if res["status"] == "ok":
            for r in res["data"].get("reports", [])[:8]:
                events.append({
                    "date": r["date"], "layer": "research",
                    "title": f"[{r['org']}] {r['rating']}",
                    "detail": r["title"], "importance": 1,
                })
    except Exception:
        pass

    # Announcements
    try:
        ann = await ann_future
        if ann["status"] == "ok":
            for a in ann["data"].get("announcements", [])[:8]:
                title = str(list(a.values())[:2]) if a else ""
                date_val = ""
                for v in a.values():
                    vstr = str(v)
                    if vstr and vstr[:4].isdigit() and "-" in vstr:
                        date_val = vstr[:10]
                        break
                events.append({
                    "date": date_val, "layer": "announcement",
                    "title": title[:60], "detail": "", "importance": 1,
                })
    except Exception:
        pass

    # Fundamental
    try:
        fund = await fund_future
        if fund["status"] == "ok":
            fd = fund["data"]
            roe = fd.get("fundamentals", {}).get("加权平均净资产收益率", {}).get("latest")
            if roe is not None and roe > 15:
                events.append({
                    "date": datetime.now().strftime("%Y-%m-%d"), "layer": "fundamental",
                    "title": f"ROE {roe:.1f}% — 高盈利能力", "detail": "", "importance": 1,
                })
    except Exception:
        pass

    events.sort(key=lambda e: e["date"], reverse=True)

    return _ok({
        "symbol": symbol,
        "price_data": price_data,
        "events": events,
        "event_count": len(events),
    })


# ═══════════════════════════════════════════
# 人道 (Rendao) — Trade Execution
# ═══════════════════════════════════════════

@router.get("/rendao/flow")
async def rendao_flow(symbol: str):
    """人道资金流向: 个股资金流向."""
    cache_key = f"rendao_flow_{symbol}"
    cached = _cached(cache_key, ttl=120)
    if cached:
        return _ok(cached, "cache")

    market = _market_code(symbol)
    df, err = _safe_akshare(
        lambda ak, s=symbol, m=market: ak.stock_individual_fund_flow(stock=s, market=m)
    )
    if err:
        return _err(err)

    # Parse columns
    cols = list(df.columns)
    data = []
    for _, row in df.tail(10).iterrows():
        data.append({
            "date": str(row[cols[0]])[:10] if len(cols) > 0 else "",
            "close": float(row[cols[1]]) if len(cols) > 1 and pd.notna(row[cols[1]]) else 0,
            "main_net": float(row[cols[3]]) if len(cols) > 3 and pd.notna(row[cols[3]]) else 0,
            "main_pct": float(row[cols[4]]) if len(cols) > 4 and pd.notna(row[cols[4]]) else 0,
        })

    net_recent = sum(d["main_net"] for d in data)
    return _ok({
        "symbol": symbol,
        "daily": data,
        "net_recent_10d": round(net_recent, 2),
        "signal": "inflow" if net_recent > 0 else "outflow",
    })


@router.get("/rendao/timeline")
async def rendao_timeline():
    """人道时间轴: 持仓股聚合事件 + 爻卦象."""
    universe = _load_universe()
    held_symbols = [s["symbol"] for s in universe[:3]]  # Top 3 as mock positions

    # Fire all per-symbol fetches in parallel
    futures = {}
    for sym in held_symbols:
        futures[(sym, 'didao')] = asyncio.ensure_future(didao_timeline(symbol=sym, days=30))
        futures[(sym, 'flow')] = asyncio.ensure_future(rendao_flow(symbol=sym))

    all_events = []
    flow_signals = []
    for sym in held_symbols:
        try:
            didao_resp = await futures[(sym, 'didao')]
            if didao_resp["status"] == "ok":
                for e in didao_resp["data"].get("events", [])[:5]:
                    e["symbol"] = sym
                    all_events.append(e)
        except Exception:
            pass
        try:
            flow_resp = await futures[(sym, 'flow')]
            if flow_resp["status"] == "ok":
                flow_signals.append({"symbol": sym, "signal": flow_resp["data"].get("signal", "outflow")})
        except Exception:
            pass

    all_events.sort(key=lambda e: e["date"], reverse=True)

    # ── 人道爻卦评估 ──
    ren_gua = assess_execution(held_symbols, flow_signals if flow_signals else None)

    return _ok({
        "positions": held_symbols,
        "events": all_events[:50],
        "event_count": len(all_events),
        "note": "模拟持仓: 实时交易功能开发中",
        "gua": {
            "hexagram": ren_gua["hexagram"],
            "symbol": ren_gua["symbol"],
            "nature": ren_gua["nature"],
            "meaning": ren_gua["meaning"],
            "color": ren_gua["color"],
            "yao_ci": ren_gua["yao_ci"],
            "yao_meaning": ren_gua["yao_meaning"],
            "advice": ren_gua["advice"],
            "direction": ren_gua["direction"],
            "detail": ren_gua["detail"],
        },
    })


@router.get("/alignment")
async def sancai_alignment():
    """三才合一信号: 检查天道/地道/人道评估是否对齐，含爻卦象."""
    universe = _load_universe()
    top_symbol = universe[0]["symbol"] if universe else "000001"

    # Get tiandao indices overview
    try:
        indices_resp = await tiandao_indices(days=90)
        t_assessment = indices_resp["data"]["overall"]["assessment"] if indices_resp["status"] == "ok" else "平"
        t_gua = indices_resp["data"]["overall"] if indices_resp["status"] == "ok" else {}
    except Exception:
        t_assessment = "平"
        t_gua = {}

    # Get didao score with gua
    didao_resp = await didao_score(symbol=top_symbol)
    d_assessment = didao_resp["data"]["assessment"] if didao_resp["status"] == "ok" else "平"
    d_gua = didao_resp["data"].get("gua", {}) if didao_resp["status"] == "ok" else {}

    # Get rendao gua
    try:
        rendao_resp = await rendao_timeline()
        r_assessment = rendao_resp["data"].get("gua", {}).get("direction", "平") if rendao_resp["status"] == "ok" else "平"
        r_gua = rendao_resp["data"].get("gua", {}) if rendao_resp["status"] == "ok" else {}
    except Exception:
        r_assessment = "平"
        r_gua = {}

    assessments = {"tiandao": t_assessment, "didao": d_assessment, "rendao": r_assessment}
    aligned = all(v == "吉" for v in assessments.values())
    all_bear = all(v == "凶" for v in assessments.values())

    signal = "三才合一·吉 🟢" if aligned else ("三才皆凶·空仓 🔴" if all_bear else "三才分歧·观望 🟡")

    return _ok({
        "assessments": assessments,
        "aligned": aligned,
        "signal": signal,
        "gua": {
            "tiandao": t_gua,
            "didao": d_gua,
            "rendao": r_gua,
        },
    })


@router.get("/didao/strategy")
async def didao_strategy(profile: str = Query("balanced", pattern="^(conservative|balanced|aggressive)$")):
    """地道策略路由: 根据人道偏好返回推荐策略 + 参数."""
    strategies = {
        "conservative": {
            "primary": "strict",
            "primary_label": "严格: 三才BP1",
            "secondary": "simple",
            "secondary_label": "简单: KDJ超卖反弹",
            "recommended_period": "daily",
            "recommended_schools": ["dow_theory", "wyckoff"],
            "description": "保守型以长周期稳健策略为主，注重安全边际和基本面支撑",
            "filters": {
                "min_score": 60,
                "require_roe_positive": True,
                "prefer_large_cap": True,
            },
        },
        "balanced": {
            "primary": "schools",
            "primary_label": "流派: 多策略共识",
            "secondary": "chan_theory",
            "secondary_label": "缠论: 笔-线段-中枢-背驰",
            "recommended_period": "60min",
            "recommended_schools": ["chan_theory", "price_action", "morphology"],
            "description": "均衡型兼顾攻守，多策略共识确认后入场",
            "filters": {
                "min_score": 50,
                "require_roe_positive": False,
                "prefer_large_cap": False,
            },
        },
        "aggressive": {
            "primary": "schools",
            "primary_label": "流派: 多策略共识",
            "secondary": "ict",
            "secondary_label": "ICT: OB+FVG+流动性猎杀",
            "recommended_period": "15min",
            "recommended_schools": ["ict", "gann", "wave_theory"],
            "description": "激进型以短线龙头策略为主，追求高赔率机会",
            "filters": {
                "min_score": 35,
                "require_roe_positive": False,
                "prefer_large_cap": False,
            },
        },
    }
    strat = strategies.get(profile, strategies["balanced"])
    return _ok({"profile": profile, "strategy": strat})


@router.get("/pipeline")
async def sancai_pipeline():
    """三才完整流程: 天道方向 → 地道筛选 → 人道计划."""
    result = {"tiandao": None, "didao": None, "rendao": None, "decision_chain": []}

    # 1. 天道方向
    try:
        t_resp = await tiandao_indices(days=90)
        if t_resp["status"] == "ok":
            overall = t_resp["data"]["overall"]
            result["tiandao"] = {
                "direction": overall["assessment"],
                "hexagram": overall["hexagram"],
                "yao_ci": overall["yao_ci"],
                "meaning": overall["meaning"],
                "advice": overall["advice"],
            }
            result["decision_chain"].append({
                "layer": "天道",
                "action": f"大势判断: {overall['assessment']} — {overall['yao_ci']}",
                "detail": overall["meaning"],
            })
    except Exception as e:
        result["tiandao"] = {"error": str(e)}

    # 2. 人道偏好
    from .rendao_quiz import get_user_profile
    profile_data = get_user_profile()
    profile = profile_data["profile"] if profile_data else "balanced"

    # 3. 地道策略
    try:
        s_resp = await didao_strategy(profile=profile)
        if s_resp["status"] == "ok":
            result["didao"]["strategy"] = s_resp["data"]["strategy"]
    except Exception as e:
        result["didao"] = {"error": str(e)}

    # 4. 地道标的评分
    universe = _load_universe()
    top_symbols = [s["symbol"] for s in universe[:5]]
    stock_scores = []
    for sym in top_symbols:
        try:
            sc_resp = await didao_score(symbol=sym, profile=profile)
            if sc_resp["status"] == "ok":
                stock_scores.append({
                    "symbol": sym,
                    "score": sc_resp["data"]["score"],
                    "assessment": sc_resp["data"]["assessment"],
                    "gua_yao": sc_resp["data"].get("gua", {}).get("yao_ci", ""),
                    "price": sc_resp["data"]["latest_price"],
                })
        except Exception:
            pass
    stock_scores.sort(key=lambda s: s["score"], reverse=True)
    result["didao"] = {
        **result.get("didao", {}),
        "stock_scores": stock_scores,
        "top_picks": [s for s in stock_scores if s["score"] >= 60][:5],
    }

    # 5. 人道计划
    from .rendao_plan import POSITION_PLANS
    plan = POSITION_PLANS.get(profile, POSITION_PLANS["balanced"])
    result["rendao"] = {
        "profile": profile,
        "plan": plan,
        "advice": (f"建议仓位 {plan['total_position_pct']}%，"
                   f"单票最大 {plan['max_single_pct']}%，"
                   f"止损 {plan['stop_loss_pct']}%"),
    }

    result["decision_chain"].append({
        "layer": "人道",
        "action": f"偏好: {profile} — {profile_data.get('label', '未测评') if profile_data else '默认均衡型'}",
        "detail": result["rendao"]["advice"],
    })
    result["decision_chain"].append({
        "layer": "地道",
        "action": f"策略: {result.get('didao', {}).get('strategy', {}).get('primary_label', 'N/A')}",
        "detail": f"推荐标的: {', '.join(s['symbol'] for s in result['didao'].get('top_picks', []))}",
    })

    tiandao_dir = (result.get("tiandao") or {}).get("direction", "平")
    top_count = len(result["didao"].get("top_picks", []))
    if tiandao_dir == "吉" and top_count >= 2:
        final_verdict = "三才共振·积极做多"
    elif tiandao_dir == "凶":
        final_verdict = "天道向下·防御为主"
    elif top_count == 0:
        final_verdict = "无合格标的·观望"
    else:
        final_verdict = "谨慎参与·轻仓试探"

    result["final_verdict"] = final_verdict

    return _ok(result)
