"""三才量化 — 集合竞价数据采集 + 分析引擎.

用于监控竞价期间(9:15-9:25)的高开低开、竞价放量、概念趋向，
生成竞价复盘归因报告并通过飞书推送。

数据源: 东方财富 push2 API (akshare stock_zh_a_auction_detail 已在 1.18 移除)
"""
import concurrent.futures
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from server.utils import DATA_DIR

logger = logging.getLogger(__name__)

# 竞价数据缓存
_auction_cache: dict = {}
_last_auction_date: str = ""
_EM_SESSION = None


def _get_em_session():
    global _EM_SESSION
    if _EM_SESSION is None:
        _EM_SESSION = requests.Session()
        _EM_SESSION.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        })
    return _EM_SESSION


def _fetch_em_stock(symbol: str) -> dict | None:
    """东方财富单票实时数据（竞价阶段=竞价价格, 开盘后=实时价）."""
    s = _get_em_session()
    secid = f"{'0' if symbol.startswith(('0','3')) else '1'}.{symbol}"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f170"
    try:
        r = s.get(url, timeout=8)
        d = r.json().get("data", {})
        if not d:
            return None
        price = round((d.get("f43", 0) or 0) / 100, 2)
        prev_close = round((d.get("f60", 0) or 0) / 100, 2)
        gap = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
        return {
            "symbol": symbol,
            "name": d.get("f58", ""),
            "price": price,
            "prev_close": prev_close,
            "open": round((d.get("f46", 0) or 0) / 100, 2),
            "high": round((d.get("f44", 0) or 0) / 100, 2),
            "low": round((d.get("f45", 0) or 0) / 100, 2),
            "volume": d.get("f47", 0) or 0,
            "amount": d.get("f48", 0) or 0,
            "vol_ratio": round((d.get("f50", 0) or 0) / 100, 2),
            "change_pct": round((d.get("f170", 0) or 0) / 100, 2),
            "gap_pct": gap,
        }
    except Exception:
        return None


def _batch_fetch_em(symbols: list[str], max_workers: int = 4) -> list[dict]:
    """并发拉取多只股票的东方财富实时数据."""
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_em_stock, s): s for s in symbols}
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            if r and r["price"] > 0:
                results.append(r)
    return results


async def fetch_auction_data(force: bool = False, symbols: list[str] = None) -> dict:
    """拉取股票实时/竞价数据（东方财富直连）。

    竞价期间 price=竞价参考价；开盘后=实时价。
    symbols=None 时拉取 持仓+昨日扫描的全部标的。

    Returns:
        {"stocks": [...], "count": int, "ts": str}
    """
    global _auction_cache, _last_auction_date
    today = datetime.now().strftime("%Y%m%d")

    if not force and _last_auction_date == today and _auction_cache:
        return _auction_cache

    if symbols is None:
        symbols = list(_get_auction_symbols())

    if not symbols:
        return {"stocks": [], "count": 0, "ts": datetime.now().isoformat()}

    stocks = _batch_fetch_em(symbols[:60])
    _auction_cache = {"stocks": stocks, "count": len(stocks),
                      "ts": datetime.now().isoformat(), "source": "eastmoney"}
    _last_auction_date = today
    logger.info(f"Auction fetched (EM): {len(stocks)}/{len(symbols)} stocks")
    return _auction_cache


def _get_auction_symbols() -> set:
    """合并持仓+昨日扫描结果的股票代码."""
    symbols = set()
    from server.utils.holdings_utils import _load_holdings as _load
    for h in _load():
        symbols.add(h["symbol"])
    try:
        from server.scan_history_db import get_db as _gdb
        db = _gdb()
        yesterday = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                     - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        modes = db.execute("SELECT DISTINCT mode FROM scan_summary WHERE date=?",
                           (yesterday,)).fetchall()
        for (mode,) in modes:
            from server.scan_history_db import get_scan_results
            for r in get_scan_results(yesterday, mode):
                if r.get("symbol"):
                    symbols.add(r["symbol"])
    except Exception:
        pass
    return symbols


def _analyze_auction(stocks: list[dict], concept_map: dict = None) -> dict:
    """分析竞价结果，生成归因报告."""
    high_open, mild_high, mild_low, low_open = [], [], [], []
    volume_surge = []
    for s in stocks:
        gap = s.get("gap_pct", 0)
        vol_r = s.get("vol_ratio", 1)
        name = s.get("name", s.get("symbol", ""))
        sym = s.get("symbol", "")
        entry = f"{name}({sym}) 竞价{s['price']:.2f} 开{gap:+.1f}% 量比{vol_r:.1f}"
        if gap > 2: high_open.append(entry)
        elif gap > 0: mild_high.append(entry)
        elif gap > -2: mild_low.append(entry)
        else: low_open.append(entry)
        if vol_r > 3:
            volume_surge.append(f"{name}({sym}) 量比{vol_r:.1f}🔥")
    advice_parts = []
    if high_open: advice_parts.append(f"✅ {len(high_open)}只高开>2%")
    if low_open: advice_parts.append(f"⚠ {len(low_open)}只低开<-2%")
    if volume_surge: advice_parts.append(f"🔥 {len(volume_surge)}只竞价放量")
    if not advice_parts: advice_parts.append("市场竞价平稳")
    return {
        "high_open": high_open, "mild_high": mild_high,
        "mild_low": mild_low, "low_open": low_open,
        "volume_surge": volume_surge,
        "total_watched": len(stocks),
        "concept_trend": "",
        "operation_advice": "\n".join(advice_parts),
    }


async def auction_report(candidates: list[str] = None) -> dict:
    """一站式竞价分析入口：采集→筛选→分析。monitor.py 竞价推送调用。"""
    auction_data = await fetch_auction_data()
    all_stocks = auction_data.get("stocks", [])
    if not all_stocks:
        return {"analysis": {}, "candidates": [], "error": auction_data.get("error", "无竞价数据")}
    watch = _get_auction_symbols()
    if candidates:
        watch.update(candidates)
    candidates_list = [s for s in all_stocks if s["symbol"] in watch]
    if len(candidates_list) < 10:
        sorted_by_gap = sorted(all_stocks, key=lambda x: abs(x.get("gap_pct", 0)), reverse=True)
        seen = {s["symbol"] for s in candidates_list}
        for s in sorted_by_gap:
            if s["symbol"] not in seen:
                candidates_list.append(s)
                seen.add(s["symbol"])
            if len(candidates_list) >= 30:
                break
    analysis = _analyze_auction(candidates_list)
    return {"analysis": analysis, "candidates": candidates_list, "ts": datetime.now().isoformat()}
