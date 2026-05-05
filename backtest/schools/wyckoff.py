"""
威科夫 (Wyckoff Method) — 太极
Creator: Richard D. Wyckoff

Core Techniques:
1. Composite Operator — "The Composite Man" concept
2. Three Laws: Supply & Demand, Cause & Effect, Effort vs Result
3. Price Cycle: Accumulation → Markup → Distribution → Markdown
4. Accumulation Schematic (Phases A-E):
   A: Preliminary Support (PS), Selling Climax (SC)
   B: Secondary Test (ST), Cause building
   C: Spring (shakeout)
   D: Sign of Strength (SOS), Last Point of Support (LPS)
   E: Markup begins
5. Distribution Schematic (Phases A-E):
   A: Preliminary Supply (PSY), Buying Climax (BC)
   B: Secondary Test (SOW)
   C: Upthrust After Distribution (UTAD)
   D: Sign of Weakness (SOW), Last Point of Supply (LPSY)
   E: Markdown begins
6. Volume Spread Analysis (VSA):
   - High volume + narrow spread = absorption
   - Low volume + wide spread = no effort
"""
import numpy as np
import pandas as pd
from .base import BaseSchool


class WyckoffSchool(BaseSchool):
    name = "wyckoff"
    description = "威科夫: 吸筹/派发·Spring/UTAD·VSA量价分析"
    core_technique = "因果法则 + 努力vs结果 + Spring/UTAD"

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        avg_vol_20 = self.sma(vol, 20)
        avg_vol_50 = self.sma(vol, 50)
        atr = self.atr(high, low, close, 14)

        # Phase detection state
        phase = "unknown"

        for i in range(120, len(close)):
            date_val = str(df.iloc[i].get("date", i))
            spread = high[i] - low[i]
            avg_spread = np.mean(high[max(0, i - 20):i] - low[max(0, i - 20):i]) if i >= 20 else spread

            # ---- Effort vs Result (VSA) ----
            # High effort (volume) + small result (price change) = absorption/rejection
            effort_high = vol[i] > avg_vol_20[i] * 1.5
            price_move = abs(close[i] - close[i - 1]) / close[i - 1]
            seg = close[max(0, i - 20):i + 1]
            avg_move = np.mean(np.abs(np.diff(seg) / seg[:-1])) if i >= 20 and len(seg) >= 2 else price_move
            result_small = price_move < avg_move * 0.5

            # Absorption: high volume, narrow spread, closing near high
            absorption = (effort_high and spread < avg_spread * 0.7 and
                          close[i] > open_[i] and (close[i] - low[i]) > spread * 0.6)
            # Rejection: high volume, narrow spread, closing near low
            rejection = (effort_high and spread < avg_spread * 0.7 and
                         close[i] < open_[i] and (high[i] - close[i]) > spread * 0.6)

            # ---- Accumulation Detection (Phases A-E) ----
            # Look for selling climax (SC) followed by spring
            selling_climax = (
                i >= 30 and
                vol[i - 5] > avg_vol_50[i - 5] * 2.5 and  # Extreme volume
                close[i - 5] < close[max(0, i - 30):i].min() * 1.02 and  # Near recent low
                close[i] > close[i - 5] * 1.03  # Recovery from SC
            )

            # Spring: false break below support with quick recovery
            recent_low_20 = np.min(low[max(0, i - 20):i])
            spring = (
                low[i] < recent_low_20 * 0.98 and  # Break below support
                close[i] > recent_low_20 and        # Recover above support
                vol[i] > avg_vol_20[i] * 1.3 and    # High volume confirmation
                close[i] > open_[i]                  # Bullish close
            )

            # Sign of Strength (SOS): wide spread up + high volume
            sos = (
                close[i] > close[i - 1] * 1.015 and
                spread > avg_spread * 1.3 and
                vol[i] > avg_vol_20[i] * 1.2 and
                close[i] > high[max(0, i - 5):i].max()
            )

            # Last Point of Support (LPS): pullback to support + low volume
            if i >= 10:
                recent_high = np.max(high[max(0, i - 20):i])
                lps = (
                    close[i] < recent_high * 0.97 and
                    vol[i] < avg_vol_20[i] * 0.8 and
                    close[i] > close[i - 1] and
                    np.min(low[max(0, i - 5):i]) > recent_low_20
                )
            else:
                lps = False

            # ---- Distribution Detection (Phases A-E) ----
            buying_climax = (
                i >= 30 and
                vol[i - 5] > avg_vol_50[i - 5] * 2.5 and
                close[i - 5] > close[max(0, i - 30):i].max() * 0.98 and
                close[i] < close[i - 5] * 0.97
            )

            # Upthrust After Distribution (UTAD): false break above resistance
            recent_high_20 = np.max(high[max(0, i - 20):i])
            utad = (
                high[i] > recent_high_20 * 1.02 and
                close[i] < recent_high_20 and
                vol[i] > avg_vol_20[i] * 1.3 and
                close[i] < open_[i]
            )

            # Sign of Weakness (SOW): wide spread down + high volume
            sow = (
                close[i] < close[i - 1] * 0.985 and
                spread > avg_spread * 1.3 and
                vol[i] > avg_vol_20[i] * 1.2 and
                close[i] < low[max(0, i - 5):i].min()
            )

            # ---- Phase Detection & Signal Logic ----
            # Accumulation phase confirmed → BUY signals
            in_accumulation = selling_climax
            if spring and in_accumulation and enabled("spring"):
                self._sig(date_val, "BUY", close[i],
                          f"威科夫·Spring: 假跌破{recent_low_20:.2f}放量收复@{close[i]:.2f}",
                          wyckoff_phase="Accumulation-C", pattern="spring")
                phase = "accumulation"

            elif sos and enabled("sos"):
                self._sig(date_val, "BUY", close[i],
                          f"威科夫·SOS强势信号: 突破{close[i]:.2f}量价齐升",
                          wyckoff_phase="Accumulation-D", pattern="sos")
                phase = "markup"

            elif lps and phase in ("accumulation", "markup") and enabled("lps"):
                self._sig(date_val, "BUY", close[i],
                          f"威科夫·LPS最后支撑: 缩量回踩{close[i]:.2f}",
                          wyckoff_phase="Markup", pattern="lps")

            elif absorption and phase in ("accumulation", "markup") and enabled("vsa_accumulation"):
                self._sig(date_val, "BUY", close[i],
                          f"威科夫·VSA吸筹: 高量窄幅收高@{close[i]:.2f}",
                          wyckoff_phase=phase, pattern="absorption")

            # Distribution phase → SELL signals
            in_distribution = buying_climax
            if utad and in_distribution and enabled("utad"):
                self._sig(date_val, "SELL", close[i],
                          f"威科夫·UTAD: 假突破{recent_high_20:.2f}放量回落@{close[i]:.2f}",
                          wyckoff_phase="Distribution-C", pattern="utad")
                phase = "distribution"

            elif sow and enabled("sow"):
                self._sig(date_val, "SELL", close[i],
                          f"威科夫·SOW弱势信号: 跌破{close[i]:.2f}量价齐跌",
                          wyckoff_phase="Distribution-D", pattern="sow")
                phase = "markdown"

            elif rejection and phase in ("distribution", "markdown") and enabled("vsa_distribution"):
                self._sig(date_val, "SELL", close[i],
                          f"威科夫·VSA派发: 高量窄幅收低@{close[i]:.2f}",
                          wyckoff_phase=phase, pattern="rejection")

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()
