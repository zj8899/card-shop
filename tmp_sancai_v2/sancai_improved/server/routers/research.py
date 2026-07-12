"""Research API — Market regime, emotion, money flow, and explainability engines.

Exposes the research/ engine pipeline as HTTP endpoints so the frontend can:
  - Read market regime classification
  - Read emotion index and hotspot lifecycle
  - Read capital behavior (money flow) per symbol
  - Get 5-point AI explanations per symbol
"""
import logging

import pandas as pd
from fastapi import APIRouter, Query, HTTPException

from server.utils import DATA_DIR
from server.utils.response import ok

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Lazy-import research modules so the server starts even without lightgbm ──
_research_available: bool | None = None


def _check_research() -> bool:
    """Check whether research modules can be imported."""
    global _research_available
    if _research_available is not None:
        return _research_available
    try:
        from research.factors import (  # noqa: F401
            MADeviationFactor, KDJFactor, RSIFactor,
            VolumeRatioFactor, TrendStrengthFactor, ATRFactor,
            StopLossClusterFactor, LiquidityDensityFactor,
            OrderImbalanceFactor, MicroMomentumFactor,
            TurnoverVolatilityFactor, PricePositionAsymmetryFactor,
            NewsSentimentFactor, AnalystRevisionFactor,
            NorthboundFlowFactor, LimitUpHeatFactor,
            LimitUpRatioFactor, LimitDownRatioFactor,
            BreakBoardRateFactor, StreakMomentumFactor,
            ProfitEffectIndexFactor, SectorRotationSpeedFactor,
        )
        _research_available = True
    except ImportError as e:
        logger.warning("Research modules unavailable: %s", e)
        _research_available = False
    return _research_available


def _get_all_factor_classes() -> list:
    """Return all available AlphaFactor subclasses."""
    from research.factors import (
        MADeviationFactor, KDJFactor, RSIFactor,
        VolumeRatioFactor, TrendStrengthFactor, ATRFactor,
        StopLossClusterFactor, LiquidityDensityFactor,
        OrderImbalanceFactor, MicroMomentumFactor,
        TurnoverVolatilityFactor, PricePositionAsymmetryFactor,
        NewsSentimentFactor, AnalystRevisionFactor,
        NorthboundFlowFactor, LimitUpHeatFactor,
        LimitUpRatioFactor, LimitDownRatioFactor,
        BreakBoardRateFactor, StreakMomentumFactor,
        ProfitEffectIndexFactor, SectorRotationSpeedFactor,
    )
    return [
        MADeviationFactor, KDJFactor, RSIFactor,
        VolumeRatioFactor, TrendStrengthFactor, ATRFactor,
        StopLossClusterFactor, LiquidityDensityFactor,
        OrderImbalanceFactor, MicroMomentumFactor,
        TurnoverVolatilityFactor, PricePositionAsymmetryFactor,
        NewsSentimentFactor, AnalystRevisionFactor,
        NorthboundFlowFactor, LimitUpHeatFactor,
        LimitUpRatioFactor, LimitDownRatioFactor,
        BreakBoardRateFactor, StreakMomentumFactor,
        ProfitEffectIndexFactor, SectorRotationSpeedFactor,
    ]


