"""Data management API endpoints."""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"
CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"
META_DB = PROJECT_ROOT / "data" / "metadata.db"


def load_universe():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("universe", [])


@router.get("/stocks")
async def list_stocks():
    """List available stocks and their data status."""
    universe = load_universe()
    stocks = []
    for s in universe:
        symbol = s["symbol"]
        name = s["name"]
        daily_path = DATA_DIR / "daily" / f"{symbol}.parquet"
        stocks.append({
            "symbol": symbol,
            "name": name,
            "has_daily": daily_path.exists(),
            "daily_bars": len(pd.read_parquet(daily_path)) if daily_path.exists() else 0,
        })
    return {"stocks": stocks, "count": len(stocks)}


@router.get("/stocks/{symbol}/kline")
async def get_kline(
    symbol: str,
    period: str = Query("daily", description="daily, 30min, 60min, etc."),
    start: str = None,
    end: str = None,
    limit: int = Query(500, description="Max bars to return"),
):
    """Get K-line data with computed indicators for a symbol."""
    if period == "daily":
        fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    else:
        fpath = DATA_DIR / "minute" / period / f"{symbol}.parquet"

    if not fpath.exists():
        raise HTTPException(status_code=404, detail=f"No data for {symbol} ({period})")

    df = pd.read_parquet(fpath)
    date_col = "date" if period == "daily" else "datetime"
    df = df.sort_values(date_col)

    if start:
        df = df[df[date_col] >= pd.Timestamp(start)]
    if end:
        df = df[df[date_col] <= pd.Timestamp(end)]
    if limit and len(df) > limit:
        df = df.tail(limit)

    # Compute MAs and indicators via Rust core if available
    try:
        import sancai_core
        ohlcv_data = {
            "open": df["open"].tolist(),
            "high": df["high"].tolist(),
            "low": df["low"].tolist(),
            "close": df["close"].tolist(),
            "volume": df["volume"].tolist(),
        }
        result_str = sancai_core.analyze_trends(json.dumps(ohlcv_data), period)
        indicators = json.loads(result_str)
    except Exception:
        # Python fallback
        close = df["close"].values
        periods = [34, 144, 233] if period != "daily" else [5, 13, 21, 34, 55, 144, 233, 623]
        indicators = {"mas": {}}
        for p in periods:
            ma_vals = pd.Series(close).rolling(window=p).mean().tolist()
            indicators["mas"][str(p)] = [None if pd.isna(v) else v for v in ma_vals]
        indicators["trends"] = []
        indicators["kdj"] = {"k": [], "d": [], "j": []}

    # Build response
    records = df.to_dict(orient="records")
    for i, rec in enumerate(records):
        rec["date"] = str(rec.get(date_col, ""))[:19]
        for p_str, ma_vals in indicators.get("mas", {}).items():
            if i < len(ma_vals) and ma_vals[i] is not None:
                rec[f"ma_{p_str}"] = round(ma_vals[i], 4)
        if i < len(indicators.get("kdj", {}).get("k", [])):
            k = indicators["kdj"]["k"][i]
            rec["kdj_k"] = round(k, 2) if k is not None else None
            d = indicators["kdj"]["d"][i]
            rec["kdj_d"] = round(d, 2) if d is not None else None
            j = indicators["kdj"]["j"][i]
            rec["kdj_j"] = round(j, 2) if j is not None else None

    return {
        "symbol": symbol,
        "period": period,
        "count": len(records),
        "data": records,
    }


def _symbol_prefix(symbol: str) -> str:
    """Convert symbol to akshare tick format: 000001→sz000001, 600519→sh600519."""
    if symbol.startswith(('6', '9')):
        return 'sh' + symbol
    return 'sz' + symbol


