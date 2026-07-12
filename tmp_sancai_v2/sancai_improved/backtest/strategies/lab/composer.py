"""Strategy Composer — assemble strategies from factor combinations.

自由组合22个因子 → 生成策略代码 → 注册到回测引擎。
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from ..interface import IStrategy, Signal, SignalType, BarContext

logger = logging.getLogger(__name__)

# ── All available factors for composition ──

ALL_FACTOR_DEFS = {
    # Technical (6) — all compute from OHLCV only
    "ma_dev_34":             {"category": "technical", "label": "MA偏离度", "desc": "价格距离MA34的偏离程度", "needs_pipeline": False},
    "kdj_9_3":               {"category": "technical", "label": "KDJ信号", "desc": "KDJ(9,3,3)超买超卖", "needs_pipeline": False},
    "rsi_14":                {"category": "technical", "label": "RSI动量", "desc": "14周期RSI多空力度", "needs_pipeline": False},
    "vol_ratio_20":          {"category": "technical", "label": "量比", "desc": "成交量相对20日均量倍数", "needs_pipeline": False},
    "trend_strength":        {"category": "technical", "label": "趋势强度", "desc": "多均线排列得分", "needs_pipeline": False},
    "atr_14":                {"category": "technical", "label": "波动率", "desc": "14周期ATR波动率", "needs_pipeline": False},
    # Microstructure (6) — all compute from OHLCV only
    "stop_loss_cluster":     {"category": "microstructure", "label": "止损聚集度", "desc": "关键价位附近止损单密度", "needs_pipeline": False},
    "liquidity_density":     {"category": "microstructure", "label": "流动性密度", "desc": "筹码分布成交量积分", "needs_pipeline": False},
    "order_imbalance":       {"category": "microstructure", "label": "订单失衡", "desc": "买卖盘口压力不对称", "needs_pipeline": False},
    "micro_momentum":        {"category": "microstructure", "label": "微观动量加速度", "desc": "短期价格冲力的二阶导数", "needs_pipeline": False},
    "turnover_volatility":   {"category": "microstructure", "label": "换手异动", "desc": "换手率异常波动检测", "needs_pipeline": False},
    "price_position_asymmetry": {"category": "microstructure", "label": "价格位置不对称", "desc": "价格相对VWAP偏移", "needs_pipeline": False},
    # Alternative (4) — need pre-populated data columns from pipeline
    "news_sentiment":        {"category": "alternative", "label": "新闻情绪", "desc": "NLP新闻多空情感 (需数据管道)", "needs_pipeline": True},
    "analyst_revision":      {"category": "alternative", "label": "分析师修正", "desc": "EPS/评级调整方向 (需数据管道)", "needs_pipeline": True},
    "northbound_flow":       {"category": "alternative", "label": "北向资金", "desc": "北向资金流入异常 (需数据管道)", "needs_pipeline": True},
    "limit_up_heat":         {"category": "alternative", "label": "涨停热度", "desc": "市场涨停板活跃度 (需数据管道)", "needs_pipeline": True},
    # Sentiment (6) — need pre-populated data columns from pipeline
    "limit_up_ratio":        {"category": "sentiment", "label": "涨停比", "desc": "全市场涨停家数占比 (需数据管道)", "needs_pipeline": True},
    "limit_down_ratio":      {"category": "sentiment", "label": "跌停比", "desc": "全市场跌停家数占比 (需数据管道)", "needs_pipeline": True},
    "break_board_rate":      {"category": "sentiment", "label": "炸板率", "desc": "封板质量指标 (需数据管道)", "needs_pipeline": True},
    "streak_momentum":       {"category": "sentiment", "label": "连板动能", "desc": "连板晋级率 (需数据管道)", "needs_pipeline": True},
    "profit_effect_index":   {"category": "sentiment", "label": "赚钱效应", "desc": "综合赚钱效应指数 (需数据管道)", "needs_pipeline": True},
    "sector_rotation_speed": {"category": "sentiment", "label": "轮动速度", "desc": "板块轮动快慢 (需数据管道)", "needs_pipeline": True},
}

CATEGORY_LABELS = {
    "technical": "技术因子",
    "microstructure": "微观结构",
    "alternative": "另类数据",
    "sentiment": "情绪因子",
}


@dataclass
class FactorCombo:
    """A combination of factors with weights."""
    name: str
    factors: list[str]           # factor column names
    weights: dict[str, float]    # factor_name → weight (-1 to +1)
    entry_threshold: float = 0.5
    exit_threshold: float = -0.3
    description: str = ""

    def composite_score(self, factor_values: dict[str, float]) -> float:
        """Compute weighted composite score from factor values."""
        if not self.factors:
            return 0.0
        total = 0.0
        for f in self.factors:
            val = factor_values.get(f, 0.0)
            w = self.weights.get(f, 1.0)
            total += val * w
        return total / max(len(self.factors), 1)


class StrategyComposer:
    """Build strategies from factor combinations."""

    @staticmethod
    def compose(combo: FactorCombo) -> type[IStrategy]:
        """Generate an IStrategy subclass from a FactorCombo."""

        class ComposedStrategy(IStrategy):
            name = combo.name

            def populate_indicators(self, df):
                df = df.copy()
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
                    if fi.name in combo.factors:
                        df[fi.name] = fi.calculate(df)
                return df

            def populate_entry_signals(self, ctx):
                vals = getattr(ctx, 'factor_values', {})
                if not vals:
                    # Fallback: try to compute factor values from available context
                    vals = {
                        'kdj_9_3': ctx.kdj_j,
                        'rsi_14': getattr(ctx, 'kdj_d', 50.0),
                        'volume_ratio': getattr(ctx, 'volume_ratio', 1.0),
                    }
                score = combo.composite_score(vals)
                if score > combo.entry_threshold:
                    return Signal(type=SignalType.BUY, reason=f"因子组合评分 {score:.3f}",
                                  price=ctx.price, confidence=min(abs(score), 1.0))
                return None

            def populate_exit_signals(self, ctx):
                if not ctx.in_position:
                    return None
                vals = getattr(ctx, 'factor_values', {})
                if not vals:
                    vals = {
                        'kdj_9_3': ctx.kdj_j,
                        'rsi_14': getattr(ctx, 'kdj_d', 50.0),
                        'volume_ratio': getattr(ctx, 'volume_ratio', 1.0),
                    }
                score = combo.composite_score(vals)
                if score < combo.exit_threshold:
                    return Signal(type=SignalType.SELL, reason=f"因子评分下降 {score:.3f}",
                                  price=ctx.price, confidence=min(abs(score), 1.0))
                if ctx.entry_price > 0 and ctx.price < ctx.entry_price * 0.95:
                    return Signal(type=SignalType.SELL, reason="硬止损 -5%", price=ctx.price, confidence=1.0)
                return None

        return ComposedStrategy

    @staticmethod
    def get_factor_catalog() -> list[dict]:
        """Return the full factor catalog for frontend display."""
        return [
            {"name": name, "category": info["category"],
             "category_label": CATEGORY_LABELS.get(info["category"], info["category"]),
             "label": info["label"], "desc": info["desc"],
             "needs_pipeline": info.get("needs_pipeline", False)}
            for name, info in ALL_FACTOR_DEFS.items()
        ]

    @staticmethod
    def auto_generate_combos(max_combos: int = 50) -> list[FactorCombo]:
        """Auto-generate factor combinations for exploration.

        Strategy: enumerate all 1-factor, 2-factor, and 3-factor combos
        across different categories, capped at max_combos.
        """
        factor_names = list(ALL_FACTOR_DEFS.keys())
        combos = []

        # 1-factor combos
        for f in factor_names:
            combos.append(FactorCombo(
                name=f"single_{f}",
                factors=[f],
                weights={f: 1.0},
                description=f"单因子策略: {ALL_FACTOR_DEFS[f]['label']}",
            ))

        # 2-factor combos (cross-category preferred)
        from itertools import combinations
        pairs = list(combinations(factor_names, 2))
        for f1, f2 in pairs[:max_combos]:
            cat1 = ALL_FACTOR_DEFS[f1]["category"]
            cat2 = ALL_FACTOR_DEFS[f2]["category"]
            combos.append(FactorCombo(
                name=f"combo_{f1}_{f2}",
                factors=[f1, f2],
                weights={f1: 1.0, f2: 0.5},
                description=f"双因子: {ALL_FACTOR_DEFS[f1]['label']}+{ALL_FACTOR_DEFS[f2]['label']}",
            ))

        return combos[:max_combos]
