"""训练数据管道 — 从回测结果生成有标签的 ML 训练数据。

每个信号触发时刻:
  - 特征 X = FactorPipeline 计算的多因子值(按列)
  - 标签 y = 该信号对应的交易盈亏(win/loss) 或未来 N 日收益

LightGBM / RollingTrainer 直接可吃产出。
"""
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"


def build_signals_dataset(symbol: str,
                          factor_names: list[str] | None = None,
                          label_horizon: int = 5,
                          tail_bars: int = 500) -> pd.DataFrame | None:
    """从回测信号+因子数据构建单标的训练数据集。

    对每根 bar：计算各因子当前值作为特征 X；标签 y = 未来 label_horizon 根 bar 的
    累积收益率(%)。只保留非 NaN 行。

    Parameters
    ----------
    symbol : str
        6 位股票代码。
    factor_names : list[str] | None
        因子名列表；None 时用默认 12 个技术因子。
    label_horizon : int
        标签前瞻窗口(bars)。
    tail_bars : int
        最近加载的 bar 数。

    Returns
    -------
    pd.DataFrame | None
        columns = factor_names + ["symbol", "date", "label"]，或数据不足时返回 None。
    """
    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        return None

    try:
        df = pd.read_parquet(fpath)
    except Exception:
        logger.debug("Failed to read parquet for %s", symbol)
        return None

    if len(df) < max(60, label_horizon + 10):
        return None

    df = df.tail(tail_bars).reset_index(drop=True)

    # 日期
    if "date" in df.columns:
        dates = df["date"].values
    elif isinstance(df.index, pd.DatetimeIndex):
        dates = df.index.values
    else:
        return None

    # 默认因子列表
    if factor_names is None:
        factor_names = [
            "ma_dev_34", "kdj_9_3", "rsi_14", "vol_ratio_20",
            "trend_strength", "atr_14",
            "stop_loss_cluster", "liquidity_density",
            "order_imbalance", "micro_momentum",
            "turnover_volatility", "price_position_asymmetry",
        ]

    # 用 FactorPipeline 计算因子列
    try:
        from research.factors import (
            MADeviationFactor, KDJFactor, RSIFactor,
            VolumeRatioFactor, TrendStrengthFactor, ATRFactor,
            StopLossClusterFactor, LiquidityDensityFactor,
            OrderImbalanceFactor, MicroMomentumFactor,
            TurnoverVolatilityFactor, PricePositionAsymmetryFactor,
        )
        FACTOR_REGISTRY = {
            "ma_dev_34": MADeviationFactor(),
            "kdj_9_3": KDJFactor(),
            "rsi_14": RSIFactor(),
            "vol_ratio_20": VolumeRatioFactor(),
            "trend_strength": TrendStrengthFactor(),
            "atr_14": ATRFactor(),
            "stop_loss_cluster": StopLossClusterFactor(),
            "liquidity_density": LiquidityDensityFactor(),
            "order_imbalance": OrderImbalanceFactor(),
            "micro_momentum": MicroMomentumFactor(),
            "turnover_volatility": TurnoverVolatilityFactor(),
            "price_position_asymmetry": PricePositionAsymmetryFactor(),
        }
        for name in factor_names:
            if name in FACTOR_REGISTRY:
                df[name] = FACTOR_REGISTRY[name].calculate(df).values
    except ImportError:
        logger.debug("Factor modules not available; skipping factor columns for %s", symbol)
        return None

    # 标签: 未来 N 日累积收益(%)
    close = df["close"].values.astype(float)
    forward = np.full(len(close), np.nan)
    for i in range(len(close) - label_horizon):
        if close[i] > 0:
            forward[i] = (close[i + label_horizon] / close[i] - 1.0) * 100.0
    df["label"] = forward

    # 去 NaN: 只保留所有因子列+标签非 NaN 的行
    keep_cols = factor_names + ["label"]
    df_clean = df.dropna(subset=keep_cols).copy()
    df_clean["symbol"] = symbol
    df_clean["date"] = dates[df_clean.index]

    return df_clean[factor_names + ["symbol", "date", "label"]]


