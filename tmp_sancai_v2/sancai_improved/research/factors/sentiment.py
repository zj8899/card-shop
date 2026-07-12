"""Sentiment / emotion alpha factors — market-level mood & hot-money dynamics.

Completes the data system from the 白皮书 (Section 8: 情绪数据):
  limit_up_ratio       — 涨停比 → greed index
  limit_down_ratio     — 跌停比 → fear index
  break_board_rate     — 炸板率 → seal quality
  streak_momentum      — 连板晋级率 → profit effect
  profit_effect_index  — composite赚钱效应
  sector_rotation_speed— 板块轮动速度

These are market-level factors — same value for all stocks on a given day.
They are essential inputs for Engine① (Market Regime) and Engine② (Emotion Graph).

Data sources:
  - data_sources/ths_hot.py  → get_topic_heatmap()
  - akshare (stock_zt_pool_em, stock_board_concept_cons_em)
  - data_sources/em_valuation.py → get_market_stats()
"""
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .base import AlphaFactor

logger = logging.getLogger(__name__)


# ── Shared helper: attempt live data when columns are missing ──
_CACHED_SNAPSHOT = None
_CACHED_SNAPSHOT_TS = None


def _get_sentiment_snapshot() -> dict | None:
    """Try to get today's sentiment snapshot, cached per session."""
    global _CACHED_SNAPSHOT, _CACHED_SNAPSHOT_TS
    now = datetime.now()
    if _CACHED_SNAPSHOT is not None and _CACHED_SNAPSHOT_TS:
        # Cache for 5 minutes
        if (now - _CACHED_SNAPSHOT_TS).seconds < 300:
            return _CACHED_SNAPSHOT
    try:
        snap = fetch_today_sentiment_snapshot()
        if snap:
            _CACHED_SNAPSHOT = snap
            _CACHED_SNAPSHOT_TS = now
            return snap
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Limit-Up Ratio — 涨停比 → greed / euphoria gauge
# ═══════════════════════════════════════════════════════════════════════════════

class LimitUpRatioFactor(AlphaFactor):
    """Market-wide limit-up ratio as a greed index.

    High values → speculative fervor / euphoria (caution: greedy).
    Low values  → risk-off / fear (opportunity? depends on context).

    Value = (number of stocks hitting limit-up) / (total traded stocks).
    Normalized to z-score over a rolling window to capture *regime shifts*.
    """

    def __init__(self, smooth: int = 5, z_window: int = 60):
        self._smooth = smooth
        self._z_window = z_window

    @property
    def name(self) -> str:
        return "limit_up_ratio"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)

        if "limit_up_ratio" in df.columns:
            raw = df["limit_up_ratio"].values.astype(float)
        elif "limit_up_count" in df.columns and "total_stocks" in df.columns:
            total = df["total_stocks"].values.astype(float)
            count = df["limit_up_count"].values.astype(float)
            raw = np.where(total > 0, count / total, 0.0)
        elif "limit_up_count" in df.columns:
            # Assume ~5000 total A-share stocks
            raw = df["limit_up_count"].values.astype(float) / 5000.0
        else:
            # Attempt live snapshot for last bar
            snap = _get_sentiment_snapshot()
            if snap and snap.get("limit_up_count", 0) > 0:
                raw = np.zeros(n)
                raw[-1] = snap.get("limit_up_count", 0) / 5000.0
                logger.info("Using live snapshot for limit_up_ratio (last bar only)")
            else:
                logger.debug("No limit_up data; returning neutral")
                return pd.Series(np.zeros(n), index=df.index)

        smooth = pd.Series(raw).rolling(self._smooth, min_periods=1).mean().values

        # Z-score
        result = np.full(n, np.nan)
        for i in range(self._z_window, n):
            mu = smooth[i - self._z_window:i].mean()
            sigma = smooth[i - self._z_window:i].std()
            result[i] = (smooth[i] - mu) / sigma if sigma > 0 else 0.0

        return pd.Series(result, index=df.index)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Limit-Down Ratio — 跌停比 → panic / fear gauge
# ═══════════════════════════════════════════════════════════════════════════════

