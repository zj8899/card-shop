"""
Base class for 8 trading school backtest modules.
Each school implements a unique core strategy for A-share markets.
"""
from abc import ABC, abstractmethod
import numpy as np
import pandas as pd


class BaseSchool(ABC):
    name: str = "base"
    description: str = ""
    core_technique: str = ""  # The signature technique

    def __init__(self):
        self.signals: list[dict] = []

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        """Generate buy/sell signals from OHLCV data.

        Args:
            df: OHLCV DataFrame
            config: Optional dict of signal type toggles. When None, all signals enabled.
        """

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
        avg_gain = pd.Series(gain).ewm(span=period, adjust=False).mean().to_numpy()
        avg_loss = pd.Series(loss).ewm(span=period, adjust=False).mean().to_numpy()
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

    def _sig(self, date_val, signal, price, reason, **kwargs):
        d = {"date": str(date_val)[:19], "signal": signal, "price": self._clean(price),
             "reason": reason, "school": self.name}
        for k, v in kwargs.items():
            d[k] = self._clean(v)
        self.signals.append(d)