def build_batch_dataset(symbols: list[str],
                        factor_names: list[str] | None = None,
                        label_horizon: int = 5) -> pd.DataFrame:
    """从全批次股票构建训练数据集（concat 单标的数据）。

    Parameters
    ----------
    symbols : list[str]
        股票代码列表。
    factor_names : list[str] | None
        因子名列表；None 时用默认 12 个技术因子。
    label_horizon : int
        标签前瞻窗口(bars)。

    Returns
    -------
    pd.DataFrame
        全批次 concat 数据集，可直喂 LightGBM.fit / RollingTrainer.run。
    """
    frames = []
    for sym in symbols:
        df = build_signals_dataset(sym, factor_names=factor_names, label_horizon=label_horizon)
        if df is not None and len(df) > 20:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    logger.info("Built training dataset: %d stocks, %d rows, %d columns",
                len(frames), len(combined), combined.shape[1])
    return combined


def compute_factor_ic(symbols: list[str],
                      factor_names: list[str] | None = None,
                      window_size: int = 60,
                      step_size: int = 20) -> list[dict]:
    """计算多标的因子的滚动窗口 IC 排名。

    对各标的分别用 FactorPipeline 跑滚动 IC，然后按因子名聚合(mean/median/pos ratio)，
    返回按 mean_ic 降序排列的列表，供进化 prompt 使用。

    Returns
    -------
    list[dict]
        [{factor_name, mean_ic, ic_ir, positive_ratio}, ...] 按 mean_ic 降序。
    """
    from collections import defaultdict

    if factor_names is None:
        factor_names = ["ma_dev_34", "kdj_9_3", "rsi_14", "vol_ratio_20",
                        "trend_strength", "atr_14"]

    agg = defaultdict(lambda: {"ic_list": [], "ir_list": [], "pos_list": []})

    for sym in symbols[:50]:  # 采样不超过50只(全量太慢)
        fpath = DATA_DIR / "daily" / f"{sym}.parquet"
        if not fpath.exists():
            continue
        try:
            df = pd.read_parquet(fpath).tail(500)
        except Exception:
            continue
        if len(df) < 100:
            continue

        try:
            from research.factors import (
                MADeviationFactor, KDJFactor, RSIFactor,
                VolumeRatioFactor, TrendStrengthFactor, ATRFactor,
            )
            FACTORS = [
                MADeviationFactor(), KDJFactor(), RSIFactor(),
                VolumeRatioFactor(), TrendStrengthFactor(), ATRFactor(),
            ]
            # 只保留匹配因子名列表的因子
            factors = [f for f in FACTORS if f.name in factor_names]
            if not factors:
                continue

            from research.pipeline import FactorPipeline
            pipeline = FactorPipeline(factors, df, window_size=window_size,
                                      step_size=step_size, forward_periods=1)
            pipeline.run()
            for r in pipeline.reports:
                agg[r.factor_name]["ic_list"].append(r.mean_ic)
                agg[r.factor_name]["ir_list"].append(r.ic_ir)
                agg[r.factor_name]["pos_list"].append(r.ic_positive_ratio)
        except Exception:
            continue

    ranked = []
    for name, vals in agg.items():
        if not vals["ic_list"]:
            continue
        ranked.append({
            "factor_name": name,
            "mean_ic": round(float(np.mean(vals["ic_list"])), 4),
            "median_ic": round(float(np.median(vals["ic_list"])), 4),
            "ic_ir": round(float(np.mean(vals["ir_list"])), 4),
            "positive_ratio": round(float(np.mean(vals["pos_list"])), 4),
            "n_stocks": len(vals["ic_list"]),
        })

    ranked.sort(key=lambda x: abs(x["mean_ic"]), reverse=True)
    return ranked
