"""
价格行为 (Price Action) — 独孤九剑
Core: Pure price structure, no indicators (or minimal)

Core Techniques:
1. Market Structure — HH/HL (uptrend), LH/LL (downtrend), Break of Structure (BOS)
2. Pin Bar / Hammer / Shooting Star — Long wick rejection
3. Engulfing Pattern — Bullish/Bearish engulfing candles
4. Inside Bar (IB) — Breakout of the mother candle
5. Fakey / False Break — Trap entry against breakout
6. Support-Resistance Flip — Old resistance becomes new support
7. Trendline Touch + Bounce
8. Multiple Timeframe Confirmation
"""
import numpy as np
import pandas as pd
from .base import BaseSchool


class PriceActionSchool(BaseSchool):
    name = "price_action"
    description = "价格行为: Market Structure+Pin Bar+Engulfing+IB+Fakey"
    core_technique = "市场结构 + 关键K线形态 + 假突破"

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        open_ = df["open"].values
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        # Find key S/R levels
        swings_h = self.swing_highs(close, 6)
        swings_l = self.swing_lows(close, 6)

        support_levels = [close[l] for l in swings_l[-10:]] if swings_l else []
        resistance_levels = [close[h] for h in swings_h[-10:]] if swings_h else []

        # Market structure tracking
        trend = "neutral"  # "up", "down", "neutral"
        last_hh = 0
        last_hl = 0
        last_lh = 0
        last_ll = 0

        for i in range(60, len(close)):
            date_val = str(df.iloc[i].get("date", i))

            # ---- Candle Metrics ----
            body = abs(close[i] - open_[i])
            candle_range = high[i] - low[i]
            if candle_range < 0.001:
                continue

            upper_wick = high[i] - max(open_[i], close[i])
            lower_wick = min(open_[i], close[i]) - low[i]
            body_pct = body / candle_range if candle_range > 0 else 0
            upper_pct = upper_wick / candle_range
            lower_pct = lower_wick / candle_range

            avg_range = np.mean(high[max(0, i - 20):i] - low[max(0, i - 20):i]) if i >= 20 else candle_range
            avg_vol = np.mean(vol[max(0, i - 20):i]) if i >= 20 else vol[i]

            # ---- Market Structure Update ----
            recent_h = [h for h in swings_h if i - 50 < h <= i]
            recent_l = [l for l in swings_l if i - 50 < l <= i]

            if len(recent_h) >= 2 and len(recent_l) >= 2:
                h1, h2 = recent_h[-2], recent_h[-1]
                l1, l2 = recent_l[-2], recent_l[-1]

                if close[h2] > close[h1] and close[l2] > close[l1]:
                    trend = "up"
                    last_hh, last_hl = h2, l2
                elif close[h2] < close[h1] and close[l2] < close[l1]:
                    trend = "down"
                    last_lh, last_ll = h2, l2
                else:
                    trend = "neutral"

            # ---- Pin Bar (Hammer / Shooting Star) ----
            is_bullish_pin = (lower_pct > 0.6 and body_pct < 0.3 and close[i] > open_[i])
            is_bearish_pin = (upper_pct > 0.6 and body_pct < 0.3 and close[i] < open_[i])

            # Pin bar at support in uptrend
            near_support = any(abs(close[i] - s) / s < 0.02 for s in support_levels) if support_levels else False
            if is_bullish_pin and (trend == "up" or near_support) and enabled("pin_bar"):
                self._sig(date_val, "BUY", close[i],
                          f"PA·Pin Bar锤子线: 长下影@{close[i]:.2f} 趋势:{trend}",
                          pattern="hammer")

            # Pin bar at resistance in downtrend
            near_resistance = any(abs(close[i] - r) / r < 0.02 for r in resistance_levels) if resistance_levels else False
            if is_bearish_pin and (trend == "down" or near_resistance) and enabled("pin_bar"):
                self._sig(date_val, "SELL", close[i],
                          f"PA·Shooting Star: 长上影@{close[i]:.2f} 趋势:{trend}",
                          pattern="shooting_star")

            # ---- Engulfing Pattern ----
            if i >= 1:
                prev_body = abs(close[i - 1] - open_[i - 1])
                prev_range = high[i - 1] - low[i - 1]

                # Bullish engulfing
                bull_eng = (close[i] > open_[i] and close[i - 1] < open_[i - 1] and
                            open_[i] < close[i - 1] and close[i] > open_[i - 1] and
                            body > prev_body * 1.5 and vol[i] > avg_vol)
                if bull_eng and (trend == "up" or near_support) and enabled("engulfing"):
                    self._sig(date_val, "BUY", close[i],
                              f"PA·看涨吞没: 阳包阴@{close[i]:.2f} 放量",
                              pattern="bullish_engulfing")

                # Bearish engulfing
                bear_eng = (close[i] < open_[i] and close[i - 1] > open_[i - 1] and
                            open_[i] > close[i - 1] and close[i] < open_[i - 1] and
                            body > prev_body * 1.5 and vol[i] > avg_vol)
                if bear_eng and (trend == "down" or near_resistance) and enabled("engulfing"):
                    self._sig(date_val, "SELL", close[i],
                              f"PA·看跌吞没: 阴包阳@{close[i]:.2f} 放量",
                              pattern="bearish_engulfing")

            # ---- Inside Bar Breakout ----
            if i >= 2:
                is_ib = (high[i - 1] < high[i - 2] and low[i - 1] > low[i - 2] and
                         candle_range < avg_range * 0.7)
                # Break above inside bar mother candle
                if is_ib and close[i] > high[i - 2] and trend == "up" and enabled("inside_bar"):
                    self._sig(date_val, "BUY", close[i],
                              f"PA·Inside Bar突破: 上破母K线{high[i-2]:.2f}",
                              pattern="ib_breakout_up")
                # Break below inside bar mother candle
                if is_ib and close[i] < low[i - 2] and trend == "down" and enabled("inside_bar"):
                    self._sig(date_val, "SELL", close[i],
                              f"PA·Inside Bar跌破: 下破母K线{low[i-2]:.2f}",
                              pattern="ib_breakout_down")

            # ---- Fakey / False Break (Trap) ----
            if i >= 4:
                # False break above resistance then reversal
                if resistance_levels:
                    nearest_r = min(resistance_levels, key=lambda r: abs(close[i-2] - r))
                    false_up = (high[i-2] > nearest_r * 1.01 and close[i-2] < nearest_r and
                                close[i] < close[i-1])
                    if false_up and enabled("fakey"):
                        self._sig(date_val, "SELL", close[i],
                                  f"PA·Fakey假突破: 上破{nearest_r:.2f}后回落 多头陷阱",
                                  pattern="fakey_sell")

                # False break below support then reversal
                if support_levels:
                    nearest_s = min(support_levels, key=lambda s: abs(close[i-2] - s))
                    false_down = (low[i-2] < nearest_s * 0.99 and close[i-2] > nearest_s and
                                  close[i] > close[i-1])
                    if false_down and enabled("fakey"):
                        self._sig(date_val, "BUY", close[i],
                                  f"PA·Fakey假跌破: 下破{nearest_s:.2f}后收复 空头陷阱",
                                  pattern="fakey_buy")

            # ---- Support-Resistance Flip ----
            if i >= 30:
                # Old resistance becomes new support
                for r in resistance_levels:
                    if abs(close[i] - r) / r < 0.015:
                        # Check if price was above R for 3+ bars
                        if np.all(close[max(0, i - 20):i] > r * 0.98):
                            if close[i] > r and close[i - 1] < r and enabled("sr_flip"):
                                self._sig(date_val, "BUY", close[i],
                                          f"PA·阻力变支撑: {r:.2f} 突破回踩确认")

                # Old support becomes new resistance
                for s in support_levels:
                    if abs(close[i] - s) / s < 0.015:
                        if np.all(close[max(0, i - 20):i] < s * 1.02):
                            if close[i] < s and close[i - 1] > s and enabled("sr_flip"):
                                self._sig(date_val, "SELL", close[i],
                                          f"PA·支撑变阻力: {s:.2f} 跌破反抽确认")

            # ---- Trendline Touch ----
            if trend == "up" and len(swings_l) >= 3:
                tl_lows = [close[l] for l in swings_l[-3:] if l < i]
                if len(tl_lows) >= 2:
                    # Rising trendline: connecting higher lows
                    tl_price = tl_lows[-1] + (tl_lows[-1] - tl_lows[0]) * 0.1
                    if abs(close[i] - tl_price) / tl_price < 0.01 and close[i] > tl_price and enabled("trendline"):
                        self._sig(date_val, "BUY", close[i],
                                  f"PA·上升趋势线支撑: {tl_price:.2f} 触线反弹")

            if trend == "down" and len(swings_h) >= 3:
                tl_highs = [close[h] for h in swings_h[-3:] if h < i]
                if len(tl_highs) >= 2:
                    tl_price = tl_highs[-1] - (tl_highs[0] - tl_highs[-1]) * 0.1
                    if abs(close[i] - tl_price) / tl_price < 0.01 and close[i] < tl_price and enabled("trendline"):
                        self._sig(date_val, "SELL", close[i],
                                  f"PA·下降趋势线压制: {tl_price:.2f} 触线回落")

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()
