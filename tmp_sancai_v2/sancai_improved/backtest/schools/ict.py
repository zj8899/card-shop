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

    SIGNAL_TOGGLES = [
        {"key": "ob", "label": "Order Block (OB)", "default": True},
        {"key": "fvg", "label": "Fair Value Gap (FVG)", "default": True},
        {"key": "liquidity_ote", "label": "流动性猎杀 + OTE入场", "default": True},
        {"key": "breaker_block", "label": "Breaker Block", "default": True},
    ]

    PARAMS = {
        "swing_window": {
            "type": "int", "default": 5, "min": 3, "max": 10, "step": 1,
            "unit_label": "根K线",
            "theory_explanation": "摆动高低点识别窗口。ICT用摆动点判断市场结构(HH/HL/LH/LL)，窗口越小对结构变化越敏感。3-5适合短线，6-10适合波段。",
        },
        "impulse_threshold": {
            "type": "float", "default": 1.5, "min": 0.5, "max": 4.0, "step": 0.5,
            "unit_label": "%",
            "theory_explanation": "冲动移动(Impulse)的最小涨幅阈值。ICT的OB出现在冲动移动前，阈值决定什么级别的移动才触发OB识别。低阈值产生更多OB信号。",
        },
        "ob_lookback": {
            "type": "int", "default": 10, "min": 5, "max": 20, "step": 1,
            "unit_label": "根K线",
            "theory_explanation": "Order Block回溯窗口。OB定义为冲动前最后一根反向K线，窗口决定向前搜索OB的最大范围。过小可能遗漏远端OB，过大引入已失效OB。",
        },
        "fvg_gap_min": {
            "type": "float", "default": 0.01, "min": 0.005, "max": 0.03, "step": 0.005,
            "unit_label": "元",
            "theory_explanation": "FVG缺口最小价差。FVG代表价格失衡区(Imbalance)，缺口越大意味着流动性空洞越显著。低阈值捕捉更多FVG但噪音增加。",
        },
        "liquidity_sweep_window": {
            "type": "int", "default": 5, "min": 3, "max": 10, "step": 1,
            "unit_label": "根K线",
            "theory_explanation": "流动性猎杀识别窗口。ICT认为价格会先扫掉明显的高低点再反转，窗口决定「明显」的范围。做市商通常扫3-5根K线的高低点。",
        },
        "ote_lower": {
            "type": "float", "default": 0.618, "min": 0.5, "max": 0.7, "step": 0.01,
            "unit_label": "斐波那契",
            "theory_explanation": "OTE区域下边界(斐波那契回撤)。ICT最优交易入场在61.8%-79%回撤区间，下边界决定OTE区域的下沿。0.618为黄金分割标准位。",
        },
        "ote_upper": {
            "type": "float", "default": 0.79, "min": 0.7, "max": 0.886, "step": 0.01,
            "unit_label": "斐波那契",
            "theory_explanation": "OTE区域上边界(斐波那契回撤)。0.79为ICT标准OTE上沿，0.886为深度回撤极限。上移扩大入场窗口但离结构点更远。",
        },
        "breaker_tolerance": {
            "type": "float", "default": 0.2, "min": 0.1, "max": 1.0, "step": 0.1,
            "unit_label": "%",
            "theory_explanation": "Breaker Block价格容差。CHoCH后原OB转化为Breaker，容差决定触及Breaker区域的灵敏度。过小几乎不触发，过大失去位置优势。",
        },
        "choch_sensitivity": {
            "type": "float", "default": 1.0, "min": 1.0, "max": 5.0, "step": 1.0,
            "unit_label": "级",
            "theory_explanation": "CHoCH灵敏度级别。值越小对市场结构转变(Change of Character)越敏感。1为最敏感(标准)，5为最迟钝(需更明确的突破确认)。",
        },
    }

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        # ---- 读取微调参数 ----
        sw_win = self.get_param(config, "swing_window")
        imp_th = self.get_param(config, "impulse_threshold") / 100.0
        ob_lb = self.get_param(config, "ob_lookback")
        fvg_gap = self.get_param(config, "fvg_gap_min")
        liq_sw = self.get_param(config, "liquidity_sweep_window")
        ote_low = self.get_param(config, "ote_lower")
        ote_up = self.get_param(config, "ote_upper")
        brk_tol = self.get_param(config, "breaker_tolerance") / 100.0
        choch_sens = self.get_param(config, "choch_sensitivity")

        # Identify Market Structure
        swings_h = self.swing_highs(close, sw_win)
        swings_l = self.swing_lows(close, sw_win)

        # Track market structure state
        hh_hl = False  # Bullish: Higher High + Higher Low
        lh_ll = False  # Bearish: Lower High + Lower Low
        choch_idx = 0  # Last Change of Character index

        # Pending FVG zones awaiting a price retrace (fill).
        # Per ICT theory a FVG is traded when price RETRACES into the gap,
        # not at the moment the gap forms. Each entry: (formed_idx, bot, top).
        pending_fvg_bull = []
        pending_fvg_bear = []

        for i in range(80, len(close)):
            date_val = str(df.iloc[i].get("date", i))

            # ---- Market Structure Shift (MSS / CHoCH) ----
            mss_win = int(40 * choch_sens)
            recent_h = [h for h in swings_h if i - mss_win < h < i]
            recent_l = [l for l in swings_l if i - mss_win < l < i]

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
                strong_up = close[i] > close[i - 3] * (1 + imp_th)
                if strong_up:
                    # Find the last bearish candle
                    for j in range(i - 1, max(0, i - ob_lb), -1):
                        if close[j] < open_[j]:
                            ob_high = high[j]
                            ob_low = low[j]
                            # Price retraces to OB zone
                            if ob_low <= close[i] <= ob_high * 1.005 and enabled("ob"):
                                self._sig(date_val, "BUY", close[i],
                                          f"ICT·看涨OB回踩: OB[{ob_low:.2f}-{ob_high:.2f}]",
                                          ob_zone=f"{ob_low:.2f}-{ob_high:.2f}")
                            break

                strong_down = close[i] < close[i - 3] * (1 - imp_th)
                if strong_down:
                    for j in range(i - 1, max(0, i - ob_lb), -1):
                        if close[j] > open_[j]:
                            ob_high = high[j]
                            ob_low = low[j]
                            if ob_low <= close[i] <= ob_high * 1.005 and enabled("ob"):
                                self._sig(date_val, "SELL", close[i],
                                          f"ICT·看跌OB反弹: OB[{ob_low:.2f}-{ob_high:.2f}]",
                                          ob_zone=f"{ob_low:.2f}-{ob_high:.2f}")
                            break

            # ---- Fair Value Gap (FVG) ----
            # A FVG is an imbalance zone. Per ICT theory it should be traded when
            # price RETRACES into the gap (revisits/fills it), NOT at the moment the
            # gap forms. So we record the zone on formation and only emit a signal
            # once a LATER bar's price re-enters the zone.
            if i >= 3:
                fvg_bull = low[i] > high[i - 2] + fvg_gap
                fvg_bear = high[i] < low[i - 2] - fvg_gap

                # Record newly-formed gap zones (bot < top).
                if fvg_bull and close[i] > close[i - 1]:
                    # Bullish gap zone spans [high[i-2], low[i]]
                    pending_fvg_bull.append((i, high[i - 2], low[i]))
                if fvg_bear and close[i] < close[i - 1]:
                    # Bearish gap zone spans [high[i], low[i-2]]
                    pending_fvg_bear.append((i, high[i], low[i - 2]))

                # Only keep zones formed within the last N bars (expire stale gaps).
                fvg_life = 20
                pending_fvg_bull = [z for z in pending_fvg_bull if i - z[0] <= fvg_life]
                pending_fvg_bear = [z for z in pending_fvg_bear if i - z[0] <= fvg_life]

                if enabled("fvg"):
                    # Bullish FVG: BUY when a later bar's low retraces DOWN into the zone.
                    for z in list(pending_fvg_bull):
                        formed_idx, fvg_bot, fvg_top = z
                        if i > formed_idx and fvg_bot <= low[i] <= fvg_top:
                            self._sig(date_val, "BUY", close[i],
                                      f"ICT·看涨FVG回补: [{fvg_bot:.2f}-{fvg_top:.2f}]价格回踩缺口买入",
                                      fvg=f"{fvg_bot:.2f}-{fvg_top:.2f}")
                            pending_fvg_bull.remove(z)

                    # Bearish FVG: SELL when a later bar's high retraces UP into the zone.
                    for z in list(pending_fvg_bear):
                        formed_idx, fvg_bot, fvg_top = z
                        if i > formed_idx and fvg_bot <= high[i] <= fvg_top:
                            self._sig(date_val, "SELL", close[i],
                                      f"ICT·看跌FVG回补: [{fvg_bot:.2f}-{fvg_top:.2f}]价格回踩缺口卖出",
                                      fvg=f"{fvg_bot:.2f}-{fvg_top:.2f}")
                            pending_fvg_bear.remove(z)

            # ---- Liquidity Sweep + OTE Entry ----
            # Buy-side liquidity sweep: price takes out high then reverses
            if i >= liq_sw:
                recent_high = np.max(high[i - int(liq_sw):i])
                swept_high = high[i - 1] > recent_high and close[i] < close[i - 1]
                if swept_high and hh_hl is False and enabled("liquidity_ote"):
                    self._sig(date_val, "SELL", close[i],
                              f"ICT·买方流动性猎杀: 扫高点{recent_high:.2f}后反转")

                recent_low = np.min(low[i - int(liq_sw):i])
                swept_low = low[i - 1] < recent_low and close[i] > close[i - 1]
                if swept_low and lh_ll is False and enabled("liquidity_ote"):
                    # Check OTE zone
                    swing_range = np.max(high[max(0, i - 20):i]) - np.min(low[max(0, i - 20):i])
                    fib_low = np.max(high[max(0, i - 20):i]) - swing_range * ote_up
                    fib_high = np.max(high[max(0, i - 20):i]) - swing_range * ote_low
                    if fib_low <= close[i] <= fib_high:
                        self._sig(date_val, "BUY", close[i],
                                  f"ICT·卖方流动性猎杀+OTE: 扫低点{recent_low:.2f}入OTE",
                                  ote_zone=f"{fib_low:.2f}-{fib_high:.2f}")

            # ---- Breaker Block ----
            if i >= 10 and choch_idx > 0:
                # After CHoCH, old OB becomes breaker (support becomes resistance)
                bb_zone_low = close[choch_idx] * (1 - brk_tol)
                bb_zone_high = close[choch_idx] * (1 + brk_tol)
                if bb_zone_low <= close[i] <= bb_zone_high and enabled("breaker_block"):
                    if hh_hl:
                        self._sig(date_val, "BUY", close[i],
                                  f"ICT·Breaker Block支撑: CHoCH点{close[choch_idx]:.2f}")
                    elif lh_ll:
                        self._sig(date_val, "SELL", close[i],
                                  f"ICT·Breaker Block阻力: CHoCH点{close[choch_idx]:.2f}")

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()
