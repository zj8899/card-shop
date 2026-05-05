"""
道氏理论 (Dow Theory) — 少林易筋经
Creator: Charles H. Dow

Core Techniques:
1. The Three Trends:
   - Primary Trend (主要趋势): 1 year+, 20%+ move, "the Tide"
   - Secondary Trend (次级趋势): 3 weeks-3 months, 33%-66% retracement, "the Waves"
   - Minor Trend (日常波动): days-weeks, noise, "the Ripples"

2. Three Phases of Primary Trend:
   - Accumulation Phase: Smart money quietly buys/sells
   - Public Participation Phase: Trend followers join, biggest move
   - Distribution Phase: Smart money exits, public still buying

3. Six Principles:
   (1) Averages Discount Everything
   (2) Three Trends
   (3) Three Phases
   (4) Averages Must Confirm Each Other (indices confirm)
   (5) Volume Must Confirm the Trend
   (6) Trend Persists Until Definitive Reversal

4. Trend Confirmation:
   - Uptrend: Higher Highs + Higher Lows (HH+HL)
   - Downtrend: Lower Highs + Lower Lows (LH+LL)
   - Break of Structure (BOS) ends the trend

5. Volume Confirmation:
   - Uptrend: volume expands on up days, contracts on down days
   - Downtrend: volume expands on down days, contracts on up days

6. Line (Range): Narrow range for weeks = accumulation or distribution
   - Breakout direction determines next primary move
"""
import numpy as np
import pandas as pd
from .base import BaseSchool


