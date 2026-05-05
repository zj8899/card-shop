"""
波浪理论 (Elliott Wave Theory) — 六脉神剑
Creator: Ralph Nelson Elliott (refined by Frost & Prechter)

Core Techniques:
1. 5-Wave Impulse Pattern:
   - Wave 1: Initial move, often short, low conviction
   - Wave 2: Sharp retracement (50%-61.8% of Wave 1), never 100%+
   - Wave 3: Strongest & longest, 1.618x-2.618x Wave 1, high volume
   - Wave 4: Sideways/flat correction (23.6%-38.2% of Wave 3)
   - Wave 5: Final push, often 0.618x-1.0x Wave 1+3, divergence

2. 3-Wave Correction (ABC):
   - Zigzag (5-3-5): Sharp, A=C often
   - Flat (3-3-5): Sideways, B retraces 90%+ of A
   - Triangle (A-B-C-D-E): Contracting, 5 subwaves
   - Combination: Multiple corrections linked

3. Wave Personality:
   - W1: "Skepticism" — no one believes
   - W2: "Panic" — sharp sell-off, fear
   - W3: "Recognition" — public joins, strongest
   - W4: "Complacency" — distribution, range
   - W5: "Euphoria" — retail FOMO, divergence

4. Fibonacci Relationships:
   - W2 = 0.5-0.618 of W1
   - W3 = 1.618-2.618 of W1
   - W4 = 0.236-0.382 of W3
   - W5 = 0.618-(W1+W3) or W1 or 0.618*W1

5. Alternation Principle:
   - If W2 is sharp → W4 is flat (and vice versa)
   - Simple vs Complex corrections alternate

6. Channeling: Parallel trendlines through W1-W3 for W5 projection
"""
import numpy as np
import pandas as pd
from .base import BaseSchool


