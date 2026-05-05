"""Sancai (三才) execution flow API endpoints."""
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from fastapi import APIRouter, HTTPException

router = APIRouter()

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"
CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"


def load_universe():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("universe", [])


@router.get("/status")
async def get_sancai_status():
    """
    Get current 三才 (San Cai) execution status:
    - 天道 (Heaven/Tiandao): Macro market timing assessment
    - 地道 (Earth/Didao): Stock selection/filtering
    - 人道 (Human/Rendao): Trade execution/position tracking
    """
    universe = load_universe()

    # 天道: Market assessment based on available data
    tiandao = _assess_tiandao()

    # 地道: Filter stocks with available data
    didao = _assess_didao(universe)

    # 人道: Execution status (empty for now, no live positions)
    rendao = _assess_rendao()

    return {
        "tiandao": tiandao,
        "didao": didao,
        "rendao": rendao,
        "timestamp": datetime.now().isoformat(),
    }


def _assess_tiandao() -> dict:
    """天道: Assess macro market conditions.
    Uses available data to gauge overall market trend.
    """
    # Try to assess market using Shanghai/Shenzhen index proxies
    # For now, use available data from universe stocks
    assessment = "平"  # Default: neutral
    market_trend = "neutral"
    details = []

    total_uptrend = 0
    total_stocks = 0

    for stock in load_universe():
        symbol = stock["symbol"]
        fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
        if fpath.exists():
            try:
                df = pd.read_parquet(fpath)
                close = df["close"].values
                if len(close) > 50:
                    # Simple trend: compare 5-day to 50-day average
                    short_ma = close[-5:].mean()
                    long_ma = close[-50:].mean()
                    if short_ma > long_ma * 1.02:
                        total_uptrend += 1
                    total_stocks += 1
            except Exception:
                pass

    if total_stocks > 0:
        up_ratio = total_uptrend / total_stocks
        if up_ratio > 0.6:
            assessment = "吉"
            market_trend = "bull"
            details.append(f"多数股票({total_uptrend}/{total_stocks})处于上升趋势")
        elif up_ratio < 0.3:
            assessment = "凶"
            market_trend = "bear"
            details.append(f"多数股票仅{total_uptrend}/{total_stocks}处于上升趋势")
        else:
            assessment = "平"
            market_trend = "neutral"
            details.append(f"市场分化({total_uptrend}/{total_stocks})处于上升趋势")

    return {
        "assessment": assessment,
        "market_trend": market_trend,
        "sentiment": "neutral",
        "details": details,
    }


def _assess_didao(universe: list) -> dict:
    """地道: Stock selection and fundamental filtering."""
    qualified = []
    for stock in universe:
        symbol = stock["symbol"]
        name = stock["name"]
        fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
        if fpath.exists():
            df = pd.read_parquet(fpath)
            close = df["close"].values
            if len(close) > 50:
                latest = close[-1]
                ma21 = close[-21:].mean()
                ma55 = close[-55:].mean() if len(close) >= 55 else ma21

                # Simple scoring
                score = 50
                if latest > ma21:
                    score += 15
                if latest > ma55:
                    score += 15
                if close[-5:].mean() > close[-20:].mean():
                    score += 10
                # Volume check
                if "volume" in df.columns and len(df) > 20:
                    recent_vol = df["volume"].values[-5:].mean()
                    prev_vol = df["volume"].values[-20:-5].mean()
                    if recent_vol > prev_vol * 1.1:
                        score += 10

                qualified.append({
                    "symbol": symbol,
                    "name": name,
                    "score": min(score, 100),
                    "latest_price": float(latest),
                    "ma21": float(ma21),
                    "ma55": float(ma55),
                    "data_bars": len(df),
                })

    qualified.sort(key=lambda x: x["score"], reverse=True)

    return {
        "filtered_count": len(qualified),
        "top_picks": qualified[:5],
        "all_stocks": qualified,
    }


def _assess_rendao() -> dict:
    """人道: Trade execution status."""
    return {
        "current_positions": 0,
        "today_signals": 0,
        "pending_actions": [],
        "execution_log": [],
        "note": "实时交易功能开发中 / Live trading under development",
    }
