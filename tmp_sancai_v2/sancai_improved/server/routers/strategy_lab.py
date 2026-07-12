"""Strategy Lab API — factor composition, batch evaluation, recommendations.

白皮书 Section 6 + P12: 因子自由组合 → 自动回测 → IC分析 → 排序推荐
"""
import logging
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from server.utils import DATA_DIR
from server.utils.response import ok, err

logger = logging.getLogger(__name__)

router = APIRouter()


class ComposeRequest(BaseModel):
    name: str = "my_combo"
    factors: list[str] = ["trend_strength", "kdj_9_3", "vol_ratio_20"]
    weights: dict[str, float] = {}
    entry_threshold: float = 0.5
    exit_threshold: float = -0.3
    description: str = ""


class EvaluateRequest(BaseModel):
    symbol: str = "000001"
    factors_list: list[list[str]] = []  # [[f1,f2], [f3,f4,f5], ...]
    metric: str = "sharpe_ratio"
    top_n: int = 10


# ═══════════════════════════════════════════════════════════════════════════════
# Factor catalog
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/lab/factors")
async def get_factor_catalog():
    """Return all 22 available factors with categories and descriptions."""
    from backtest.strategies.lab.composer import StrategyComposer
    catalog = StrategyComposer.get_factor_catalog()
    categories = {}
    for item in catalog:
        cat = item["category_label"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(item)
    return ok({
        "total": len(catalog),
        "categories": categories,
        "catalog": catalog,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Evaluate
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/lab/evaluate")
async def evaluate_combos(req: EvaluateRequest):
    """Batch evaluate factor combinations and rank by metric.

    Supports: sharpe_ratio, total_return, win_rate, profit_factor, ic.
    """
    from backtest.strategies.lab.composer import FactorCombo
    from backtest.strategies.lab.evaluator import LabEvaluator

    fpath = DATA_DIR / "daily" / f"{req.symbol}.parquet"
    if not fpath.exists():
        raise HTTPException(status_code=404, detail=f"No data for {req.symbol}")

    df = pd.read_parquet(fpath)
    if len(df) < 100:
        raise HTTPException(status_code=400, detail=f"Only {len(df)} bars, need 100+")

    # Build combos
    combos = []
    if req.factors_list:
        for factors in req.factors_list:
            combos.append(FactorCombo(
                name="+".join(factors[:3]),
                factors=factors,
                weights={f: 1.0 for f in factors},
            ))
    else:
        # Auto-generate top single-factor + cross-category combos
        from backtest.strategies.lab.composer import StrategyComposer
        combos = StrategyComposer.auto_generate_combos(max_combos=30)

    import asyncio

    evaluator = LabEvaluator(df)
    evaluator.symbol = req.symbol
    results = await asyncio.to_thread(
        evaluator.recommend, combos, metric=req.metric, top_n=req.top_n
    )

    return ok({
        "symbol": req.symbol,
        "metric": req.metric,
        "combos_evaluated": len(combos),
        "data_bars": len(df),
        "results": results,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Save strategy from lab result
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/lab/save-strategy")
async def save_lab_strategy(req: ComposeRequest):
    """Save a lab-generated combo as a persistent user strategy."""
    from backtest.strategies.registry import register_user_strategy

    # Generate strategy code
    code = f'''"""Auto-generated strategy from Strategy Lab: {req.name}."""
from backtest.strategies.interface import IStrategy, Signal, SignalType, BarContext
from typing import Optional
import pandas as pd

class {req.name.title().replace("_", "")}Strategy(IStrategy):
    name = "{req.name}"
    factors = {req.factors}
    weights = {req.weights or {f: 1.0 for f in req.factors}}
    entry_threshold = {req.entry_threshold}
    exit_threshold = {req.exit_threshold}

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        try:
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
            all_factors = [
                MADeviationFactor(), KDJFactor(), RSIFactor(),
                VolumeRatioFactor(), TrendStrengthFactor(), ATRFactor(),
                StopLossClusterFactor(), LiquidityDensityFactor(),
                OrderImbalanceFactor(), MicroMomentumFactor(),
                TurnoverVolatilityFactor(), PricePositionAsymmetryFactor(),
                NewsSentimentFactor(), AnalystRevisionFactor(),
                NorthboundFlowFactor(), LimitUpHeatFactor(),
                LimitUpRatioFactor(), LimitDownRatioFactor(),
                BreakBoardRateFactor(), StreakMomentumFactor(),
                ProfitEffectIndexFactor(), SectorRotationSpeedFactor(),
            ]
            for fi in all_factors:
                if fi.name in self.factors:
                    df[fi.name] = fi.calculate(df)
        except ImportError:
            pass
        return df

    def _composite_score(self, factor_values):
        score = 0.0
        for f in self.factors:
            w = self.weights.get(f, 1.0)
            score += factor_values.get(f, 0.0) * w
        return score / max(len(self.factors), 1)

    def populate_entry_signals(self, ctx: BarContext) -> Optional[Signal]:
        vals = getattr(ctx, 'factor_values', {{}})
        score = self._composite_score(vals)
        if score > self.entry_threshold:
            return Signal(type=SignalType.BUY, reason=f"Lab评分 {{score:.3f}}", price=ctx.price, confidence=min(abs(score), 1.0))
        return None

    def populate_exit_signals(self, ctx: BarContext) -> Optional[Signal]:
        if not ctx.in_position: return None
        vals = getattr(ctx, 'factor_values', {{}})
        score = self._composite_score(vals)
        if score < self.exit_threshold:
            return Signal(type=SignalType.SELL, reason=f"Lab评分下降 {{score:.3f}}", price=ctx.price, confidence=min(abs(score), 1.0))
        if ctx.entry_price > 0 and ctx.price < ctx.entry_price * 0.95:
            return Signal(type=SignalType.SELL, reason="硬止损 -5%", price=ctx.price, confidence=1.0)
        return None
'''

    key = register_user_strategy(req.name, code)
    return ok({
        "name": req.name,
        "registry_key": key,
        "factors": req.factors,
        "saved": True,
        "message": f"策略 {req.name} 已保存到 user_generated/ 并注册为 {key}",
    })