@router.get("/stocks/{symbol}/depth")
async def get_depth(symbol: str):
    """Get 5-level bid/ask depth for a symbol using akshare."""
    try:
        import akshare as ak
        df = ak.stock_bid_ask_em(symbol=symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取盘口失败: {e}")

    # Parse item/value structure
    raw = {}
    for _, row in df.iterrows():
        raw[row['item']] = row['value']

    # Extract 5-level depth
    def level(n, side):
        price = raw.get(f'{side}_{n}')
        vol = raw.get(f'{side}_{n}_vol')
        if price is None or vol is None:
            return None
        return {'price': float(price), 'volume': int(float(vol))}

    sells = [level(i, 'sell') for i in range(5, 0, -1)]
    buys = [level(i, 'buy') for i in range(1, 6)]
    sells = [s for s in sells if s]
    buys = [b for b in buys if b]

    spread = None
    spread_pct = None
    if sells and buys:
        spread = float(sells[-1]['price'] - buys[0]['price'])
        if buys[0]['price'] > 0:
            spread_pct = round(spread / buys[0]['price'] * 100, 3)

    return {
        'symbol': symbol,
        'sells': sells,  # 5→1, nearest last
        'buys': buys,    # 1→5, nearest first
        'spread': spread,
        'spread_pct': spread_pct,
        'prev_close': raw.get('昨收'),
        'limit_up': raw.get('涨停'),
        'limit_down': raw.get('跌停'),
        'total_volume': raw.get('总量'),
        'total_amount': raw.get('金额'),
    }


@router.get("/stocks/{symbol}/ticks")
async def get_ticks(symbol: str, limit: int = None):
    """Get tick-by-tick transactions with active buy/sell classification."""
    full_symbol = _symbol_prefix(symbol)

    try:
        import akshare as ak
        df = ak.stock_zh_a_tick_tx_js(symbol=full_symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取逐笔成交失败: {e}")

    if df.empty:
        return {
            'symbol': symbol,
            'summary': None,
            'recent_ticks': [],
            'big_trades': [],
            'note': '暂无逐笔成交数据',
        }

    # Column mapping (Chinese to English)
    col_map = {c: c for c in df.columns}
    time_col = df.columns[0]
    price_col = df.columns[1]
    change_col = df.columns[2]
    vol_col = df.columns[3]
    amount_col = df.columns[4]
    nature_col = df.columns[5]

    n = limit or min(100, len(df))
    recent = df.tail(n)

    # Classification
    buy_mask = recent[nature_col] == '买盘'
    sell_mask = recent[nature_col] == '卖盘'

    buy_cnt = int(buy_mask.sum())
    sell_cnt = int(sell_mask.sum())
    buy_vol = float(recent.loc[buy_mask, vol_col].sum()) if buy_cnt > 0 else 0.0
    sell_vol = float(recent.loc[sell_mask, vol_col].sum()) if sell_cnt > 0 else 0.0
    buy_amt = float(recent.loc[buy_mask, amount_col].sum()) if buy_cnt > 0 else 0.0
    sell_amt = float(recent.loc[sell_mask, amount_col].sum()) if sell_cnt > 0 else 0.0

    total_amt = buy_amt + sell_amt
    buy_pct = round(buy_amt / total_amt * 100, 1) if total_amt > 0 else 0

    # Big trades (>= 100 lots = 10000 shares)
    big = df[df[vol_col] >= 100].tail(30)
    big_trades = []
    for _, row in big.iterrows():
        big_trades.append({
            'time': str(row[time_col]),
            'price': float(row[price_col]),
            'volume': int(row[vol_col]),
            'amount': float(row[amount_col]),
            'type': str(row[nature_col]),
        })

    # Recent ticks
    recent_ticks = []
    for _, row in recent.iterrows():
        recent_ticks.append({
            'time': str(row[time_col]),
            'price': float(row[price_col]),
            'change': float(row[change_col]) if row[change_col] else 0,
            'volume': int(row[vol_col]),
            'amount': float(row[amount_col]),
            'type': str(row[nature_col]),
        })

    return {
        'symbol': symbol,
        'summary': {
            'buy_count': buy_cnt,
            'buy_volume': int(buy_vol),
            'buy_amount': round(buy_amt, 2),
            'sell_count': sell_cnt,
            'sell_volume': int(sell_vol),
            'sell_amount': round(sell_amt, 2),
            'net_flow': round(buy_amt - sell_amt, 2),
            'buy_pct': buy_pct,
            'total_count': len(recent),
        },
        'recent_ticks': recent_ticks,
        'big_trades': big_trades,
    }


@router.get("/gaps")
async def check_gaps():
    """Check for data gaps."""
    conn = sqlite3.connect(str(META_DB))
    try:
        cursor = conn.execute(
            "SELECT symbol, name, period, start_date, end_date, row_count FROM data_catalog"
        )
        gaps = []
        for row in cursor:
            gaps.append({
                "symbol": row[0], "name": row[1], "period": row[2],
                "start_date": row[3], "end_date": row[4], "row_count": row[5],
            })
        return {"gaps": gaps}
    finally:
        conn.close()
