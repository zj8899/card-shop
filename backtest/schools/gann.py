"""
江恩理论 (Gann Theory) — 九阴真经 + 九阳神功
Creator: W.D. Gann

Core Techniques:
1. Gann Angles (甘氏角度线):
   - 1x1 (45°): 1 unit price per 1 unit time (most important)
   - 1x2: 1 price per 2 time (shallower, support)
   - 2x1: 2 price per 1 time (steeper, resistance)
   - 1x4, 4x1, 1x8, 8x1
2. Square of 9 (九方图):
   - Cardinal Cross (十字线): 0°/90°/180°/270°
   - Ordinal Cross (对角线): 45°/135°/225°/315°
3. Gann Fan: Multiple angles from pivot points
4. Time Cycles: 30, 45, 60, 90, 120, 144, 180, 270, 360
5. Retracement levels: 1/8, 2/8, 3/8, 4/8 (50%), 5/8, 6/8, 7/8
6. Price-Time Squaring: When price = time
7. Seasonal cycles (seasonal dates)
"""
import numpy as np
import pandas as pd
from .base import BaseSchool


class GannSchool(BaseSchool):
    name = "gann"
    description = "江恩理论: Gann角度线+九方图+时间周期+八分法"
    core_technique = "Gann 1x1角度线 + 九方图十字线 + 时间窗口"

    # Gann critical angles
    GANN_ANGLES = {
        "8x1": 8.0, "4x1": 4.0, "3x1": 3.0, "2x1": 2.0,
        "1x1": 1.0,
        "1x2": 0.5, "1x3": 0.333, "1x4": 0.25, "1x8": 0.125,
    }

    # Gann retracement eighths
    GANN_EIGHTHS = [0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        atr = self.atr(high, low, close, 14)
        swings_l = self.swing_lows(close, 5)
        swings_h = self.swing_highs(close, 5)

        for i in range(120, len(close)):
            date_val = str(df.iloc[i].get("date", i))

            # Find most recent significant low as pivot
            recent_pivot_lows = [l for l in swings_l if l < i - 10]
            if not recent_pivot_lows:
                continue
            pivot_idx = recent_pivot_lows[-1]
            pivot_price = low[pivot_idx]

            bars_from_pivot = i - pivot_idx
            if bars_from_pivot < 5:
                continue

            # Average True Range as the unit of price/time
            unit_atr = np.nanmean(atr[max(0, i - 10):i + 1]) or 0.01 * close[i]

            # ---- Gann Fan Angles from pivot ----
            gann_lines = {}
            for name, slope in self.GANN_ANGLES.items():
                gann_lines[name] = pivot_price + bars_from_pivot * unit_atr * slope

            # ---- Gann Retracement Levels (Eighths) ----
            recent_swing_range = np.max(high[pivot_idx:i + 1]) - np.min(low[pivot_idx:i + 1])
            if recent_swing_range > 0:
                range_high = np.max(high[pivot_idx:i + 1])
                range_low = np.min(low[pivot_idx:i + 1])
                gann_retracements = {
                    f"{int(r*8)}/8": range_high - recent_swing_range * r
                    for r in self.GANN_EIGHTHS
                }
            else:
                gann_retracements = {}

            # ---- Buy: Bounce from Gann angle support ----
            # Support at 1x2 or 1x3 (shallow angles)
            support_1x2 = gann_lines.get("1x2", close[i] * 2)
            support_1x3 = gann_lines.get("1x3", close[i] * 2)
            support_1x4 = gann_lines.get("1x4", close[i] * 2)

            bounce_1x2 = (low[i - 1] < support_1x2 and close[i] > support_1x2 and close[i] > close[i - 1])
            bounce_1x3 = (low[i - 1] < support_1x3 and close[i] > support_1x3 and close[i] > close[i - 1])
            bounce_1x4 = (low[i - 1] < support_1x4 and close[i] > support_1x4 and close[i] > close[i - 1])

            if bounce_1x2 and enabled("angle_support"):
                self._sig(date_val, "BUY", close[i],
                          f"江恩·1x2角度线支撑反弹: {support_1x2:.2f}",
                          gann_angle="1x2", angle_price=round(support_1x2, 2))
            elif bounce_1x3 and enabled("angle_support"):
                self._sig(date_val, "BUY", close[i],
                          f"江恩·1x3角度线支撑反弹: {support_1x3:.2f}",
                          gann_angle="1x3", angle_price=round(support_1x3, 2))
            elif bounce_1x4 and enabled("angle_support"):
                self._sig(date_val, "BUY", close[i],
                          f"江恩·1x4角度线支撑反弹: {support_1x4:.2f}",
                          gann_angle="1x4", angle_price=round(support_1x4, 2))

            # ---- Buy: Retracement to key Gann levels ----
            for level_name, level_price in gann_retracements.items():
                if level_name in ("3/8", "4/8", "5/8"):
                    touch_bounce = (abs(low[i] - level_price) / level_price < 0.01 and
                                    close[i] > open_[i] and vol[i] > np.mean(vol[max(0, i - 10):i]))
                    if touch_bounce and enabled("retrace_levels"):
                        self._sig(date_val, "BUY", close[i],
                                  f"江恩·{level_name}回调位支撑: {level_price:.2f}",
                                  retracement=level_name, level_price=round(level_price, 2))

            # ---- Sell: Resistance at Gann angles or retracements ----
            resist_2x1 = gann_lines.get("2x1", 0)
            resist_3x1 = gann_lines.get("3x1", 0)
            resist_1x1 = gann_lines.get("1x1", 0)

            reject_2x1 = (high[i - 1] > resist_2x1 and close[i] < resist_2x1)
            reject_3x1 = (high[i - 1] > resist_3x1 and close[i] < resist_3x1)
            reject_1x1 = (high[i - 1] > resist_1x1 and close[i] < resist_1x1)

            if reject_2x1 and enabled("angle_support"):
                self._sig(date_val, "SELL", close[i],
                          f"江恩·2x1角度线阻力回落: {resist_2x1:.2f}",
                          gann_angle="2x1")
            elif reject_3x1 and enabled("angle_support"):
                self._sig(date_val, "SELL", close[i],
                          f"江恩·3x1角度线阻力回落: {resist_3x1:.2f}",
                          gann_angle="3x1")
            elif reject_1x1 and enabled("angle_support"):
                self._sig(date_val, "SELL", close[i],
                          f"江恩·1x1主角度线阻力: {resist_1x1:.2f}",
                          gann_angle="1x1")

            # Sell at retracement resistance
            for level_name, level_price in gann_retracements.items():
                if level_name in ("5/8", "6/8", "7/8"):
                    touch_reject = (abs(high[i] - level_price) / level_price < 0.01 and
                                    close[i] < open_[i])
                    if touch_reject and enabled("retrace_levels"):
                        self._sig(date_val, "SELL", close[i],
                                  f"江恩·{level_name}反弹位阻力: {level_price:.2f}",
                                  retracement=level_name)

            # ---- Time Cycle Windows ----
            # Gann significant time cycles
            gann_cycles = [30, 45, 60, 90, 120, 144, 180, 270, 360]
            for cycle in gann_cycles:
                if bars_from_pivot == cycle:
                    # At cycle completion, look for reversal
                    if close[i] > close[i - 1] and close[i - 1] < close[i - 2] and enabled("time_cycles"):
                        self._sig(date_val, "BUY", close[i],
                                  f"江恩·{cycle}天周期窗口+反转: 自低点{pivot_price:.2f}",
                                  time_cycle=cycle)
                    elif close[i] < close[i - 1] and close[i - 1] > close[i - 2] and enabled("time_cycles"):
                        self._sig(date_val, "SELL", close[i],
                                  f"江恩·{cycle}天周期窗口+见顶: 自低点{pivot_price:.2f}",
                                  time_cycle=cycle)
                    break

            # ---- Square of 9 - Cardinal Cross ----
            # Simplified: check if price is near a square-of-9 cardinal level
            if i >= 20:
                price_range = np.max(high[max(0, i - 50):i]) - np.min(low[max(0, i - 50):i])
                base_price = np.min(low[max(0, i - 50):i])
                # Cardinal cross: 0°, 90°, 180°, 270° from base
                cardinal_levels = []
                for deg in [0, 90, 180, 270]:
                    ring = int(np.sqrt(price_range))
                    level_p = base_price + deg / 360 * price_range
                    cardinal_levels.append(level_p)

                for cl in cardinal_levels:
                    if abs(close[i] - cl) / cl < 0.01:
                        if close[i] > close[i - 1] and enabled("square_of_nine"):
                            self._sig(date_val, "BUY", close[i],
                                      f"江恩·九方图十字线支撑: {cl:.2f}")
                        else:
                            self._sig(date_val, "SELL", close[i],
                                      f"江恩·九方图十字线阻力: {cl:.2f}")
                        break

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()
