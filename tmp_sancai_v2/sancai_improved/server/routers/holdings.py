"""模拟持仓管理 — CRUD + 现价刷新 + 信号检测."""
import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel as PydanticBaseModel

from server.utils.holdings_utils import _load_holdings, _save_holdings, HOLDINGS_FILE

logger = logging.getLogger(__name__)
router = APIRouter()

DAILY_DIR = Path(__file__).parent.parent.parent / "data" / "raw" / "daily"


class HoldingIn(PydanticBaseModel):
    symbol: str
    name: str = ""
    quantity: int = 0
    cost_price: float = 0.0
    strategy: str = "chan_theory"


@router.get("/api/holdings")
async def list_holdings():
    """获取所有持仓，附带实时现价和最新信号."""
    holdings = _load_holdings()
    if not holdings:
        return {"status": "ok", "data": {"holdings": [], "total_value": 0, "total_pnl": 0}}

    # 找最近扫描日期
    latest_date = None
    try:
        from server.scan_history_db import get_latest_date, get_scan_results
        latest_date = get_latest_date()
    except Exception:
        pass

    # query_real_time (try tencent)
    try:
        from data_sources.tencent_quotes import tencent_quote
        symbols = [h["symbol"] for h in holdings]
        quotes = tencent_quote(symbols) if symbols else {}
    except Exception:
        quotes = {}

    enriched = []
    total_value = 0.0
    total_pnl = 0.0
    for h in holdings:
        sym = h.get("symbol", "")
        cost = float(h.get("cost_price", 0))
        qty = int(h.get("quantity", 0))
        name = h.get("name", "")
        strategy = h.get("strategy", "")

        # 现价：腾讯实时 > parquet日线收盘
        price = 0.0
        source = "无数据"
        q = quotes.get(sym, {})
        if q and q.get("price", 0) > 0:
            price = float(q["price"])
            name = q.get("name", name) or name
            source = "腾讯实时"
        else:
            # fallback to parquet
            fpath = DAILY_DIR / f"{sym}.parquet"
            if fpath.exists():
                try:
                    df = pd.read_parquet(fpath)
                    raw_price = df["close"].values[-1]
                    # Guard against NaN last-close propagating into total_value.
                    price = float(raw_price) if not pd.isna(raw_price) else 0.0
                    source = "日线收盘"
                except Exception:
                    price = 0.0

        if price <= 0:
            continue  # skip this holding — no valid price

        mv = round(price * qty, 2) if price > 0 and qty > 0 else 0
        pnl = round((price - cost) * qty, 2) if price > 0 and cost > 0 else 0
        pnl_pct = round((price - cost) / cost * 100, 2) if cost > 0 and price > 0 else 0
        total_value += mv
        total_pnl += pnl

        # 信号检测
        signal = None
        if latest_date:
            try:
                from server.scan_history_db import get_scan_results
                for mode in [strategy, "chan_theory", "strict", "strict_reverse", "simple"]:
                    results = get_scan_results(latest_date, mode)
                    for r in results:
                        if r.get("symbol") == sym:
                            signal = {
                                "date": latest_date,
                                "mode": mode,
                                "buy_type": r.get("buy_type", ""),
                                "confidence": r.get("confidence", 0),
                                "reason": (r.get("reason", "") or "")[:80],
                            }
                            break
                    if signal:
                        break
            except Exception:
                pass

        enriched.append({
            "symbol": sym,
            "name": name,
            "cost_price": cost,
            "price": price,
            "quantity": qty,
            "market_value": mv,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "strategy": strategy,
            "price_source": source,
            "signal": signal,
        })

    return {
        "status": "ok",
        "data": {
            "holdings": enriched,
            "total_value": round(total_value, 2),
            "total_pnl": round(total_pnl, 2),
        }
    }


@router.post("/api/holdings")
async def add_holding(req: HoldingIn):
    """添加持仓."""
    holdings = _load_holdings()
    # dedup
    for h in holdings:
        if h.get("symbol") == req.symbol:
            raise HTTPException(status_code=400, detail=f"已存在 {req.symbol}，请使用编辑")
    holdings.append(req.model_dump())
    _save_holdings(holdings)
    logger.info(f"Holding added: {req.symbol} {req.name}")
    return {"status": "ok", "message": "已添加", "count": len(holdings)}


@router.put("/api/holdings/{symbol}")
async def update_holding(symbol: str, req: HoldingIn):
    """编辑持仓."""
    holdings = _load_holdings()
    found = False
    for h in holdings:
        if h.get("symbol") == symbol:
            h.update(req.model_dump())
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="未找到该持仓")
    _save_holdings(holdings)
    logger.info(f"Holding updated: {symbol}")
    return {"status": "ok", "message": "已更新"}


@router.delete("/api/holdings/{symbol}")
async def delete_holding(symbol: str):
    """删除持仓."""
    holdings = _load_holdings()
    before = len(holdings)
    holdings = [h for h in holdings if h.get("symbol") != symbol]
    if len(holdings) == before:
        raise HTTPException(status_code=404, detail="未找到该持仓")
    _save_holdings(holdings)
    logger.info(f"Holding deleted: {symbol}")
    return {"status": "ok", "message": "已删除", "count": len(holdings)}