def _load_daily_data(symbol: str, limit: int = 500) -> pd.DataFrame | None:
    """Load daily OHLCV data for a single symbol."""
    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        return None
    df = pd.read_parquet(fpath).tail(limit)
    if len(df) < 60:
        return None
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# Engine endpoints — Market Regime, Emotion, Money Flow, Explain
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/engines/regime")
async def get_regime(
    symbol: str = Query("000001", description="Reference stock (uses its factor data)"),
):
    """Detect current market regime using rule-based classifier.

    Returns: label, confidence, risk_level, suggested_position_pct,
             recommended_strategies, explanation, all_scores.
    """
    if not _check_research():
        raise HTTPException(status_code=503, detail="Research modules not available")

    from research.engines.regime import MarketRegimeClassifier

    df = _load_daily_data(symbol)
    if df is None:
        # Return a default neutral result
        return ok({
            "label": "修复行情",
            "confidence": 0.3,
            "risk_level": 3,
            "suggested_position_pct": 0.4,
            "recommended_strategies": ["simple", "strict"],
            "explanation": "数据不足，默认为修复行情。请确保数据已下载。",
            "all_scores": {},
            "source": "fallback",
        })

    classifier = MarketRegimeClassifier()
    result = classifier.classify_from_df(df)

    return ok({
        "label": result.label,
        "confidence": result.confidence,
        "risk_level": result.risk_level,
        "suggested_position_pct": result.suggested_position_pct,
        "recommended_strategies": result.recommended_strategies,
        "explanation": result.explanation,
        "all_scores": result.all_scores,
        "timestamp": result.timestamp,
        "source": "rule_based",
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Emotion Engine endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/engines/emotion/index")
async def get_emotion_index():
    """Compute market emotion index (0-100) from available sentiment data."""
    if not _check_research():
        raise HTTPException(status_code=503, detail="Research modules not available")

    from research.engines.emotion import compute_emotion_index

    # Attempt to fetch real market sentiment data
    data_quality = "fallback"
    factors = {}
    hotspot_count = 0
    strongest_strength = 0.0

    try:
        from research.factors.sentiment import fetch_today_sentiment_snapshot

        snapshot = fetch_today_sentiment_snapshot()
        data_quality = "live"

        # Map snapshot fields to emotion calculator factor keys
        total_stocks = 5000  # approximate total A-share count
        factors["limit_up_ratio"] = snapshot.get("limit_up_count", 0) / max(total_stocks, 1)
        factors["limit_down_ratio"] = snapshot.get("limit_down_count", 0) / max(total_stocks, 1)

        # Break board rate: (touched - sealed) / touched
        touched = snapshot.get("touched_limit_up", 0)
        sealed = snapshot.get("sealed_limit_up", 0)
        factors["break_board_rate"] = (touched - sealed) / max(touched, 1) if touched > 0 else 0.0

        # Streak momentum: average of 2to3 and 3to4 rates
        factors["streak_momentum"] = (
            snapshot.get("streak_2to3", 0.0) + snapshot.get("streak_3to4", 0.0)
        ) / 2

        # Profit effect: composite of advance/decline ratio
        adv = snapshot.get("advance_count", 0)
        dec = snapshot.get("decline_count", 1)
        factors["profit_effect_index"] = adv / max(adv + dec, 1) if adv + dec > 0 else 0.5

        # Hotspot data from snapshot
        hotspot_count = sum(1 for k, v in snapshot.items()
                            if isinstance(v, (int, float)) and v > 0
                            and k not in ("timestamp", "advance_count", "decline_count", "touched_limit_up", "sealed_limit_up"))

        # Check if data is stale (snapshot hasn't been updated today)
        from datetime import datetime
        ts = snapshot.get("timestamp", "")
        if ts:
            try:
                snap_date = datetime.fromisoformat(ts).date()
                if snap_date < datetime.now().date():
                    data_quality = "stale"
            except Exception:
                pass

    except ImportError:
        logger.info("Sentiment snapshot unavailable (akshare not installed). Using defaults.")
        data_quality = "fallback"
    except Exception as e:
        logger.warning(f"Failed to fetch sentiment snapshot: {e}")
        data_quality = "fallback"

    result = compute_emotion_index(factors, hotspot_count=hotspot_count, strongest_strength=strongest_strength)
    return ok({
        "value": result.value,
        "label": result.label,
        "components": result.components,
        "timestamp": result.timestamp,
        "data_quality": data_quality,
        "note": "Live sentiment data from akshare snapshot." if data_quality == "live"
                else "Sentiment snapshot is from a previous session." if data_quality == "stale"
                else "Live sentiment data requires daily pipeline (fetch_today_sentiment_snapshot). Currently using defaults.",
    })


@router.get("/engines/emotion/hotspots")
async def get_hotspots():
    """Return all active concept hotspots with lifecycle phases.

    Uses MainlineDetector + ConceptEnricher infrastructure.
    """
    if not _check_research():
        raise HTTPException(status_code=503, detail="Research modules not available")

    from research.engines.emotion import analyze_hotspots

    # Build sample hotspot data from MainlineDetector
    hotspots_data = {}

    try:
        from server.services.mainline_detector import MainlineDetector
        from server.services.concept_enricher import ConceptEnricher
        from data_sources.ths_hot import get_topic_heatmap

        # Get today's topic heatmap
        topic_map = {}
        try:
            topic_map = get_topic_heatmap()
        except Exception:
            pass

        # Build per-concept data
        for concept, count in topic_map.items():
            hotspots_data[concept] = {
                "limit_up_count": count,
                "sector_change_pct": 0.0,
                "total_matched": count,
                "leading_stocks": [],
            }
    except ImportError:
        pass

    # If no live data, provide example data
    data_quality = "live"
    if not hotspots_data:
        data_quality = "fallback"
        hotspots_data = {
            "机器人": {"limit_up_count": 5, "sector_change_pct": 3.2, "total_matched": 8, "leading_stocks": []},
            "AI算力": {"limit_up_count": 3, "sector_change_pct": 2.1, "total_matched": 5, "leading_stocks": []},
            "新能源": {"limit_up_count": 1, "sector_change_pct": 0.5, "total_matched": 2, "leading_stocks": []},
        }

    hotspots = analyze_hotspots(hotspots_data)
    return ok({
        "data_quality": data_quality,
        "hotspot_count": len(hotspots),
        "hotspots": [
            {
                "concept": h.concept,
                "phase": h.phase,
                "phase_confidence": h.phase_confidence,
                "leading_stock": h.leading_stock,
                "leading_stock_name": h.leading_stock_name,
                "followers": h.followers,
                "strength_score": h.strength_score,
                "duration_days": h.duration_days,
                "daily_limit_up_count": h.daily_limit_up_count,
                "sector_change_pct": h.sector_change_pct,
                "next_phase_probability": h.next_phase_probability,
                "phase_history": h.history,
            }
            for h in hotspots
        ],
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Money Flow Engine endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/engines/money-flow/{symbol}")
async def get_money_flow(symbol: str):
    """Analyze capital behavior patterns for a single stock."""
    if not _check_research():
        raise HTTPException(status_code=503, detail="Research modules not available")

    from research.engines.money_flow import analyze_money_flow

    df = _load_daily_data(symbol)
    if df is None:
        raise HTTPException(status_code=404, detail=f"No data for {symbol}")

    report = analyze_money_flow(symbol, df)
    return ok({
        "symbol": report.symbol,
        "current_phase": report.current_phase,
        "phase_sequence": report.phase_sequence,
        "confidence": report.confidence,
        "money_flow_score": report.money_flow_score,
        "recent_signals": [
            {
                "date": s.date,
                "behavior": s.behavior_label,
                "strength": s.strength,
                "price": s.price,
                "volume_ratio": s.volume_ratio,
                "description": s.description,
            }
            for s in report.recent_signals[-15:]
        ],
        "summary": report.summary,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Explain endpoint
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/engines/explain/{symbol}")
async def explain_decision(
    symbol: str,
    name: str = Query(""),
):
    """Generate a structured 5-point AI explanation for a stock.

    Integrates regime, money_flow, and factor data.
    """
    if not _check_research():
        raise HTTPException(status_code=503, detail="Research modules not available")

    from research.engines.explainer import explain_decision as _explain
    from research.engines.regime import MarketRegimeClassifier
    from research.engines.money_flow import analyze_money_flow

    df = _load_daily_data(symbol)
    if df is None:
        raise HTTPException(status_code=404, detail=f"No data for {symbol}")

    # Gather engine outputs
    regime_data = None
    try:
        classifier = MarketRegimeClassifier()
        result = classifier.classify_from_df(df)
        regime_data = {"label": result.label, "risk_level": result.risk_level,
                       "explanation": result.explanation}
    except Exception:
        pass

    mf_data = None
    try:
        mf_report = analyze_money_flow(symbol, df)
        mf_data = {"current_phase": mf_report.current_phase,
                   "money_flow_score": mf_report.money_flow_score}
    except Exception:
        pass

    factor_data = None
    try:
        factor_classes = _get_all_factor_classes()
        for cls in factor_classes:
            inst = cls()
            col = inst.name
            if col in df.columns:
                if factor_data is None:
                    factor_data = {}
                factor_data[col] = float(df[col].iloc[-1]) if not pd.isna(df[col].iloc[-1]) else 0.0
    except Exception:
        pass

    response = _explain(
        symbol=symbol, name=name,
        regime=regime_data, money_flow=mf_data, factor=factor_data,
    )

    return ok({
        "symbol": symbol,
        "name": name or symbol,
        "action": response.score.action,
        "composite_score": response.score.composite_score,
        "risk_level": response.score.risk_level,
        "recommendation_pct": response.score.recommendation_pct,
        "reasons": {
            "market": response.score.reason_market,
            "sector": response.score.reason_sector,
            "capital": response.score.reason_capital,
            "technical": response.score.reason_technical,
            "event": response.score.reason_event,
        },
        "risks": response.score.risks,
        "summary": response.summary,
        "detailed": response.detailed,
    })