class WaveTheorySchool(BaseSchool):
    name = "wave_theory"
    description = "波浪理论: 5浪推动+3浪调整+斐波那契+交替原则"
    core_technique = "5浪识别 + 斐波关系验证 + Channeling投影"

    def generate_signals(self, df: pd.DataFrame, config: dict = None) -> pd.DataFrame:
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        open_ = df["open"].values
        vol = df["volume"].values if "volume" in df.columns else np.ones(len(close))
        self.signals = []
        enabled = lambda key: config is None or config.get(key, True)

        swings_h = self.swing_highs(close, 5)
        swings_l = self.swing_lows(close, 5)
        dif, dea, macd_bar = self.macd(close)

        # Build all swing points chronologically
        all_swings = sorted(
            [(h, "H", close[h]) for h in swings_h] +
            [(l, "L", close[l]) for l in swings_l],
            key=lambda x: x[0]
        )

        for i in range(100, len(close)):
            date_val = str(df.iloc[i].get("date", i))

            # Get swings before current index
            swings_before = [s for s in all_swings if s[0] <= i - 5]

            # Need at least 6 swing points for 5-wave + 1
            if len(swings_before) < 6:
                continue

            last_swings = swings_before[-6:]

            # ---- Impulse Up Detection (L-H-L-H-L-H) ----
            if self._is_sequence(last_swings, ["L", "H", "L", "H", "L", "H"]):
                w0 = last_swings[0]  # Start low
                w1 = last_swings[1]  # Wave 1 high
                w2 = last_swings[2]  # Wave 2 low
                w3 = last_swings[3]  # Wave 3 high
                w4 = last_swings[4]  # Wave 4 low
                w5 = last_swings[5]  # Wave 5 high (projected)

                wave1 = w1[2] - w0[2]
                wave2_retrace = (w1[2] - w2[2]) / wave1 if wave1 > 0 else 1
                wave3 = w3[2] - w2[2]
                wave4_retrace = (w3[2] - w4[2]) / wave3 if wave3 > 0 else 0

                # Wave rules validation
                w2_valid = 0.35 < wave2_retrace < 0.90  # W2: 35%-90% of W1
                w3_valid = wave3 > wave1 * 1.2  # W3 > W1
                w4_valid = 0.15 < wave4_retrace < 0.50  # W4: 15%-50% of W3
                w4_above_w1 = w4[2] > w1[2]  # W4 does not overlap W1 (核心规则)
                w3_longest = wave3 > wave1 and wave3 > (w5[2] - w4[2])  # W3 usually longest

                # Alternation: W2 sharp → W4 flat (or vice versa)
                w2_duration = w2[0] - w1[0]
                w4_duration = i - w3[0]
                alternation = abs(w2_duration - w4_duration) > 5  # Different durations

                if w2_valid and w3_valid and w4_valid and w4_above_w1:
                    # Wave 5 projection
                    w5_proj_618 = w4[2] + (w0[2] - w3[2]) * 0.618
                    w5_proj_100 = w4[2] + wave1

                    # Current price is Wave 5: buy if in early W5
                    if w5[0] == last_swings[-1][0] and close[i] > w4[2] * 1.01 and enabled("impulse_w5"):
                        self._sig(date_val, "BUY", close[i],
                                  f"波浪·W5推动中: W1={wave1:.2f} W3={wave3:.2f} 目标618={w5_proj_618:.2f}",
                                  wave="5", w5_target=round(w5_proj_618, 2))

            # ---- Impulse Down Detection (H-L-H-L-H-L) ----
            if self._is_sequence(last_swings, ["H", "L", "H", "L", "H", "L"]):
                w0 = last_swings[0]
                w1 = last_swings[1]
                w2 = last_swings[2]
                w3 = last_swings[3]
                w4 = last_swings[4]
                w5 = last_swings[5]

                wave1 = w0[2] - w1[2]
                wave2_retrace = (w2[2] - w1[2]) / wave1 if wave1 > 0 else 1
                wave3 = w2[2] - w3[2]

                w2_valid = 0.35 < wave2_retrace < 0.90
                w3_valid = wave3 > wave1 * 1.2

                if w2_valid and w3_valid and enabled("impulse_w5"):
                    self._sig(date_val, "SELL", close[i],
                              f"波浪·下跌W5: W1={wave1:.2f} W3={wave3:.2f} 下跌推动中",
                              wave="5_down")

            # ---- ABC Correction After Impulse ----
            # After 5-wave up → ABC down (buy at C completion)
            if len(last_swings) >= 3:
                abc = last_swings[-3:]
                abc_types = [s[1] for s in abc]
                if abc_types == ["H", "L", "H"]:  # A down, B up, C about to go down
                    a_wave = abc[0][2] - abc[1][2]
                    b_wave = abc[2][2] - abc[1][2]
                    b_retrace = b_wave / a_wave if a_wave > 0 else 0

                    # B typically retraces 50-78.6% of A
                    if 0.4 < b_retrace < 0.85:
                        # C wave target: A length from B top
                        c_target = abc[2][2] - a_wave
                        c_1618_target = abc[2][2] - a_wave * 1.618

                        # If price is approaching C target → buy
                        if close[i] < abc[2][2] * 0.98 and close[i] > c_target and enabled("abc_correction"):
                            self._sig(date_val, "BUY", close[i],
                                      f"波浪·ABC调整C浪买入: A={a_wave:.2f} B回撤={b_retrace*100:.0f}% C目标={c_target:.2f}",
                                      pattern="abc_buy", c_target=round(c_target, 2))

                if abc_types == ["L", "H", "L"]:  # A up, B down, C about to go up
                    a_wave = abc[1][2] - abc[0][2]
                    b_wave = abc[1][2] - abc[2][2]
                    b_retrace = b_wave / a_wave if a_wave > 0 else 0
                    if 0.4 < b_retrace < 0.85:
                        c_target = abc[2][2] + a_wave
                        if close[i] > abc[2][2] * 1.02 and close[i] < c_target and enabled("abc_correction"):
                            self._sig(date_val, "SELL", close[i],
                                      f"波浪·ABC反弹C浪卖出: A={a_wave:.2f} C目标={c_target:.2f}",
                                      pattern="abc_sell")

            # ---- Fibonacci Wave Relationship Confirmation ----
            if i >= 30:
                # Find the most recent impulse and compute Fibonacci projections
                recent_high = np.max(high[max(0, i - 60):i])
                recent_low = np.min(low[max(0, i - 60):i])
                wave_range = recent_high - recent_low

                if wave_range > 0:
                    # Standard fib levels for wave projections
                    fib_382 = recent_low + wave_range * 0.382
                    fib_500 = recent_low + wave_range * 0.500
                    fib_618 = recent_low + wave_range * 0.618
                    fib_786 = recent_low + wave_range * 0.786
                    fib_1618 = recent_low + wave_range * 1.618

                    # Bounce from fib level
                    fib_bounce_618 = (abs(low[i] - fib_618) / fib_618 < 0.015 and
                                      close[i] > open_[i] and close[i] > close[i - 1])
                    fib_bounce_500 = (abs(low[i] - fib_500) / fib_500 < 0.015 and
                                      close[i] > open_[i] and close[i] > close[i - 1])
                    fib_bounce_382 = (abs(low[i] - fib_382) / fib_382 < 0.015 and
                                      close[i] > open_[i] and close[i] > close[i - 1])

                    if fib_bounce_618 and enabled("fib_retrace"):
                        self._sig(date_val, "BUY", close[i],
                                  f"波浪·斐波那契0.618回撤支撑: {fib_618:.2f}")
                    elif fib_bounce_500 and enabled("fib_retrace"):
                        self._sig(date_val, "BUY", close[i],
                                  f"波浪·斐波那契0.50回撤支撑: {fib_500:.2f}")
                    elif fib_bounce_382 and enabled("fib_retrace"):
                        self._sig(date_val, "BUY", close[i],
                                  f"波浪·斐波那契0.382回撤支撑: {fib_382:.2f}")

                    # Rejection from fib extension
                    if abs(high[i] - fib_1618) / fib_1618 < 0.015 and close[i] < open_[i] and enabled("fib_extension"):
                        self._sig(date_val, "SELL", close[i],
                                  f"波浪·斐波那契1.618延伸阻力: {fib_1618:.2f}")

        return pd.DataFrame(self.signals) if self.signals else pd.DataFrame()

    def _is_sequence(self, swings, pattern):
        """Check if swings follow a specific type pattern."""
        if len(swings) < len(pattern):
            return False
        types = [s[1] for s in swings[-len(pattern):]]
        return types == pattern