class LimitDownRatioFactor(AlphaFactor):
    """Market-wide limit-down ratio as a fear index.

    High values → panic selling / capitulation.
    Low values  → normal / calm market.

    Value = (number of stocks hitting limit-down) / (total traded stocks).
    """

    def __init__(self, smooth: int = 5, z_window: int = 60):
        self._smooth = smooth
        self._z_window = z_window

    @property
    def name(self) -> str:
        return "limit_down_ratio"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)

        if "limit_down_ratio" in df.columns:
            raw = df["limit_down_ratio"].values.astype(float)
        elif "limit_down_count" in df.columns:
            raw = df["limit_down_count"].values.astype(float) / 5000.0
        else:
            snap = _get_sentiment_snapshot()
            if snap and snap.get("limit_down_count", 0) > 0:
                raw = np.zeros(n)
                raw[-1] = snap.get("limit_down_count", 0) / 5000.0
                logger.info("Using live snapshot for limit_down_ratio (last bar only)")
            else:
                logger.debug("No limit_down data; returning neutral")
                return pd.Series(np.zeros(n), index=df.index)

        smooth = pd.Series(raw).rolling(self._smooth, min_periods=1).mean().values

        result = np.full(n, np.nan)
        for i in range(self._z_window, n):
            mu = smooth[i - self._z_window:i].mean()
            sigma = smooth[i - self._z_window:i].std()
            result[i] = (smooth[i] - mu) / sigma if sigma > 0 else 0.0

        return pd.Series(result, index=df.index)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Break-Board Rate — 炸板率 → board-seal quality
# ═══════════════════════════════════════════════════════════════════════════════

class BreakBoardRateFactor(AlphaFactor):
    """Rate at which stocks hit limit-up but fail to seal (炸板率).

    High break-board rate → weak conviction, selling pressure overwhelming bids.
    Low break-board rate  → strong conviction, buyers in control.

    Value = stocks that touched limit-up but closed below / all stocks that touched limit-up.
    Sign-inverted so: HIGH factor = GOOD (low break rate), LOW factor = BAD (high break rate).
    """

    def __init__(self, smooth: int = 5):
        self._smooth = smooth

    @property
    def name(self) -> str:
        return "break_board_rate"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)

        if "break_board_rate" in df.columns:
            raw = df["break_board_rate"].values.astype(float)
        elif "touched_limit_up" in df.columns and "sealed_limit_up" in df.columns:
            touched = df["touched_limit_up"].values.astype(float)
            sealed = df["sealed_limit_up"].values.astype(float)
            raw = np.where(touched > 0, 1.0 - sealed / touched, 0.0)
        else:
            snap = _get_sentiment_snapshot()
            if snap:
                touched = snap.get("touched_limit_up", 0)
                sealed = snap.get("sealed_limit_up", 0)
                if touched > 0:
                    raw = np.zeros(n)
                    raw[-1] = sealed / touched  # seal rate
                    logger.info("Using live snapshot for break_board_rate (last bar only)")
                else:
                    logger.debug("No break_board data; returning neutral")
                    return pd.Series(np.zeros(n), index=df.index)
            else:
                logger.debug("No break_board data; returning neutral")
                return pd.Series(np.zeros(n), index=df.index)

        # Invert so high = good
        raw_inv = 1.0 - raw
        smooth = pd.Series(raw_inv).rolling(self._smooth, min_periods=1).mean().values

        return pd.Series(smooth, index=df.index)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Streak Momentum — 连板晋级率 → money-making effect strength
# ═══════════════════════════════════════════════════════════════════════════════

class StreakMomentumFactor(AlphaFactor):
    """Consecutive limit-up advancement rate (连板晋级率).

    Measures whether streaking stocks are sustaining (bullish) or failing (bearish).
    High → strong trend persistence, money-making effect propagating.
    Low  → streaks breaking, hot money cooling.

    Value = (N→N+1 advancement rate across active streaks), weighted by streak length.
    """

    def __init__(self, smooth: int = 5):
        self._smooth = smooth

    @property
    def name(self) -> str:
        return "streak_momentum"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)

        if "streak_momentum" in df.columns:
            raw = df["streak_momentum"].values.astype(float)
        elif "streak_2to3" in df.columns and "streak_3to4" in df.columns:
            # Weighted composite: 2→3 counts 0.3, 3→4 counts 0.5, 4→5+ counts 0.2
            s23 = df["streak_2to3"].values.astype(float)
            s34 = df["streak_3to4"].values.astype(float)
            raw = 0.3 * s23 + 0.5 * s34
            if "streak_5plus" in df.columns:
                raw += 0.2 * df["streak_5plus"].values.astype(float)
        else:
            snap = _get_sentiment_snapshot()
            if snap:
                s23 = snap.get("streak_2to3", 0.0)
                s34 = snap.get("streak_3to4", 0.0)
                if s23 > 0 or s34 > 0:
                    raw = np.zeros(n)
                    raw[-1] = s23 * 0.5 + s34 * 0.5
                    logger.info("Using live snapshot for streak_momentum (last bar only)")
                else:
                    logger.debug("No streak data; returning neutral")
                    return pd.Series(np.zeros(n), index=df.index)
            else:
                logger.debug("No streak data; returning neutral")
                return pd.Series(np.zeros(n), index=df.index)

        smooth = pd.Series(raw).rolling(self._smooth, min_periods=1).mean().values
        return pd.Series(smooth, index=df.index)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Profit Effect Index — composite 赚钱效应
