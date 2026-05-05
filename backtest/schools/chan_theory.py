"""
缠论 (Chan Theory) — 乾坤大挪移
Creator: 缠中说禅 (Chan Zhongshuo Chan)

Core Techniques:
1. 包含关系处理 (Inclusion Processing) — K-line merging
2. 笔 (Bi / Stroke) — 5 consecutive bars, top/bottom formed
3. 线段 (Segment) — formed by at least 3 strokes
4. 中枢 (Hub / Pivot Zone) — overlapping zone of 3+ segments
5. 背驰 (Divergence) — MACD area comparison between segments
6. 三类买卖点:
   - 第一类买点: 下跌趋势背驰后，一买 (First Buy: after downtrend divergence)
   - 第二类买点: 回踩中枢不破，二买 (Second Buy: pullback to hub support)
   - 第三类买点: 突破中枢后回抽，三买 (Third Buy: breakout pullback to hub top)
7. 级别联立 (Multi-timeframe alignment)
"""
import numpy as np
import pandas as pd
from .base import BaseSchool


class ChanTheorySchool(BaseSchool):
    name = "chan_theory"
    description = "缠论: 笔-线段-中枢-背驰·三类买卖点"
    core_technique = "中枢背驰 + 三类买卖点"

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        # Step 1: Process inclusion and build strokes (笔)
        strokes = self._build_strokes(high, low, close)

        if len(strokes) < 5:
            return pd.DataFrame()

        # Step 2: Build segments (线段) from strokes
        segments = self._build_segments(strokes)

        if len(segments) < 3:
            return pd.DataFrame()

        # Step 3: Find hubs (中枢) — overlapping zones of 3+ consecutive segments
        hubs = self._find_hubs(segments)

        # Step 4: MACD for divergence detection
        dif, dea, macd_bar = self.macd(close)
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))

        # Step 5: Detect 三类买卖点
        for i in range(100, len(close)):
            date_val = df.iloc[i].get("date", i)

            # --- 第一类买点 (1st Buy Point) ---
            # Downtrend divergence: price makes lower low but MACD histogram makes higher low
            # Check at segment low points
            bp1 = self._check_buy_point_1(i, close, macd_bar, low, segments, hubs)
            if bp1 and enabled("buy_point_1"):
                self._sig(date_val, "BUY", close[i],
                          f"缠论·一买: 下跌背驰 @{close[i]:.2f}",
                          buy_type="first", stop_loss=round(close[i]*0.95, 2))

            # --- 第二类买点 (2nd Buy Point) ---
            # After 1st buy confirmed, pullback to hub bottom without breaking
            bp2 = self._check_buy_point_2(i, close, low, segments, hubs)
            if bp2 and enabled("buy_point_2"):
                self._sig(date_val, "BUY", close[i],
                          f"缠论·二买: 回踩中枢不破 @{close[i]:.2f}",
                          buy_type="second", stop_loss=round(close[i]*0.95, 2))

            # --- 第三类买点 (3rd Buy Point) ---
            # Break above hub then pull back to hub top without entering hub
            bp3 = self._check_buy_point_3(i, close, high, low, hubs)
            if bp3 and enabled("buy_point_3"):
                self._sig(date_val, "BUY", close[i],
                          f"缠论·三买: 突破中枢回抽确认 @{close[i]:.2f}",
                          buy_type="third", stop_loss=round(close[i]*0.97, 2))

            # --- 卖点 (Sell Points) ---
            # 1st Sell: uptrend divergence (higher high + weaker MACD)
            sp1 = self._check_sell_point_1(i, close, macd_bar, high, segments, hubs)
            if sp1 and enabled("sell_point_1"):
                self._sig(date_val, "SELL", close[i],
                          f"缠论·一卖: 上涨背驰 @{close[i]:.2f}",
                          sell_type="first")

            # 2nd Sell: rally to hub top and rejected
            sp2 = self._check_sell_point_2(i, close, high, hubs)
            if sp2 and enabled("sell_point_2"):
                self._sig(date_val, "SELL", close[i],
                          f"缠论·二卖: 反弹中枢顶受阻 @{close[i]:.2f}",
                          sell_type="second")

            # 3rd Sell: break below hub + retest
            sp3 = self._check_sell_point_3(i, close, low, hubs)
            if sp3 and enabled("sell_point_3"):
                self._sig(date_val, "SELL", close[i],
                          f"缠论·三卖: 跌破中枢反抽确认 @{close[i]:.2f}",
                          sell_type="third")

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()

    # ---- 包含关系 & 笔 ----
    def _build_strokes(self, high, low, close):
        """Build strokes (笔) from K-lines with inclusion processing."""
        n = len(close)
        # Simplified: detect directional changes of 5+ bars
        strokes = []  # [(idx, price, type)]
        direction = 0  # 1=up, -1=down
        ext_start = 0

        for i in range(5, n - 5):
            # Going up: local high
            if close[i] > close[i - 5] and high[i] >= np.max(high[i - 4:i + 1]):
                if high[i] == np.max(high[max(0, i - 3):min(n, i + 4)]):
                    if direction != 1:
                        if direction == -1 and ext_start > 0:
                            strokes.append((ext_start, low[ext_start], "L"))
                        direction = 1
                    ext_start = i
            # Going down: local low
            elif close[i] < close[i - 5] and low[i] <= np.min(low[i - 4:i + 1]):
                if low[i] == np.min(low[max(0, i - 3):min(n, i + 4)]):
                    if direction != -1:
                        if direction == 1 and ext_start > 0:
                            strokes.append((ext_start, high[ext_start], "H"))
                        direction = -1
                    ext_start = i

        return strokes

    # ---- 线段 ----
    def _build_segments(self, strokes):
        """Group strokes into segments (at least 3 strokes per segment)."""
        segments = []
        for i in range(len(strokes) - 2):
            s0, s1, s2 = strokes[i], strokes[i + 1], strokes[i + 2]
            # Segment: alternating H-L-H or L-H-L
            if s0[2] != s1[2] and s1[2] != s2[2]:
                segments.append({
                    "start_idx": s0[0], "start_price": s0[1], "start_type": s0[2],
                    "end_idx": s2[0], "end_price": s2[1], "end_type": s2[2],
                    "mid_idx": s1[0], "mid_price": s1[1],
                    "price_range": (min(s0[1], s1[1], s2[1]), max(s0[1], s1[1], s2[1])),
                })
        return segments

    # ---- 中枢 (Hub) ----
    def _find_hubs(self, segments):
        """Find hubs where 3+ consecutive segments overlap."""
        hubs = []
        for i in range(len(segments) - 2):
            s1, s2, s3 = segments[i], segments[i + 1], segments[i + 2]
            # Overlap zone: min of highs vs max of lows
            overlap_high = min(s1["price_range"][1], s2["price_range"][1], s3["price_range"][1])
            overlap_low = max(s1["price_range"][0], s2["price_range"][0], s3["price_range"][0])
            if overlap_low < overlap_high:  # Valid hub
                hubs.append({
                    "zg": overlap_high,  # 中枢上沿
                    "zd": overlap_low,   # 中枢下沿
                    "center": (overlap_high + overlap_low) / 2,
                    "start_idx": s1["start_idx"],
                    "end_idx": s3["end_idx"],
                    "segments": [s1, s2, s3],
                })
        return hubs

    # ---- 三类买点检测 ----
    def _check_buy_point_1(self, idx, close, macd_bar, low, segments, hubs):
        """第一类买点: 下跌趋势背驰"""
        if idx < 60:
            return False
        # Find if we're at a segment low
        recent_seg = [s for s in segments if s["end_idx"] <= idx and idx - s["end_idx"] < 20]
        if not recent_seg or recent_seg[-1]["end_type"] != "L":
            return False

        seg_end = recent_seg[-1]["end_idx"]
        # Compare: current low vs previous low + MACD divergence
        prev_lows = [s for s in segments if s["end_type"] == "L" and s["end_idx"] < seg_end]
        if len(prev_lows) < 2:
            return False

        prev_seg = prev_lows[-2]
        # Price: lower low
        price_ll = close[seg_end] < prev_seg["end_price"]
        # MACD: higher low (divergence)
        macd_hl = macd_bar[seg_end] > macd_bar[prev_seg["end_idx"]]

        return price_ll and macd_hl

    def _check_buy_point_2(self, idx, close, low, segments, hubs):
        """第二类买点: 回踩中枢下沿不破"""
        if not hubs:
            return False
        hub = hubs[-1]
        if idx < hub["end_idx"] + 5:
            return False
        # Price pulls back near hub bottom zone
        near_zd = close[idx] <= hub["zd"] * 1.02 and close[idx] >= hub["zd"] * 0.98
        # Reversal candle: close higher than open or hammer
        reversal = close[idx] > low[idx] + (close[idx] - low[idx]) * 0.6
        return near_zd and reversal and idx > hub["end_idx"]

    def _check_buy_point_3(self, idx, close, high, low, hubs):
        """第三类买点: 突破中枢后回抽中枢上沿"""
        if not hubs or idx < 50:
            return False
        hub = hubs[-1]
        start = hub["end_idx"]
        if idx <= start + 5:
            return False
        # Must have broken above hub
        broke_out = np.max(high[start:idx]) > hub["zg"] * 1.02
        # Now pulling back to hub top zone
        pullback = hub["zd"] * 0.99 < close[idx] < hub["zg"] * 1.02
        return broke_out and pullback

    # ---- 三类卖点检测 ----
    def _check_sell_point_1(self, idx, close, macd_bar, high, segments, hubs):
        """第一类卖点: 上涨趋势背驰"""
        if idx < 60:
            return False
        recent_seg = [s for s in segments if s["end_idx"] <= idx and idx - s["end_idx"] < 20]
        if not recent_seg or recent_seg[-1]["end_type"] != "H":
            return False
        seg_end = recent_seg[-1]["end_idx"]
        prev_highs = [s for s in segments if s["end_type"] == "H" and s["end_idx"] < seg_end]
        if len(prev_highs) < 2:
            return False
        prev_seg = prev_highs[-2]
        price_hh = close[seg_end] > prev_seg["end_price"]
        macd_lh = macd_bar[seg_end] < macd_bar[prev_seg["end_idx"]]
        return price_hh and macd_lh

    def _check_sell_point_2(self, idx, close, high, hubs):
        """第二类卖点: 反弹中枢上沿受阻"""
        if not hubs or idx < 20:
            return False
        hub = hubs[-1]
        near_zg = hub["zg"] * 0.98 <= close[idx] <= hub["zg"] * 1.02
        rejection = high[idx] > hub["zg"] and close[idx] < hub["zg"]
        return near_zg and rejection

    def _check_sell_point_3(self, idx, close, low, hubs):
        """第三类卖点: 跌破中枢后反抽中枢下沿"""
        if not hubs or idx < 30:
            return False
        hub = hubs[-1]
        start = hub["end_idx"]
        if start >= idx:
            return False
        broke_down = np.min(low[start:idx]) < hub["zd"] * 0.98
        retest = hub["zd"] * 0.98 < close[idx] < hub["zd"] * 1.02
        return broke_down and retest
