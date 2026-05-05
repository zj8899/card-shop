"""Sancai multi-layer data endpoints.

Tiandao (macro timing), Didao (stock selection), Rendao (execution).
Each tier exposes: market, research, news, fundamental, announcements + unified timeline.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

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
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sh000300": "沪深300",
}


@router.get("/tiandao/market")
async def tiandao_market(days: int = Query(60, ge=1, le=365)):
    """天道行情层: 三大指数日线数据."""
    cache_key = f"tiandao_market_{days}"
    cached = _cached(cache_key, ttl=120)  # 2min for index data
    if cached:
        return _ok(cached, "cache")

    result = {}
    for code, name in TIANDAO_INDICES.items():
        df, err = _safe_akshare(lambda ak, c=code: ak.stock_zh_index_daily(symbol=c))
        if err:
            result[code] = {"name": name, "data": [], "error": err}
            continue
        df = df.sort_values("date").tail(days)
        result[code] = {
            "name": name,
            "data": [{
                "date": str(r["date"])[:10],
                "open": float(r["open"]),
                "close": float(r["close"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "volume": float(r.get("volume", 0)),
            } for _, r in df.iterrows()],
            "latest": float(df["close"].iloc[-1]),
            "change_pct": round(float(df["close"].pct_change().iloc[-1] * 100), 2) if len(df) > 1 else 0,
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

    # 1. Market data (price line)
    market_result = await tiandao_market(days=days)
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
        fund_resp = await tiandao_fundamental()
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
        research_resp = await tiandao_research(days=days)
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
async def didao_score(symbol: str):
    """地道评分: 多因子综合评分."""
    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        return _err(f"No data for {symbol}")

    df = pd.read_parquet(fpath).sort_values("date")
    close = df["close"].values
    volume = df["volume"].values
    latest_close = close[-1]

    score = 50

    # MA factors (from stock data)
    if len(close) >= 55:
        ma21 = close[-21:].mean()
        ma34 = close[-34:].mean()
        ma55 = close[-55:].mean()
        if latest_close > ma21:
            score += 15
        if latest_close > ma55:
            score += 15
        if ma21 > ma34:
            score += 10

    # Volume factor
    if len(volume) >= 10:
        recent_vol = volume[-5:].mean()
        prior_vol = volume[-10:-5].mean()
        if prior_vol > 0 and recent_vol > prior_vol * 1.1:
            score += 10

    # Fundamental factors (from cache if available)
    try:
        fund_resp = await didao_fundamental(symbol)
        if fund_resp["status"] == "ok":
            fd = fund_resp["data"]

            # ROE check
            roe_data = fd.get("fundamentals", {}).get("加权平均净资产收益率", {}).get("latest")
            if roe_data is not None and roe_data > 15:
                score += 10

            # PE check
            pe_data = fd.get("fundamentals", {}).get("归属于母公司所有者的净利润", {}).get("latest")
            if pe_data is not None and pe_data > 0:
                score += 5

            # Rating upgrades (recent buy/hold ratings)
            ratings = fd.get("ratings", [])
            recent_buys = sum(1 for r in ratings if "买入" in r.get("rating", "") or "增持" in r.get("rating", ""))
            if recent_buys >= 3:
                score += 10
            elif recent_buys >= 1:
                score += 5

            # Money flow
            flow = fd.get("fund_flow") or {}
            if flow.get("positive_days", 0) >= 7:
                score += 10
    except Exception:
        pass

    # PE percentile from market data
    try:
        fund_resp = await tiandao_fundamental()
        if fund_resp["status"] == "ok":
            pe_pct = fund_resp["data"].get("沪深300", {}).get("pe_pct")
            if pe_pct is not None and pe_pct < 30:
                score += 5  # low overall PE = good time to buy
    except Exception:
        pass

    score = min(score, 100)

    assessment = "吉" if score >= 70 else ("凶" if score < 35 else "平")

    return _ok({
        "symbol": symbol,
        "score": score,
        "assessment": assessment,
        "latest_price": float(latest_close),
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

    # Market data
    try:
        mkt = await didao_market(symbol=symbol, days=days)
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
        res = await didao_research(symbol=symbol, days=days)
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
        ann = await didao_announcements(symbol=symbol, days=days)
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
        fund = await didao_fundamental(symbol)
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
    """人道时间轴: 持仓股聚合事件（模拟持仓数据）."""
    universe = _load_universe()
    held_symbols = [s["symbol"] for s in universe[:3]]  # Top 3 as mock positions

    all_events = []
    for sym in held_symbols:
        try:
            didao_resp = await didao_timeline(symbol=sym, days=30)
            if didao_resp["status"] == "ok":
                for e in didao_resp["data"].get("events", [])[:5]:
                    e["symbol"] = sym
                    all_events.append(e)
        except Exception:
            pass

    all_events.sort(key=lambda e: e["date"], reverse=True)

    return _ok({
        "positions": held_symbols,
        "events": all_events[:50],
        "event_count": len(all_events),
        "note": "模拟持仓: 实时交易功能开发中",
    })


@router.get("/alignment")
async def sancai_alignment():
    """三才合一信号: 检查天道/地道/人道评估是否对齐."""
    universe = _load_universe()
    top_symbol = universe[0]["symbol"] if universe else "000001"

    # Quick assessment
    tiandao_resp = await tiandao_timeline(days=30)
    didao_resp = await didao_score(symbol=top_symbol)

    t_assessment = "吉"
    if tiandao_resp["status"] == "ok":
        # Simple: if PE is low → bullish
        pass  # Default to 吉 for now

    d_assessment = didao_resp["data"]["assessment"] if didao_resp["status"] == "ok" else "平"
    r_assessment = "平"  # 人道 placeholder

    assessments = {"tiandao": t_assessment, "didao": d_assessment, "rendao": r_assessment}
    aligned = all(v == "吉" for v in assessments.values())
    all_bear = all(v == "凶" for v in assessments.values())

    signal = "三才合一·吉 🟢" if aligned else ("三才皆凶·空仓 🔴" if all_bear else "三才分歧·观望 🟡")

    return _ok({
        "assessments": assessments,
        "aligned": aligned,
        "signal": signal,
    })