# ═══════════════════════════════════════════════════════════════════════════════

class ProfitEffectIndexFactor(AlphaFactor):
    """Composite profit-making effect index.

    Combines multiple dimensions into a single 0-1 score:

      profit_effect = 0.30 * (advance/decline ratio normalized)
                    + 0.25 * limit_up_ratio_z
                    + 0.25 * streak_momentum
                    + 0.20 * (1 - break_board_rate)

    High (>0.6)  → market is rewarding risk-taking (赚钱效应强).
    Low  (<0.3)  → market is punishing risk-taking (亏钱效应强).
    """

    def __init__(self, window: int = 20):
        self._window = window

    @property
    def name(self) -> str:
        return "profit_effect_index"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)

        # Build component series
        adv_dec = np.zeros(n)
        limit_up_z = np.zeros(n)
        streak = np.zeros(n)
        break_rate = np.zeros(n)

        # 1. Advance/decline ratio
        if "advance_count" in df.columns and "decline_count" in df.columns:
            adv = df["advance_count"].values.astype(float)
            dec = df["decline_count"].values.astype(float)
            ratio = np.where(dec > 0, adv / dec, 1.0)
            adv_dec = np.clip((ratio - 0.5) / 2.0, -0.5, 0.5) + 0.5  # normalize to ~[0,1]

        # 2. Limit-up ratio proxy
        if "limit_up_count" in df.columns:
            raw = df["limit_up_count"].values.astype(float) / 5000.0
            mu = pd.Series(raw).rolling(60).mean().values
            sigma = pd.Series(raw).rolling(60).std().values
            for i in range(60, n):
                limit_up_z[i] = (raw[i] - mu[i]) / sigma[i] if sigma[i] > 0 else 0.0
            limit_up_z = np.clip((limit_up_z + 2.0) / 4.0, 0.0, 1.0)  # z-score → [0,1]

        # 3. Streak momentum proxy
        if "streak_momentum" in df.columns:
            streak = np.clip(df["streak_momentum"].values.astype(float), 0.0, 1.0)

        # 4. Break-board rate
        if "break_board_rate" in df.columns:
            break_rate = np.clip(1.0 - df["break_board_rate"].values.astype(float), 0.0, 1.0)

        # Composite
        composite = 0.30 * adv_dec + 0.25 * limit_up_z + 0.25 * streak + 0.20 * break_rate

        # If no columns at all, return neutral
        has_data = any(c in df.columns for c in ["advance_count", "limit_up_count", "streak_momentum", "break_board_rate"])
        if not has_data:
            return pd.Series(np.full(n, 0.5), index=df.index)

        smooth = pd.Series(composite).rolling(self._window, min_periods=1).mean().values
        return pd.Series(smooth, index=df.index)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Sector Rotation Speed — 板块轮动速度
# ═══════════════════════════════════════════════════════════════════════════════

