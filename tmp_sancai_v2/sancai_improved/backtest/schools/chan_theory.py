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

    SIGNAL_TOGGLES = [
        {"key": "buy_point_1", "label": "第一类买点 (一买)", "default": True},
        {"key": "buy_point_2", "label": "第二类买点 (二买)", "default": True},
        {"key": "buy_point_3", "label": "第三类买点 (三买)", "default": True},
        {"key": "sell_point_1", "label": "第一类卖点 (一卖)", "default": True},
        {"key": "sell_point_2", "label": "第二类卖点 (二卖)", "default": True},
        {"key": "sell_point_3", "label": "第三类卖点 (三卖)", "default": True},
    ]

    PARAMS = {
        "stroke_window": {
            "type": "int", "default": 5, "min": 3, "max": 10, "step": 1,
            "unit_label": "根K线",
            "theory_explanation": "笔的最小K线数量。缠论标准笔至少5根K线，减少可捕捉更细微转折，增加过滤噪音。3-4根适合高频震荡市，6-10根适合趋势市。",
        },
        "macd_fast": {
            "type": "int", "default": 12, "min": 8, "max": 19, "step": 1,
            "unit_label": "周期",
            "theory_explanation": "MACD快线EMA周期。默认12，降低更敏感可提前捕捉背驰，但假信号增多。缠论用MACD辅助判断背驰力度。",
        },
        "macd_slow": {
            "type": "int", "default": 26, "min": 20, "max": 34, "step": 1,
            "unit_label": "周期",
            "theory_explanation": "MACD慢线EMA周期。默认26，与快线配合决定DIF波动特征。缠论中DIF穿越DEA是中枢震荡的辅助判断依据。",
        },
        "macd_signal": {
            "type": "int", "default": 9, "min": 5, "max": 13, "step": 1,
            "unit_label": "周期",
            "theory_explanation": "MACD信号线(DEA)周期。默认9，越小DEA越灵敏，MACD柱变化越快。缠论用MACD柱面积比较判断背驰力度。",
        },
        "buy1_divergence_lookback": {
            "type": "int", "default": 60, "min": 30, "max": 120, "step": 5,
            "unit_label": "根K线",
            "theory_explanation": "一买背驰回溯窗口。一买需比较当前下跌段与前一下跌段的MACD面积，窗口决定比较范围。30适合短线，120适合大级别背驰判断。",
        },
        "buy2_hub_touch_margin": {
            "type": "float", "default": 2.0, "min": 0.5, "max": 5.0, "step": 0.5,
            "unit_label": "%",
            "theory_explanation": "二买回踩中枢下沿(ZD)的容差范围。容差过小可能错过优质二买，过大则失去中枢支撑意义。以ZD的百分比计。",
        },
        "buy3_breakout_margin": {
            "type": "float", "default": 2.0, "min": 1.0, "max": 5.0, "step": 0.5,
            "unit_label": "%",
            "theory_explanation": "三买突破中枢上沿(ZG)的确认幅度。需先有效突破ZG再回抽确认，幅度太小易被假突破欺骗，太大则入场过晚。",
        },
        "sell1_divergence_lookback": {
            "type": "int", "default": 60, "min": 30, "max": 120, "step": 5,
            "unit_label": "根K线",
            "theory_explanation": "一卖背驰回溯窗口。一卖需比较当前上涨段与前段MACD面积，窗口越小对短线顶背驰越敏感，越大越适合趋势背驰。",
        },
        "segment_overlap_min": {
            "type": "int", "default": 3, "min": 3, "max": 5, "step": 1,
            "unit_label": "段",
            "theory_explanation": "中枢最少重叠线段数。标准中枢由≥3段重叠构成，增加段数中枢更稳定但信号更少。3段为基准中枢，5段为高级别中枢。",
        },
    }

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        # ---- 读取微调参数 ----
        self._sw = self.get_param(config, "stroke_window")
        self._macd_f = self.get_param(config, "macd_fast")
        self._macd_s = self.get_param(config, "macd_slow")
        self._macd_sig = self.get_param(config, "macd_signal")
        self._bp1_lb = self.get_param(config, "buy1_divergence_lookback")
        self._bp2_margin = self.get_param(config, "buy2_hub_touch_margin") / 100.0
        self._bp3_margin = self.get_param(config, "buy3_breakout_margin") / 100.0
        self._sp1_lb = self.get_param(config, "sell1_divergence_lookback")
        self._seg_min = self.get_param(config, "segment_overlap_min")

        # Step 1: Process inclusion and build strokes (笔)
        strokes = self._build_strokes(high, low, close)

        if len(strokes) < 5:
            return pd.DataFrame()

        # Step 2: Build segments (线段) from strokes
        segments = self._build_segments(strokes)

        if len(segments) < self._seg_min:
            return pd.DataFrame()

        # Step 3: Find hubs (中枢) — overlapping zones of N+ consecutive segments
        hubs = self._find_hubs(segments)

        # Step 4: MACD for divergence detection
        dif, dea, macd_bar = self.macd(close, self._macd_f, self._macd_s, self._macd_sig)
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

    def position_analysis(self, df: pd.DataFrame, config: dict = None) -> dict:
        """分析当前价格在缠论结构中的位置：中枢/笔/买卖点距离."""
        # 初始化默认参数（兼容直接调用而不经过 generate_signals）
        self._sw = self.get_param(config, "stroke_window")
        self._macd_f = self.get_param(config, "macd_fast")
        self._macd_s = self.get_param(config, "macd_slow")
        self._macd_sig = self.get_param(config, "macd_signal")
        self._bp1_lb = self.get_param(config, "buy1_divergence_lookback")
        self._bp2_margin = self.get_param(config, "buy2_hub_touch_margin") / 100.0
        self._bp3_margin = self.get_param(config, "buy3_breakout_margin") / 100.0
        self._sp1_lb = self.get_param(config, "sell1_divergence_lookback")
        self._seg_min = self.get_param(config, "segment_overlap_min")

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        current_price = float(close[-1])

        # Build internal structures
        strokes = self._build_strokes(high, low, close)
        segments = self._build_segments(strokes) if len(strokes) >= 5 else []
        hubs = self._find_hubs(segments) if len(segments) >= self._seg_min else []

        result = {
            "school": "chan_theory",
            "current_price": round(current_price, 2),
            "trend": self._detect_trend(df),
            "strokes_count": len(strokes),
            "segments_count": len(segments),
            "hubs": [],
            "position_summary": "",
            "nearest_buy_point": None,
            "nearest_sell_point": None,
        }

        # --- Hub position analysis ---
        hub_infos = []
        active_hub = None
        for h in hubs:
            info = {
                "zg": round(h["zg"], 2),
                "zd": round(h["zd"], 2),
                "center": round(h["center"], 2),
                "width": round(h["zg"] - h["zd"], 2),
            }
            if current_price > h["zg"]:
                info["position"] = "above"
                info["distance_pct"] = round((current_price - h["zg"]) / h["zg"] * 100, 2)
                info["note"] = f"价格在中枢上方{info['distance_pct']:.1f}%"
            elif current_price < h["zd"]:
                info["position"] = "below"
                info["distance_pct"] = round((h["zd"] - current_price) / h["zd"] * 100, 2)
                info["note"] = f"价格在中枢下方{info['distance_pct']:.1f}%"
            else:
                info["position"] = "inside"
                info["note"] = "价格运行于中枢内部"
            hub_infos.append(info)
            # Track the most recent hub that overlaps current price range
            if h["start_idx"] <= len(close) - 1 and (
                active_hub is None or h["start_idx"] > active_hub["start_idx"]
            ):
                if current_price <= h["zg"] * 1.05 and current_price >= h["zd"] * 0.95:
                    active_hub = h

        result["hubs"] = hub_infos[-3:] if len(hub_infos) > 3 else hub_infos

        # --- Active hub summary ---
        if active_hub:
            zg, zd = round(active_hub["zg"], 2), round(active_hub["zd"], 2)
            if current_price > zg:
                result["position_summary"] = f"突破中枢上沿{zg}，上行空间打开"
            elif current_price < zd:
                result["position_summary"] = f"跌破中枢下沿{zd}，下行风险"
            else:
                result["position_summary"] = f"中枢震荡[{zd}-{zg}]，等待方向选择"
        else:
            result["position_summary"] = "无有效中枢覆盖，趋势运行中"

        # --- Nearest stroke/segment levels ---
        if strokes:
            last_stroke = strokes[-1]
            stroke_dir = "向上笔" if last_stroke[2] == "H" else "向下笔"
            result["last_stroke"] = {
                "type": stroke_dir,
                "start_price": round(last_stroke[1], 2),
                "end_price": round(close[-1], 2),
                "range_pct": round(abs(close[-1] - last_stroke[1]) / last_stroke[1] * 100, 2),
            }
            # Find nearest support/resistance from stroke highs/lows
            stroke_highs = sorted([s[1] for s in strokes if s[2] == "H"], reverse=True)
            stroke_lows = sorted([s[1] for s in strokes if s[2] == "L"])
            nearest_resistance = next((h for h in stroke_highs if h > current_price * 1.005), None)
            nearest_support = next((l for l in reversed(stroke_lows) if l < current_price * 0.995), None)
            if nearest_resistance:
                result["nearest_resistance"] = round(nearest_resistance, 2)
                result["resistance_distance_pct"] = round((nearest_resistance - current_price) / current_price * 100, 2)
            if nearest_support:
                result["nearest_support"] = round(nearest_support, 2)
                result["support_distance_pct"] = round((current_price - nearest_support) / current_price * 100, 2)

        # --- Check recent 买卖点 发生情况 ---
        recent_signals = self._find_recent_signal_positions(
            close, high, low, df, strokes, segments, hubs
        )
        result["recent_signals"] = recent_signals

        # --- MACD status ---
        dif, dea, macd_bar = self.macd(close, self._macd_f, self._macd_s, self._macd_sig)
        if len(dif) > 0:
            result["macd"] = {
                "dif": round(float(dif[-1]), 4),
                "dea": round(float(dea[-1]), 4),
                "bar": round(float(macd_bar[-1]), 4),
                "status": "金叉" if dif[-1] > dea[-1] else "死叉",
            }

        return result

    def _find_recent_signal_positions(self, close, high, low, df, strokes, segments, hubs):
        """查找最近可能出现的买卖点信号（仅结构位置，不检测MACD背驰）."""
        recent = []
        if not segments:
            return recent
        # Check last 80 bars for signals
        check_end = min(len(close), 400)
        check_start = max(100, check_end - 80)
        for i in range(check_start, check_end):
            try:
                # Buy point 1 (needs macd_bar)
                bp1 = self._check_buy_point_1(i, close, None, low, segments, hubs)
                if bp1:
                    recent.append({
                        "type": "第一类买点", "direction": "buy",
                        "bar": i, "price": round(float(close[i]), 2),
                        "bars_ago": check_end - i,
                    })
                # Buy point 2: (idx, close, low, segments, hubs)
                if self._check_buy_point_2(i, close, low, segments, hubs):
                    recent.append({
                        "type": "第二类买点", "direction": "buy",
                        "bar": i, "price": round(float(close[i]), 2),
                        "bars_ago": check_end - i,
                    })
                # Buy point 3: (idx, close, high, low, hubs)
                if self._check_buy_point_3(i, close, high, low, hubs):
                    recent.append({
                        "type": "第三类买点", "direction": "buy",
                        "bar": i, "price": round(float(close[i]), 2),
                        "bars_ago": check_end - i,
                    })
                # Sell point 1: (idx, close, macd_bar, high, segments, hubs)
                if self._check_sell_point_1(i, close, None, high, segments, hubs):
                    recent.append({
                        "type": "第一类卖点", "direction": "sell",
                        "bar": i, "price": round(float(close[i]), 2),
                        "bars_ago": check_end - i,
                    })
                # Sell point 2: (idx, close, high, hubs)
                if self._check_sell_point_2(i, close, high, hubs):
                    recent.append({
                        "type": "第二类卖点", "direction": "sell",
                        "bar": i, "price": round(float(close[i]), 2),
                        "bars_ago": check_end - i,
                    })
                # Sell point 3: (idx, close, low, hubs)
                if self._check_sell_point_3(i, close, low, hubs):
                    recent.append({
                        "type": "第三类卖点", "direction": "sell",
                        "bar": i, "price": round(float(close[i]), 2),
                        "bars_ago": check_end - i,
                    })
            except Exception:
                continue
        return recent[-5:] if len(recent) > 5 else recent

    # ---- 包含关系 & 笔 ----
    def _build_strokes(self, high, low, close):
        """Build strokes (笔) from K-lines with inclusion processing."""
        n = len(close)
        w = self._sw
        strokes = []  # [(idx, price, type)]
        direction = 0  # 1=up, -1=down
        ext_start = 0

        for i in range(w, n - w):
            # Going up: local high
            if close[i] > close[i - w] and high[i] >= np.max(high[i - (w - 1):i + 1]):
                if high[i] == np.max(high[max(0, i - (w - 2)):min(n, i + w - 1)]):
                    if direction != 1:
                        if direction == -1 and ext_start > 0:
                            strokes.append((ext_start, low[ext_start], "L"))
                        direction = 1
                    ext_start = i
            # Going down: local low
            elif close[i] < close[i - w] and low[i] <= np.min(low[i - (w - 1):i + 1]):
                if low[i] == np.min(low[max(0, i - (w - 2)):min(n, i + w - 1)]):
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
        """Find hubs where N+ consecutive segments overlap (N = segment_overlap_min)."""
        hubs = []
        n = self._seg_min
        for i in range(len(segments) - (n - 1)):
            segs = segments[i:i + n]
            overlap_high = min(s["price_range"][1] for s in segs)
            overlap_low = max(s["price_range"][0] for s in segs)
            if overlap_low < overlap_high:
                hubs.append({
                    "zg": overlap_high,
                    "zd": overlap_low,
                    "center": (overlap_high + overlap_low) / 2,
                    "start_idx": segs[0]["start_idx"],
                    "end_idx": segs[-1]["end_idx"],
                    "segments": segs,
                })
        return hubs

    # ---- 三类买点检测 ----
    def _check_buy_point_1(self, idx, close, macd_bar, low, segments, hubs):
        """第一类买点: 下跌趋势背驰"""
        if macd_bar is None:
            return False
        if idx < self._bp1_lb:
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
        # Price pulls back near hub bottom zone (param-controlled margin)
        m = self._bp2_margin
        near_zd = close[idx] <= hub["zd"] * (1 + m) and close[idx] >= hub["zd"] * (1 - m)
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
        m = self._bp3_margin
        # Must have broken above hub
        broke_out = np.max(high[start:idx]) > hub["zg"] * (1 + m)
        # Now pulling back to hub top zone
        pullback = hub["zd"] * (1 - m) < close[idx] < hub["zg"] * (1 + m)
        return broke_out and pullback

    # ---- 三类卖点检测 ----
    def _check_sell_point_1(self, idx, close, macd_bar, high, segments, hubs):
        """第一类卖点: 上涨趋势背驰"""
        if macd_bar is None:
            return False
        if idx < self._sp1_lb:
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
        m = self._bp2_margin
        near_zg = hub["zg"] * (1 - m) <= close[idx] <= hub["zg"] * (1 + m)
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
        m = self._bp2_margin
        broke_down = np.min(low[start:idx]) < hub["zd"] * (1 - m)
        retest = hub["zd"] * (1 - m) < close[idx] < hub["zd"] * (1 + m)
        return broke_down and retest