class DowTheorySchool(BaseSchool):
    name = "dow_theory"
    description = "道氏理论: 三趋势·三阶段·指数确认·量价验证"
    core_technique = "趋势分级(HH/HL·LH/LL) + 三阶段 + 成交量验证"

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

        # State tracking
        primary_trend = "neutral"  # "up", "down", "neutral"
        phase = "unknown"  # "accumulation", "participation", "distribution"
        last_signal_idx = 0

        avg_vol_50 = self.sma(vol, 50)
        avg_vol_200 = self.sma(vol, 200)

        for i in range(150, len(close)):
            date_val = str(df.iloc[i].get("date", i))

            recent_h = [(h, close[h]) for h in swings_h if i - 120 < h <= i]
            recent_l = [(l, close[l]) for l in swings_l if i - 120 < l <= i]

            if len(recent_h) < 2 or len(recent_l) < 2:
                continue

            # ---- Primary Trend Detection ----
            h1_idx, h1_price = recent_h[-2]
            h2_idx, h2_price = recent_h[-1]
            l1_idx, l1_price = recent_l[-2]
            l2_idx, l2_price = recent_l[-1]

            hh_hl = h2_price > h1_price and l2_price > l1_price  # Uptrend
            lh_ll = h2_price < h1_price and l2_price < l1_price  # Downtrend

            # Detect trend change
            if hh_hl and primary_trend != "up":
                primary_trend = "up"
                phase = "participation"
                self._sig(date_val, "BUY", close[i],
                          f"道氏·主升趋势确认: HH+HL [{l1_price:.2f}>{l2_price:.2f}] [{h1_price:.2f}>{h2_price:.2f}]",
                          trend_change="bullish")

            elif lh_ll and primary_trend != "down":
                primary_trend = "down"
                phase = "distribution"
                self._sig(date_val, "SELL", close[i],
                          f"道氏·主跌趋势确认: LH+LL [{h1_price:.2f}>{h2_price:.2f}] [{l1_price:.2f}>{l2_price:.2f}]",
                          trend_change="bearish")

            # ---- Three Phases Detection ----
            # Phase 1: Accumulation (narrow range + low volume → breakout)
            if i >= 60:
                range_60 = (np.max(high[i - 60:i]) - np.min(low[i - 60:i])) / np.mean(close[i - 60:i])
                narrow_range = range_60 < 0.08  # <8% range over 60 bars
                vol_declining = avg_vol_50[i] < avg_vol_200[i] * 0.85 if avg_vol_200[i] > 0 else False

                # Accumulation breakout (Line → Up)
                if narrow_range and vol_declining and close[i] > np.max(high[i - 60:i]):
                    phase = "participation"
                    self._sig(date_val, "BUY", close[i],
                              f"道氏·吸筹突破(Line→Up): 60日窄幅{range_60*100:.1f}%放量突破",
                              phase="accumulation_breakout")

                # Distribution breakout (Line → Down)
                if narrow_range and vol_declining and close[i] < np.min(low[i - 60:i]):
                    phase = "distribution"
                    self._sig(date_val, "SELL", close[i],
                              f"道氏·派发跌破(Line→Down): 60日窄幅{range_60*100:.1f}%放量跌破",
                              phase="distribution_breakdown")

            # ---- Volume Confirmation ----
            if i >= 30:
                recent_up_vol = []
                recent_down_vol = []
                for j in range(max(0, i - 30), i):
                    if close[j] > close[j - 1]:
                        recent_up_vol.append(vol[j])
                    else:
                        recent_down_vol.append(vol[j])

                avg_up_vol = np.mean(recent_up_vol) if recent_up_vol else 0
                avg_down_vol = np.mean(recent_down_vol) if recent_down_vol else 0

                # Volume confirms uptrend: up days have higher volume
                vol_confirms_up = avg_up_vol > avg_down_vol * 1.15

                # Volume confirms downtrend: down days have higher volume
                vol_confirms_down = avg_down_vol > avg_up_vol * 1.15

                # Trend + Volume alignment = strong signal
                if primary_trend == "up" and vol_confirms_up:
                    # Secondary pullback entry in primary uptrend
                    ma_21 = np.mean(close[max(0, i - 21):i])
                    pullback = close[i] < ma_21 and low[i] > l2_price
                    vol_dry = vol[i] < avg_vol_50[i] * 0.7
                    if pullback and vol_dry and (i - last_signal_idx > 20) and enabled("secondary_pullback"):
                        self._sig(date_val, "BUY", close[i],
                                  f"道氏·次级回调买入: 主升趋势回踩MA21{ma_21:.2f}缩量",
                                  phase="secondary_buy")
                        last_signal_idx = i

                if primary_trend == "down" and vol_confirms_down:
                    ma_21 = np.mean(close[max(0, i - 21):i])
                    rally = close[i] > ma_21 and high[i] < h2_price
                    vol_dry = vol[i] < avg_vol_50[i] * 0.7
                    if rally and vol_dry and (i - last_signal_idx > 20) and enabled("secondary_pullback"):
                        self._sig(date_val, "SELL", close[i],
                                  f"道氏·次级反弹卖出: 主跌趋势反弹MA21{ma_21:.2f}缩量",
                                  phase="secondary_sell")
                        last_signal_idx = i

            # ---- Definitive Reversal Signal ----
            # Primary uptrend → downtrend: price breaks below last HL + volume expansion
            if primary_trend == "up" and len(recent_l) >= 3:
                last_hl = recent_l[-1]
                break_structure = close[i] < last_hl[1] * 0.98 and vol[i] > avg_vol_50[i] * 1.3
                if break_structure and enabled("trend_reversal"):
                    self._sig(date_val, "SELL", close[i],
                              f"道氏·趋势反转: 跌破最后低点{last_hl[1]:.2f}放量确认",
                              reversal="primary_up_to_down")
                    primary_trend = "down"

            if primary_trend == "down" and len(recent_h) >= 3:
                last_lh = recent_h[-1]
                break_structure = close[i] > last_lh[1] * 1.02 and vol[i] > avg_vol_50[i] * 1.3
                if break_structure and enabled("trend_reversal"):
                    self._sig(date_val, "BUY", close[i],
                              f"道氏·趋势反转: 突破最后高点{last_lh[1]:.2f}放量确认",
                              reversal="primary_down_to_up")
                    primary_trend = "up"

            # ---- Participation Phase Entry (追涨/追跌) ----
            if phase == "participation" and primary_trend == "up":
                # Breakout from consolidation within participation
                if i >= 20:
                    range_20 = (np.max(high[i - 20:i]) - np.min(low[i - 20:i])) / close[i]
                    if range_20 < 0.04 and close[i] > np.max(high[i - 20:i]) and enabled("participation_breakout"):
                        self._sig(date_val, "BUY", close[i],
                                  f"道氏·公众参与阶段突破: 横盘后放量创新高",
                                  phase="participation_breakout")

            if phase == "distribution" and primary_trend == "down":
                if i >= 20:
                    range_20 = (np.max(high[i - 20:i]) - np.min(low[i - 20:i])) / close[i]
                    if range_20 < 0.04 and close[i] < np.min(low[i - 20:i]) and enabled("participation_breakout"):
                        self._sig(date_val, "SELL", close[i],
                                  f"道氏·派发阶段下破: 横盘后放量创新低",
                                  phase="distribution_breakdown")

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()
