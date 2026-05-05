"""
形态学 (Chart Pattern / Morphology) — 降龙十八掌

Core Techniques:
1. 双重顶/底 (Double Top/Bottom) — 亚当夏娃理论
2. 头肩顶/底 (Head & Shoulders Top/Bottom) — 最可靠的 reversal 形态
3. 三角形 (Triangles) — Ascending/Descending/Symmetrical
4. 旗形/三角旗 (Flags & Pennants) — 中继形态
5. 杯柄 (Cup & Handle) — William O'Neil
6. 楔形 (Wedges) — Rising/Falling
7. 矩形/箱体 (Rectangle/Box) — 横盘突破
8. 菱形/扩散形态 (Diamond/Broadening)
9. 目标测算 (Measured Move) — 形态高度投影
"""
import numpy as np
import pandas as pd
from .base import BaseSchool


class MorphologySchool(BaseSchool):
    name = "morphology"
    description = "形态学: 双顶底·头肩·三角·旗形·杯柄·目标测算"
    core_technique = "经典形态识别 + 颈线突破 + 量度目标"

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        swings_h = self.swing_highs(close, 6)
        swings_l = self.swing_lows(close, 6)

        avg_vol = self.sma(vol, 20)

        for i in range(100, len(close)):
            date_val = str(df.iloc[i].get("date", i))

            recent_h = [(h, close[h]) for h in swings_h if i - 80 < h <= i - 5]
            recent_l = [(l, close[l]) for l in swings_l if i - 80 < l <= i - 5]

            # ---- Double Bottom (W底) ----
            if len(recent_l) >= 4:
                l1_idx, l1_price = recent_l[-2]
                l2_idx, l2_price = recent_l[-1]
                # Two bottoms: similar price, separated by a high (neckline)
                similar_pct = abs(l2_price - l1_price) / l1_price
                if similar_pct < 0.04 and l2_idx - l1_idx >= 10:
                    # Find neckline (highest point between two bottoms)
                    mid_range = close[l1_idx:l2_idx + 1]
                    neckline = np.max(mid_range)
                    # Breakout above neckline
                    if close[i] > neckline and close[i - 1] <= neckline:
                        target = neckline + (neckline - min(l1_price, l2_price))
                        self._sig(date_val, "BUY", close[i],
                                  f"形态·W底突破: 颈线{neckline:.2f} 目标{target:.2f}",
                                  pattern="double_bottom", neckline=round(neckline, 2),
                                  target=round(target, 2))

            # ---- Double Top (M顶) ----
            if len(recent_h) >= 4:
                h1_idx, h1_price = recent_h[-2]
                h2_idx, h2_price = recent_h[-1]
                similar_pct = abs(h2_price - h1_price) / h1_price
                if similar_pct < 0.04 and h2_idx - h1_idx >= 10:
                    mid_range = close[h1_idx:h2_idx + 1]
                    neckline = np.min(mid_range)
                    if close[i] < neckline and close[i - 1] >= neckline:
                        target = neckline - (max(h1_price, h2_price) - neckline)
                        self._sig(date_val, "SELL", close[i],
                                  f"形态·M顶跌破: 颈线{neckline:.2f} 目标{target:.2f}",
                                  pattern="double_top", neckline=round(neckline, 2),
                                  target=round(target, 2))

            # ---- Head & Shoulders Bottom (头肩底) ----
            if len(recent_l) >= 5:
                ls = recent_l[-5:]
                left_shoulder = ls[0]
                head = min(ls[1:4], key=lambda x: x[1])
                right_shoulder = ls[-1]
                if (head[1] < left_shoulder[1] * 0.97 and
                    head[1] < right_shoulder[1] * 0.97 and
                    abs(right_shoulder[1] - left_shoulder[1]) / left_shoulder[1] < 0.05):
                    neckline = np.max(close[ls[0][0]:ls[-1][0] + 1])
                    if close[i] > neckline and close[i - 1] <= neckline:
                        target = neckline + (neckline - head[1])
                        self._sig(date_val, "BUY", close[i],
                                  f"形态·头肩底突破: 颈线{neckline:.2f} 目标{target:.2f}",
                                  pattern="head_shoulders_bottom", target=round(target, 2))

            # ---- Head & Shoulders Top (头肩顶) ----
            if len(recent_h) >= 5:
                hs = recent_h[-5:]
                left_shoulder = hs[0]
                head = max(hs[1:4], key=lambda x: x[1])
                right_shoulder = hs[-1]
                if (head[1] > left_shoulder[1] * 1.03 and
                    head[1] > right_shoulder[1] * 1.03 and
                    abs(right_shoulder[1] - left_shoulder[1]) / left_shoulder[1] < 0.05):
                    neckline = np.min(close[hs[0][0]:hs[-1][0] + 1])
                    if close[i] < neckline and close[i - 1] >= neckline:
                        target = neckline - (head[1] - neckline)
                        self._sig(date_val, "SELL", close[i],
                                  f"形态·头肩顶跌破: 颈线{neckline:.2f} 目标{target:.2f}",
                                  pattern="head_shoulders_top", target=round(target, 2))

            # ---- Triangle Breakout (三角形突破) ----
            if len(recent_h) >= 3 and len(recent_l) >= 3:
                h3 = recent_h[-3:]
                l3 = recent_l[-3:]
                h_prices = [x[1] for x in h3]
                l_prices = [x[1] for x in l3]
                h_range = max(h_prices) - min(h_prices)
                l_range = max(l_prices) - min(l_prices)

                # Ascending triangle: flat top, rising lows
                ascending = (h_range / np.mean(h_prices) < 0.03 and
                             l_prices[-1] > l_prices[0] * 1.01 and
                             vol[i] > avg_vol[i] * 1.3)
                if ascending and close[i] > np.mean(h_prices) and close[i - 1] <= np.mean(h_prices):
                    triangle_height = np.mean(h_prices) - l_prices[0]
                    target = np.mean(h_prices) + triangle_height
                    self._sig(date_val, "BUY", close[i],
                              f"形态·上升三角突破: 目标{target:.2f}",
                              pattern="ascending_triangle", target=round(target, 2))

                # Descending triangle: flat bottom, falling highs
                descending = (l_range / np.mean(l_prices) < 0.03 and
                              h_prices[-1] < h_prices[0] * 0.99 and
                              vol[i] > avg_vol[i] * 1.3)
                if descending and close[i] < np.mean(l_prices) and close[i - 1] >= np.mean(l_prices):
                    triangle_height = h_prices[0] - np.mean(l_prices)
                    target = np.mean(l_prices) - triangle_height
                    self._sig(date_val, "SELL", close[i],
                              f"形态·下降三角跌破: 目标{target:.2f}",
                              pattern="descending_triangle", target=round(target, 2))

                # Symmetrical triangle: converging highs and lows
                symm = (h_prices[-1] < h_prices[0] and l_prices[-1] > l_prices[0] and
                        h_range / np.mean(h_prices) > 0.02 and l_range / np.mean(l_prices) > 0.02)
                if symm:
                    upper_line = np.linspace(h_prices[0], h_prices[-1], i - h3[0][0])[-1]
                    lower_line = np.linspace(l_prices[0], l_prices[-1], i - l3[0][0])[-1]
                    if close[i] > upper_line and close[i - 1] <= upper_line:
                        height = max(h_prices) - min(l_prices)
                        target = close[i] + height
                        self._sig(date_val, "BUY", close[i],
                                  f"形态·对称三角上破: 目标{target:.2f}",
                                  pattern="sym_triangle_up", target=round(target, 2))
                    elif close[i] < lower_line and close[i - 1] >= lower_line:
                        height = max(h_prices) - min(l_prices)
                        target = close[i] - height
                        self._sig(date_val, "SELL", close[i],
                                  f"形态·对称三角下破: 目标{target:.2f}",
                                  pattern="sym_triangle_down", target=round(target, 2))

            # ---- Bull Flag / Bear Flag (旗形) ----
            if i >= 30:
                # Bull flag: strong pole up + consolidation channel down
                pole_up = close[i - 20] > close[i - 30] * 1.06  # 6%+ pole
                flag_consolidation = (
                    np.max(high[i - 15:i]) - np.min(low[i - 15:i])
                ) / np.mean(close[i - 15:i]) < 0.04  # Tight range
                flag_breakout = close[i] > np.max(high[i - 15:i])
                if pole_up and flag_consolidation and flag_breakout:
                    pole_height = close[i - 20] - close[i - 30]
                    target = close[i] + pole_height
                    self._sig(date_val, "BUY", close[i],
                              f"形态·牛旗突破: 旗杆{close[i-20]:.2f}-{close[i-30]:.2f} 目标{target:.2f}",
                              pattern="bull_flag", target=round(target, 2))

                # Bear flag: strong pole down + consolidation channel up
                pole_down = close[i - 20] < close[i - 30] * 0.94
                flag_breakdown = close[i] < np.min(low[i - 15:i])
                if pole_down and flag_consolidation and flag_breakdown:
                    pole_height = close[i - 30] - close[i - 20]
                    target = close[i] - pole_height
                    self._sig(date_val, "SELL", close[i],
                              f"形态·熊旗跌破: 目标{target:.2f}",
                              pattern="bear_flag", target=round(target, 2))

            # ---- Cup & Handle (杯柄) ----
            if i >= 60:
                cup_left = close[i - 60]
                cup_bottom = np.min(close[i - 55:i - 10])
                cup_right = close[i - 5]
                # Cup shape: U-shape, left≈right, bottom lower
                cup_valid = (abs(cup_right - cup_left) / cup_left < 0.06 and
                             cup_bottom < cup_left * 0.7 and
                             close[i - 5] > close[max(0, i - 10):i - 5].min())
                if cup_valid:
                    handle_low = np.min(low[i - 8:i])
                    handle_range = (np.max(high[i - 8:i]) - handle_low) / handle_low
                    if handle_range < 0.05 and close[i] > cup_left and close[i - 1] <= cup_left:
                        cup_depth = cup_left - cup_bottom
                        target = cup_left + cup_depth
                        self._sig(date_val, "BUY", close[i],
                                  f"形态·杯柄突破: 杯深{cup_depth:.2f} 目标{target:.2f}",
                                  pattern="cup_handle", target=round(target, 2))

            # ---- Rectangle / Box Breakout (箱体突破) ----
            if i >= 30:
                box_high = np.max(high[i - 30:i - 5])
                box_low = np.min(low[i - 30:i - 5])
                box_range_pct = (box_high - box_low) / box_low
                is_box = 0.03 < box_range_pct < 0.10
                # Up breakout
                if is_box and close[i] > box_high and close[i - 1] <= box_high and vol[i] > avg_vol[i] * 1.2:
                    target = box_high + (box_high - box_low)
                    self._sig(date_val, "BUY", close[i],
                              f"形态·箱体上破: 箱[{box_low:.2f}-{box_high:.2f}] 目标{target:.2f}",
                              pattern="box_breakout_up", target=round(target, 2))
                # Down breakout
                if is_box and close[i] < box_low and close[i - 1] >= box_low and vol[i] > avg_vol[i] * 1.2:
                    target = box_low - (box_high - box_low)
                    self._sig(date_val, "SELL", close[i],
                              f"形态·箱体下破: 箱[{box_low:.2f}-{box_high:.2f}] 目标{target:.2f}",
                              pattern="box_breakout_down", target=round(target, 2))

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()
