"""
Base class for 8 trading school backtest modules.
Each school implements a unique core strategy for A-share markets.
"""
from abc import ABC, abstractmethod
from typing import Optional
import numpy as np
import pandas as pd


class BaseSchool(ABC):
    name: str = "base"
    description: str = ""
    core_technique: str = ""  # The signature technique

    # Subclasses override: parameter schema for the tuning panel
    # Each param: {type, default, min, max, step, unit_label, theory_explanation}
    PARAMS: dict[str, dict] = {}

    # Subclasses override: signal toggle definitions
    # Each toggle: {key, label, default}
    SIGNAL_TOGGLES: list[dict] = []

    def __init__(self):
        self.signals: list[dict] = []

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        """Generate buy/sell signals from OHLCV data.

        Args:
            df: OHLCV DataFrame
            config: Optional dict of signal type toggles and numeric params.
                    When None, all signals enabled with default params.
        """

    # ---- Parameter helpers ----

    def get_param(self, config: Optional[dict], key: str):
        """Read parameter from config, falling back to PARAMS schema default.
        Automatically clamps to min/max range."""
        schema = self.PARAMS.get(key, {})
        default = schema.get("default")
        val = config.get(key, default) if config else default
        if schema:
            if "min" in schema:
                val = max(schema["min"], val)
            if "max" in schema:
                val = min(schema["max"], val)
        return val

    def param_enabled(self, config: Optional[dict], key: str) -> bool:
        """Check if a signal toggle is enabled."""
        if config is None:
            return True
        return config.get(key, True)

    def signal_check(self, df: pd.DataFrame, config: dict = None) -> dict:
        """Real-time signal check for live trading.
        Returns latest bar's signal with conditions_met and conditions_close."""
        sigs = self.generate_signals(df, config)
        if len(sigs) == 0:
            return {"signal": None, "condition": "无信号", "conditions_close": []}
        last = sigs.iloc[-1]
        close_conds = []
        for key in list(last.keys()):
            if key.startswith("close_"):
                close_conds.append({"key": key, "note": str(last[key])})
        return {
            "signal": last.get("signal"),
            "price": float(last.get("price", 0)) if last.get("price") is not None else None,
            "reason": last.get("reason", ""),
            "conditions_met": last.get("conditions_met", []) if "conditions_met" in last.index else [],
            "conditions_close": close_conds,
        }

    def position_analysis(self, df: pd.DataFrame, config: dict = None) -> dict:
        """Analyze current price position in the strategy's structural framework.

        Returns strategy-specific structural info. Default returns empty.
        Subclasses (e.g. ChanTheorySchool) override to provide hub/segment/stroke info.
        """
        close = df["close"].values
        return {
            "school": self.name,
            "current_price": float(close[-1]) if len(close) > 0 else 0,
            "trend": self._detect_trend(df),
            "note": "该策略不支持结构位置分析",
        }

    def _detect_trend(self, df: pd.DataFrame) -> str:
        """Detect simple trend from MA34 slope."""
        close = df["close"].values
        if len(close) < 34:
            return "数据不足"
        ma34 = pd.Series(close).rolling(34).mean().values
        if len(ma34) < 10:
            return "数据不足"
        slope = (ma34[-1] - ma34[-10]) / ma34[-10] if ma34[-10] > 0 else 0
        if slope > 0.01:
            return "上升"
        elif slope < -0.01:
            return "下降"
        return "震荡"

    @staticmethod
    def get_school_params() -> dict:
        """Aggregate PARAMS and SIGNAL_TOGGLES from all registered schools."""
        from . import SCHOOLS
        result = {}
        for name, cls in SCHOOLS.items():
            inst = cls()
            result[name] = {
                "toggles": cls.SIGNAL_TOGGLES,
                "params": cls.PARAMS,
                "name": cls.name,
                "description": cls.description,
                "core_technique": cls.core_technique,
            }
        return result

    # ---- Shared utilities ----

    def sma(self, s: np.ndarray, p: int) -> np.ndarray:
        return pd.Series(s).rolling(p).mean().to_numpy()

    def ema(self, s: np.ndarray, p: int) -> np.ndarray:
        return pd.Series(s).ewm(span=p, adjust=False).mean().to_numpy()

    def atr(self, h: np.ndarray, l: np.ndarray, c: np.ndarray, p: int = 14) -> np.ndarray:
        tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
        tr[0] = h[0] - l[0]
        return pd.Series(tr).ewm(span=p, adjust=False).mean().to_numpy()

    def swing_highs(self, prices: np.ndarray, window: int = 5) -> list:
        res = []
        for i in range(window, len(prices) - window):
            if prices[i] == np.max(prices[i - window:i + window + 1]):
                res.append(i)
        return res

    def swing_lows(self, prices: np.ndarray, window: int = 5) -> list:
        res = []
        for i in range(window, len(prices) - window):
            if prices[i] == np.min(prices[i - window:i + window + 1]):
                res.append(i)
        return res

    def macd(self, close: np.ndarray, fast=12, slow=26, sig=9):
        e1 = self.ema(close, fast)
        e2 = self.ema(close, slow)
        dif = e1 - e2
        dea = self.ema(dif, sig)
        bar = 2 * (dif - dea)
        return dif, dea, bar

    def rsi(self, close: np.ndarray, period: int = 14) -> np.ndarray:
        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = pd.Series(gain).ewm(alpha=1/period, adjust=False).mean().to_numpy()
        avg_loss = pd.Series(loss).ewm(alpha=1/period, adjust=False).mean().to_numpy()
        rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss != 0)
        return 100 - 100 / (1 + rs)

    def _clean(self, v):
        """Replace NaN/Inf with None for JSON serialization."""
        if isinstance(v, (float, np.floating, np.integer)):
            fv = float(v)
            if np.isnan(fv) or np.isinf(fv):
                return None
            return fv
        return v

    def _sig(self, date_val, signal, price, reason, conditions=None, **kwargs):
        d = {"date": str(date_val)[:19], "signal": signal, "price": self._clean(price),
             "reason": reason, "school": self.name}
        if conditions is not None:
            d["conditions_met"] = conditions
        for k, v in kwargs.items():
            d[k] = self._clean(v)
        self.signals.append(d)
