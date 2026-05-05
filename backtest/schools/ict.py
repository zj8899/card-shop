"""
ICT (Inner Circle Trader) — 唐门暗器
Creator: Michael Huddleston

Core Techniques:
1. Order Blocks (OB) — Last opposite candle before impulse
2. Fair Value Gaps (FVG) — Imbalance / liquidity void
3. Liquidity Concepts — Buy-side / Sell-side liquidity, Stop Hunts
4. Market Structure Shift (MSS) — Change of Character (CHoCH)
5. Optimal Trade Entry (OTE) — Fibonacci 0.618-0.79 retracement zone
6. Breaker Blocks — Failed OB that becomes support/resistance
7. Kill Zones — London Open, New York Open, London Close
"""
import numpy as np
import pandas as pd
from .base import BaseSchool


class ICTSchool(BaseSchool):
    name = "ict"
    description = "ICT: Order Block+FVG+Liquidity+MSS+OTE"
    core_technique = "Order Block + FVG + Liquidity Sweep"

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        # Identify Market Structure
        swings_h = self.swing_highs(close, 5)
        swings_l = self.swing_lows(close, 5)

        # Track market structure state
        hh_hl = False  # Bullish: Higher High + Higher Low
        lh_ll = False  # Bearish: Lower High + Lower Low
        choch_idx = 0  # Last Change of Character index

        for i in range(80, len(close)):
            date_val = str(df.iloc[i].get("date", i))

            # ---- Market Structure Shift (MSS / CHoCH) ----
            recent_h = [h for h in swings_h if i - 40 < h < i]
            recent_l = [l for l in swings_l if i - 40 < l < i]

            if len(recent_h) >= 3 and len(recent_l) >= 3:
                h1, h2, h3 = recent_h[-3], recent_h[-2], recent_h[-1]
                l1, l2, l3 = recent_l[-3], recent_l[-2], recent_l[-1]

                # Previous bullish: HH+HL
                was_bullish = close[h2] > close[h1] and close[l2] > close[l1]
                # Now bearish shift: breaks below last higher low
                if was_bullish and close[i] < close[l2]:
                    choch_idx = i
                    lh_ll = True
                    hh_hl = False

                # Previous bearish: LH+LL
                was_bearish = close[h2] < close[h1] and close[l2] < close[l1]
                # Now bullish shift: breaks above last lower high
                if was_bearish and close[i] > close[h2]:
                    choch_idx = i
                    hh_hl = True
                    lh_ll = False

            # ---- Order Block Detection ----
            # Bullish OB: Last bearish candle before a strong up move
            if i >= 3:
                strong_up = close[i] > close[i - 3] * 1.015
                if strong_up:
                    # Find the last bearish candle
                    for j in range(i - 1, max(0, i - 10), -1):
                        if close[j] < open_[j]:
                            ob_high = high[j]
                            ob_low = low[j]
                            # Price retraces to OB zone
                            if ob_low <= close[i] <= ob_high * 1.005 and enabled("ob"):
                                self._sig(date_val, "BUY", close[i],
                                          f"ICT·看涨OB回踩: OB[{ob_low:.2f}-{ob_high:.2f}]",
                                          ob_zone=f"{ob_low:.2f}-{ob_high:.2f}")
                            break

                strong_down = close[i] < close[i - 3] * 0.985
                if strong_down:
                    for j in range(i - 1, max(0, i - 10), -1):
                        if close[j] > open_[j]:
                            ob_high = high[j]
                            ob_low = low[j]
                            if ob_low <= close[i] <= ob_high * 1.005 and enabled("ob"):
                                self._sig(date_val, "SELL", close[i],
                                          f"ICT·看跌OB反弹: OB[{ob_low:.2f}-{ob_high:.2f}]",
                                          ob_zone=f"{ob_low:.2f}-{ob_high:.2f}")
                            break

            # ---- Fair Value Gap (FVG) ----
            # Bullish FVG: current low > 2-bars-ago high (gap up imbalance)
            if i >= 3:
                fvg_bull = low[i] > high[i - 2] + 0.01
                fvg_bear = high[i] < low[i - 2] - 0.01

                if fvg_bull and close[i] > close[i - 1] and enabled("fvg"):
                    fvg_top = low[i]
                    fvg_bot = high[i - 2]
                    self._sig(date_val, "BUY", close[i],
                              f"ICT·看涨FVG: [{fvg_bot:.2f}-{fvg_top:.2f}]缺口回补买入",
                              fvg=f"{fvg_bot:.2f}-{fvg_top:.2f}")

                if fvg_bear and close[i] < close[i - 1] and enabled("fvg"):
                    fvg_top = low[i - 2]
                    fvg_bot = high[i]
                    self._sig(date_val, "SELL", close[i],
                              f"ICT·看跌FVG: [{fvg_bot:.2f}-{fvg_top:.2f}]缺口回补卖出",
                              fvg=f"{fvg_bot:.2f}-{fvg_top:.2f}")

            # ---- Liquidity Sweep + OTE Entry ----
            # Buy-side liquidity sweep: price takes out high then reverses
            if i >= 5:
                recent_high_5 = np.max(high[i - 5:i])
                swept_high = high[i - 1] > recent_high_5 and close[i] < close[i - 1]
                if swept_high and hh_hl is False and enabled("liquidity_ote"):
                    self._sig(date_val, "SELL", close[i],
                              f"ICT·买方流动性猎杀: 扫高点{recent_high_5:.2f}后反转")

                recent_low_5 = np.min(low[i - 5:i])
                swept_low = low[i - 1] < recent_low_5 and close[i] > close[i - 1]
                if swept_low and lh_ll is False and enabled("liquidity_ote"):
                    # Check OTE zone (61.8%-79% retracement)
                    swing_range = np.max(high[max(0, i - 20):i]) - np.min(low[max(0, i - 20):i])
                    fib_618 = np.max(high[max(0, i - 20):i]) - swing_range * 0.618
                    fib_79 = np.max(high[max(0, i - 20):i]) - swing_range * 0.79
                    if fib_79 <= close[i] <= fib_618:
                        self._sig(date_val, "BUY", close[i],
                                  f"ICT·卖方流动性猎杀+OTE: 扫低点{recent_low_5:.2f}入OTE",
                                  ote_zone=f"{fib_79:.2f}-{fib_618:.2f}")

            # ---- Breaker Block ----
            if i >= 10 and choch_idx > 0:
                # After CHoCH, old OB becomes breaker (support becomes resistance)
                bb_zone_low = close[choch_idx] * 0.998
                bb_zone_high = close[choch_idx] * 1.002
                if bb_zone_low <= close[i] <= bb_zone_high and enabled("breaker_block"):
                    if hh_hl:
                        self._sig(date_val, "BUY", close[i],
                                  f"ICT·Breaker Block支撑: CHoCH点{close[choch_idx]:.2f}")
                    elif lh_ll:
                        self._sig(date_val, "SELL", close[i],
                                  f"ICT·Breaker Block阻力: CHoCH点{close[choch_idx]:.2f}")

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()