class SectorRotationSpeedFactor(AlphaFactor):
    """Measures how fast sector leadership is changing.

    High rotation speed → no stable mainline, money chasing different themes daily.
    Low rotation speed  → persistent mainline, money concentrated in few themes.

    Value = 1 - rank_correlation(yesterday's sector rankings, today's) over top-N sectors.
    Range: [0, 1] where 0 = no rotation (same leaders), 1 = full rotation (completely new leaders).
    """

    def __init__(self, top_n: int = 20, smooth: int = 5):
        self._top_n = top_n
        self._smooth = smooth

    @property
    def name(self) -> str:
        return "sector_rotation_speed"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)

        if "sector_rotation_speed" in df.columns:
            raw = df["sector_rotation_speed"].values.astype(float)
            smooth = pd.Series(raw).rolling(self._smooth, min_periods=1).mean().values
            return pd.Series(smooth, index=df.index)

        # Cannot compute from OHLCV alone — needs sector ranking time series
        logger.debug("No sector_rotation_speed column; returning neutral")
        return pd.Series(np.full(n, 0.5), index=df.index)

    @staticmethod
    def compute_from_sector_rankings(
        today_rankings: dict[str, float],
        yesterday_rankings: dict[str, float],
        top_n: int = 20,
    ) -> float:
        """Compute rotation speed from two days of sector-ranking data.

        Args:
            today_rankings: {sector_name: change_pct}
            yesterday_rankings: {sector_name: change_pct}

        Returns:
            float in [0, 1] — rotation speed.
        """
        from scipy.stats import spearmanr

        today_sorted = sorted(today_rankings.items(), key=lambda x: x[1], reverse=True)[:top_n]
        yesterday_sorted = sorted(yesterday_rankings.items(), key=lambda x: x[1], reverse=True)[:top_n]

        today_names = [s[0] for s in today_sorted]
        yesterday_names = [s[0] for s in yesterday_sorted]

        # Build rank dictionaries
        today_ranks = {name: i for i, name in enumerate(today_names)}
        yesterday_ranks = {name: i for i, name in enumerate(yesterday_names)}

        # Common sectors
        common = set(today_names) & set(yesterday_names)
        if len(common) < 5:
            return 0.8  # high rotation when few overlaps

        t_ranks = [today_ranks[n] for n in common]
        y_ranks = [yesterday_ranks[n] for n in common]

        try:
            corr, _ = spearmanr(t_ranks, y_ranks)
            if np.isnan(corr):
                return 0.5
            return max(0.0, min(1.0, 1.0 - corr))
        except Exception:
            return 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Data pipeline helpers
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_today_sentiment_snapshot() -> dict:
    """Fetch today's sentiment indicators from live data sources.

    Returns a dict that can be written to a parquet column for backfilling.
    Call this from the daily data update pipeline (e.g. monitor post-market).

    Returns:
        {limit_up_ratio, limit_down_ratio, break_board_rate, streak_momentum,
         profit_effect, sector_rotation_speed, advance_decline_ratio, timestamp}
    """
    result = {
        "limit_up_count": 0, "limit_down_count": 0,
        "touched_limit_up": 0, "sealed_limit_up": 0,
        "streak_2to3": 0.0, "streak_3to4": 0.0,
        "advance_count": 0, "decline_count": 0,
        "timestamp": datetime.now().isoformat(),
    }

    try:
        import akshare as ak
        today_str = datetime.now().strftime("%Y%m%d")

        # Limit-up pool
        try:
            zt_df = ak.stock_zt_pool_em(date=today_str)
            if zt_df is not None and len(zt_df) > 0:
                result["sealed_limit_up"] = len(zt_df)
                result["limit_up_count"] = len(zt_df)
                # Estimate touched from the "开板" status
                if "开板" in zt_df.columns:
                    result["touched_limit_up"] = len(zt_df) + (zt_df["开板"] != "").sum()
        except Exception:
            pass

        # Limit-down pool
        try:
            dt_df = ak.stock_zt_pool_dtgc_em(date=today_str)
            if dt_df is not None and len(dt_df) > 0:
                result["limit_down_count"] = len(dt_df)
        except Exception:
            pass

        # Market overview (advance/decline)
        try:
            overview = ak.stock_sse_summary()
            if overview is not None and "上涨家数" in overview:
                result["advance_count"] = int(overview["上涨家数"])
            if overview is not None and "下跌家数" in overview:
                result["decline_count"] = int(overview["下跌家数"])
        except Exception:
            pass

        # Streak stats from limit-up pool
        try:
            zt_df2 = ak.stock_zt_pool_zbgc_em(date=today_str)
            if zt_df2 is not None and len(zt_df2) > 0:
                # Estimate streak advancement
                streaks = zt_df2.get("连板数", pd.Series()).value_counts().to_dict()
                total_continuous = sum(streaks.values())
                if total_continuous > 0:
                    streak_2 = streaks.get(2, 0)
                    streak_3 = streaks.get(3, 0)
                    streak_4 = streaks.get(4, 0)
                    result["streak_2to3"] = streak_3 / max(streak_2, 1)
                    result["streak_3to4"] = streak_4 / max(streak_3, 1)
        except Exception:
            pass

    except ImportError:
        logger.warning("akshare not available for sentiment snapshot")
    except Exception as e:
        logger.warning("Sentiment snapshot fetch failed: %s", e)

    # Compute derived fields
    total = result.get("advance_count", 0) + result.get("decline_count", 0)
    result["limit_up_ratio"] = result["limit_up_count"] / max(total, 1)
    result["limit_down_ratio"] = result["limit_down_count"] / max(total, 1)
    if result["touched_limit_up"] > 0:
        result["break_board_rate"] = 1.0 - result["sealed_limit_up"] / result["touched_limit_up"]
    else:
        result["break_board_rate"] = 0.0
    result["advance_decline_ratio"] = (
        result["advance_count"] / max(result["decline_count"], 1)
    )

    return result
