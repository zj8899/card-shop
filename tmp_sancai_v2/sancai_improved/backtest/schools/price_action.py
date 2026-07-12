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

    SIGNAL_TOGGLES = [
        {"key": "pin_bar", "label": "Pin Bar (锤子线/流星)", "default": True},
        {"key": "engulfing", "label": "吞没形态 (Engulfing)", "default": True},
        {"key": "inside_bar", "label": "Inside Bar突破", "default": True},
        {"key": "fakey", "label": "Fakey假突破陷阱", "default": True},
        {"key": "sr_flip", "label": "支撑阻力互换 (S/R Flip)", "default": True},
        {"key": "trendline", "label": "趋势线触碰反弹", "default": True},
    ]

    PARAMS = {
        "pin_bar_wick_pct": {
            "type": "float", "default": 60.0, "min": 50.0, "max": 80.0, "step": 5.0,
            "unit_label": "%",
            "theory_explanation": "Pin Bar影线占比下限。影线占总波动范围的比例，影线越长表示价格拒绝越强烈。60%为经典Pin Bar标准，80%为严格标准减少假信号。",
        },
        "pin_bar_body_max_pct": {
            "type": "float", "default": 30.0, "min": 10.0, "max": 40.0, "step": 5.0,
            "unit_label": "%",
            "theory_explanation": "Pin Bar实体占比上限。实体越小表示方向性越弱，反转意味越强。30%为标准，10%为极严格标准(几乎纯十字星+长影线)。",
        },
        "engulfing_body_mult": {
            "type": "float", "default": 1.5, "min": 1.2, "max": 3.0, "step": 0.1,
            "unit_label": "倍",
            "theory_explanation": "吞没形态实体倍数。多头吞没的阳线实体需大于前阴线实体的N倍，倍数越大信号越强但信号越少。1.5为经典标准。",
        },
        "engulfing_vol_min_mult": {
            "type": "float", "default": 1.0, "min": 0.8, "max": 2.0, "step": 0.1,
            "unit_label": "倍",
            "theory_explanation": "吞没形态最小放量倍数。成交量需大于均量的N倍以确认主力参与。1.0为均量线，1.5以上为显著放量确认。",
        },
        "sr_touch_pct": {
            "type": "float", "default": 2.0, "min": 1.0, "max": 5.0, "step": 0.5,
            "unit_label": "%",
            "theory_explanation": "支撑阻力触碰容差。价格距S/R水平的距离，以百分比计。容差越小要求越精确但可能错过，容差越大信号越多但可靠性下降。",
        },
        "swing_window": {
            "type": "int", "default": 6, "min": 3, "max": 10, "step": 1,
            "unit_label": "根K线",
            "theory_explanation": "摆动高低点识别窗口。价格行为的S/R水平依赖摆动点，窗口大小决定支撑阻力的级别。3-5适合短线S/R，6-10适合波段S/R。",
        },
        "fakey_break_pct": {
            "type": "float", "default": 1.0, "min": 0.5, "max": 3.0, "step": 0.5,
            "unit_label": "%",
            "theory_explanation": "Fakey假突破幅度阈值。价格短暂突破S/R后迅速回落的幅度，决定什么是有效的假突破。1%为标准假突破，0.5%捕捉细微陷阱。",
        },
        "trendline_touch_pct": {
            "type": "float", "default": 1.0, "min": 0.5, "max": 3.0, "step": 0.5,
            "unit_label": "%",
            "theory_explanation": "趋势线触碰容差。价格距离趋势线的判定范围，越小要求越精确但可能错过有效触碰。1%为标准容差。",
        },
        "inside_bar_range_max_pct": {
            "type": "float", "default": 70.0, "min": 50.0, "max": 90.0, "step": 5.0,
            "unit_label": "%",
            "theory_explanation": "Inside Bar范围上限(相对均幅)。内包线的高低点范围相对20周期均幅的比例。70%为标准，意味着内包线的振幅不超过均幅的70%。",
        },
    }

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        open_ = df["open"].values
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)
        # ---- 读取微调参数 ----
        pb_wick = self.get_param(config, "pin_bar_wick_pct") / 100.0
        pb_body = self.get_param(config, "pin_bar_body_max_pct") / 100.0
        eng_mult = self.get_param(config, "engulfing_body_mult")
        eng_vol = self.get_param(config, "engulfing_vol_min_mult")
        sr_touch = self.get_param(config, "sr_touch_pct") / 100.0
        sw_win = self.get_param(config, "swing_window")
        fk_break = self.get_param(config, "fakey_break_pct") / 100.0
        tl_touch = self.get_param(config, "trendline_touch_pct") / 100.0
        ib_range = self.get_param(config, "inside_bar_range_max_pct") / 100.0

        swings_h = self.swing_highs(close, sw_win)
        swings_l = self.swing_lows(close, sw_win)

        support_levels = [close[l] for l in swings_l[-10:]] if swings_l else []
        resistance_levels = [close[h] for h in swings_h[-10:]] if swings_h else []

        trend = "neutral"
        last_hh = 0
        last_hl = 0
        last_lh = 0
        last_ll = 0

        for i in range(60, len(close)):
            date_val = str(df.iloc[i].get("date", i))

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

            is_bullish_pin = (lower_pct > pb_wick and body_pct < pb_body and close[i] > open_[i])
            is_bearish_pin = (upper_pct > pb_wick and body_pct < pb_body and close[i] < open_[i])

            near_support = any(abs(close[i] - s) / s < sr_touch for s in support_levels) if support_levels else False
            if is_bullish_pin and (trend == "up" or near_support) and enabled("pin_bar"):
                self._sig(date_val, "BUY", close[i],
                          f"PA.Pin Bar锤子线: 长下影@{close[i]:.2f} 趋势:{trend}",
                          pattern="hammer")

            near_resistance = any(abs(close[i] - r) / r < sr_touch for r in resistance_levels) if resistance_levels else False
            if is_bearish_pin and (trend == "down" or near_resistance) and enabled("pin_bar"):
                self._sig(date_val, "SELL", close[i],
                          f"PA.Shooting Star: 长上影@{close[i]:.2f} 趋势:{trend}",
                          pattern="shooting_star")

            if i >= 1:
                prev_body = abs(close[i - 1] - open_[i - 1])
                prev_range = high[i - 1] - low[i - 1]

                bull_eng = (close[i] > open_[i] and close[i - 1] < open_[i - 1] and
                            open_[i] < close[i - 1] and close[i] > open_[i - 1] and
                            body > prev_body * eng_mult and vol[i] > avg_vol * eng_vol)
                if bull_eng and (trend == "up" or near_support) and enabled("engulfing"):
                    self._sig(date_val, "BUY", close[i],
                              f"PA.看涨吞没: 阳包阴@{close[i]:.2f} 放量",
                              pattern="bullish_engulfing")

                bear_eng = (close[i] < open_[i] and close[i - 1] > open_[i - 1] and
                            open_[i] > close[i - 1] and close[i] < open_[i - 1] and
                            body > prev_body * 1.5 and vol[i] > avg_vol)
                if bear_eng and (trend == "down" or near_resistance) and enabled("engulfing"):
                    self._sig(date_val, "SELL", close[i],
                              f"PA.看跌吞没: 阴包阳@{close[i]:.2f} 放量",
                              pattern="bearish_engulfing")

            if i >= 2:
                is_ib = (high[i - 1] < high[i - 2] and low[i - 1] > low[i - 2] and
                         (high[i - 1] - low[i - 1]) < avg_range * ib_range)
                if is_ib and close[i] > high[i - 2] and trend == "up" and enabled("inside_bar"):
                    self._sig(date_val, "BUY", close[i],
                              f"PA.Inside Bar突破: 上破母K线{high[i-2]:.2f}",
                              pattern="ib_breakout_up")
                if is_ib and close[i] < low[i - 2] and trend == "down" and enabled("inside_bar"):
                    self._sig(date_val, "SELL", close[i],
                              f"PA.Inside Bar跌破: 下破母K线{low[i-2]:.2f}",
                              pattern="ib_breakout_down")

            if i >= 4:
                if resistance_levels:
                    nearest_r = min(resistance_levels, key=lambda r: abs(close[i-2] - r))
                    false_up = (high[i-2] > nearest_r * (1 + fk_break) and close[i-2] < nearest_r and
                                close[i] < close[i-1])
                    if false_up and enabled("fakey"):
                        self._sig(date_val, "SELL", close[i],
                                  f"PA.Fakey假突破: 上破{nearest_r:.2f}后回落 多头陷阱",
                                  pattern="fakey_sell")

                if support_levels:
                    nearest_s = min(support_levels, key=lambda s: abs(close[i-2] - s))
                    false_down = (low[i-2] < nearest_s * (1 - fk_break) and close[i-2] > nearest_s and
                                  close[i] > close[i-1])
                    if false_down and enabled("fakey"):
                        self._sig(date_val, "BUY", close[i],
                                  f"PA.Fakey假跌破: 下破{nearest_s:.2f}后收复 空头陷阱",
                                  pattern="fakey_buy")

            if i >= 30:
                for r in resistance_levels:
                    if abs(close[i] - r) / r < sr_touch * 0.75:
                        if np.all(close[max(0, i - 20):i] > r * 0.98):
                            if close[i] > r and close[i - 1] < r and enabled("sr_flip"):
                                self._sig(date_val, "BUY", close[i],
                                          f"PA.阻力变支撑: {r:.2f} 突破回踩确认")

                for s in support_levels:
                    if abs(close[i] - s) / s < sr_touch * 0.75:
                        if np.all(close[max(0, i - 20):i] < s * 1.02):
                            if close[i] < s and close[i - 1] > s and enabled("sr_flip"):
                                self._sig(date_val, "SELL", close[i],
                                          f"PA.支撑变阻力: {s:.2f} 跌破反抽确认")

            if trend == "up" and len(swings_l) >= 3:
                tl_idx = [l for l in swings_l[-3:] if l < i]
                tl_lows = [close[l] for l in tl_idx]
                if len(tl_lows) >= 2:
                    # Slope over the actual bar (time) axis, then project to bar i.
                    slope = (tl_lows[-1] - tl_lows[0]) / max(tl_idx[-1] - tl_idx[0], 1)
                    tl_price = tl_lows[-1] + slope * (i - tl_idx[-1])
                    if abs(close[i] - tl_price) / tl_price < tl_touch and close[i] > tl_price and enabled("trendline"):
                        self._sig(date_val, "BUY", close[i],
                                  f"PA.上升趋势线支撑: {tl_price:.2f} 触线反弹")

            if trend == "down" and len(swings_h) >= 3:
                tl_idx = [h for h in swings_h[-3:] if h < i]
                tl_highs = [close[h] for h in tl_idx]
                if len(tl_highs) >= 2:
                    # Slope over the actual bar (time) axis, then project to bar i.
                    slope = (tl_highs[-1] - tl_highs[0]) / max(tl_idx[-1] - tl_idx[0], 1)
                    tl_price = tl_highs[-1] + slope * (i - tl_idx[-1])
                    if abs(close[i] - tl_price) / tl_price < tl_touch and close[i] < tl_price and enabled("trendline"):
                        self._sig(date_val, "SELL", close[i],
                                  f"PA.下降趋势线压制: {tl_price:.2f} 触线回落")

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()
