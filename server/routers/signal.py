"""Signal query API endpoints."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"


@router.get("/trend/{symbol}")
async def get_trend(
    symbol: str,
    period: str = Query("daily"),
):
    """Get current MA trend status for a symbol."""
    if period == "daily":
        fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    else:
        fpath = DATA_DIR / "minute" / period / f"{symbol}.parquet"

    if not fpath.exists():
        raise HTTPException(status_code=404, detail=f"No data for {symbol}")

    df = pd.read_parquet(fpath)
    close = df["close"].values

    # Get trends from Rust core
    try:
        import sancai_core
        ohlcv_data = {
            "open": df["open"].tolist(),
            "high": df["high"].tolist(),
            "low": df["low"].tolist(),
            "close": close.tolist(),
            "volume": df["volume"].tolist(),
        }
        result_str = sancai_core.analyze_trends(json.dumps(ohlcv_data), period)
        result = json.loads(result_str)
        trends = result.get("trends", [])
    except Exception:
        trends = []

    # Get latest trend for each period
    latest_trends = {}
    for t in trends:
        period_key = str(t["period"])
        if period_key not in latest_trends or t["index"] > latest_trends[period_key]["index"]:
            latest_trends[period_key] = t

    return {
        "symbol": symbol,
        "period": period,
        "latest_price": float(close[-1]) if len(close) > 0 else None,
        "trends": latest_trends,
        "data_points": len(close),
    }


@router.get("/latest/{symbol}")
async def get_latest_signals(
    symbol: str,
    limit: int = Query(20),
):
    """Get latest generated signals for a symbol."""
    return {
        "symbol": symbol,
        "signals": [],
        "note": "Signals are generated during backtest runs. Run a backtest to see signals.",
    }


@router.get("/elliott/{symbol}")
async def get_elliott_wave(symbol: str):
    """Get Elliott Wave analysis for a symbol."""
    from backtest.elliott_wave import analyze_wave_structure

    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        raise HTTPException(status_code=404, detail=f"No data for {symbol}")

    import pandas as pd
    df = pd.read_parquet(fpath)
    result = analyze_wave_structure(symbol, df)
    return result


@router.get("/schools")
async def list_trading_schools():
    """List available trading schools."""
    from backtest.schools import list_schools, SCHOOLS
    return {
        "schools": [
            {"id": sid, "name": cls.name, "description": cls.description}
            for sid, cls in SCHOOLS.items()
        ]
    }


@router.get("/schools/{school_id}/signals/{symbol}")
async def get_school_signals(school_id: str, symbol: str):
    """Get signals from a specific trading school for a symbol."""
    from backtest.schools import get_school

    school_cls = get_school(school_id)
    if school_cls is None:
        raise HTTPException(status_code=404, detail=f"School '{school_id}' not found")

    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        raise HTTPException(status_code=404, detail=f"No data for {symbol}")

    import pandas as pd
    df = pd.read_parquet(fpath)
    school = school_cls()
    signals_df = school.generate_signals(df)

    if len(signals_df) > 0:
        import numpy as np
        # Clean NaN/Inf for JSON (Starlette uses allow_nan=False)
        clean = signals_df.astype(object)
        clean = clean.where(clean.notna(), None)
        records = clean.to_dict(orient="records")
        # Also catch inf values
        for r in records:
            for k, v in list(r.items()):
                if isinstance(v, float):
                    if np.isnan(v) or np.isinf(v):
                        r[k] = None
    else:
        records = []

    return {
        "school": school_id,
        "name": school.name,
        "symbol": symbol,
        "signal_count": len(signals_df),
        "signals": records,
    }
