"""
Elliott Wave Theory + Fibonacci Full Module
5-wave impulse + 3-wave correction + Fibonacci retracement/extension
Supports real-time signal generation and post-market backtest.

Wave labeling rules:
- Wave 2 retraces 50%-78.6% of Wave 1
- Wave 3 is usually 1.618x Wave 1 (strongest)
- Wave 4 retraces 23.6%-38.2% of Wave 3
- Wave 5 often equals Wave 1 or 0.618x Wave 1-3
- ABC correction: A down, B up (usually 50%-61.8% of A), C down
"""
import json
import numpy as np
import pandas as pd


def find_swings(prices: np.ndarray, window: int = 5) -> list[tuple[int, float, str]]:
    """
    Find swing highs and lows.
    Returns list of (index, price, type) sorted by index.
    """
    swings = []
    for i in range(window, len(prices) - window):
        is_high = all(prices[i] >= prices[j] for j in range(i - window, i + window + 1) if j != i)
        is_low = all(prices[i] <= prices[j] for j in range(i - window, i + window + 1) if j != i)
        if is_high:
            swings.append((i, prices[i], "H"))
        elif is_low:
            swings.append((i, prices[i], "L"))
    return swings


def label_waves(swings: list[tuple[int, float, str]]) -> dict:
    """
    Label Elliott Waves from swing points.
    Tries to identify 5-wave impulse (1-2-3-4-5) or ABC correction.
    """
    if len(swings) < 5:
        return {"pattern": "insufficient_data", "waves": []}

    result = {"pattern": "unknown", "waves": [], "fib_levels": []}

    # Check for 5-wave impulse up: L-H-L-H-L-H sequence
    # Starting from a low: Wave1(H), Wave2(L), Wave3(H), Wave4(L), Wave5(H)
    waves = []
    for i in range(len(swings) - 4):
        s0, s1, s2, s3, s4 = swings[i:i + 5]

        # Impulse up pattern: L-H-L-H-L (5 waves up starting from low)
        if s0[2] == "L" and s1[2] == "H" and s2[2] == "L" and s3[2] == "H" and s4[2] == "L":
            w1_start = s0[1]
            w1_end = s1[1]
            w2_end = s2[1]
            w3_end = s3[1]
            w4_end = s4[1]

            w1 = w1_end - w1_start
            w2_retrace = (w1_end - w2_end) / w1 if w1 > 0 else 0
            w3 = w3_end - w2_end
            w4_retrace = (w3_end - w4_end) / w3 if w3 > 0 else 0

            # Fibonacci validation for wave relationships
            valid_w2 = 0.3 < w2_retrace < 0.8
            valid_w3 = w3 > w1 * 0.8  # Wave 3 should be at least 80% of W1
            valid_w4 = 0.15 < w4_retrace < 0.5

            if valid_w2 and valid_w3 and valid_w4:
                fib_ext = _compute_fib_levels(w1_start, w3_end, w4_end)

                waves = [
                    {"wave": "1", "type": "impulse_up", "start": float(w1_start), "end": float(w1_end)},
                    {"wave": "2", "type": "corrective_down", "start": float(w1_end), "end": float(w2_end),
                     "retracement": f"{w2_retrace*100:.1f}%"},
                    {"wave": "3", "type": "impulse_up", "start": float(w2_end), "end": float(w3_end),
                     "vs_w1": f"{w3/w1:.2f}x"},
                    {"wave": "4", "type": "corrective_down", "start": float(w3_end), "end": float(w4_end),
                     "retracement": f"{w4_retrace*100:.1f}%"},
                    {"wave": "5", "type": "projected", "target": "projecting", "projection": fib_ext},
                ]
                result = {
                    "pattern": "impulse_5wave_up",
                    "current_wave": "Wave 4 complete / Wave 5 pending",
                    "waves": waves,
                    "fib_levels": fib_ext,
                }
                break

        # Impulse down pattern: H-L-H-L-H (5 waves down)
        if s0[2] == "H" and s1[2] == "L" and s2[2] == "H" and s3[2] == "L" and s4[2] == "H":
            w1_start = s0[1]
            w1_end = s1[1]
            w2_end = s2[1]
            w3_end = s3[1]

            w1 = w1_start - w1_end
            w2_retrace = (w2_end - w1_end) / w1 if w1 > 0 else 0
            w3 = w2_end - w3_end
            valid_w2 = 0.3 < w2_retrace < 0.8
            valid_w3 = w3 > w1 * 0.8

            if valid_w2 and valid_w3:
                fib_ext = _compute_fib_levels_down(w1_start, w3_end, s4[1])
                waves = [
                    {"wave": "1", "type": "impulse_down", "start": float(w1_start), "end": float(w1_end)},
                    {"wave": "2", "type": "corrective_up", "start": float(w1_end), "end": float(w2_end),
                     "retracement": f"{w2_retrace*100:.1f}%"},
                    {"wave": "3", "type": "impulse_down", "start": float(w2_end), "end": float(w3_end)},
                ]
                result = {
                    "pattern": "impulse_5wave_down",
                    "current_wave": "Wave 3 complete / Wave 4 pending",
                    "waves": waves,
                    "fib_levels": fib_ext,
                }
                break

    return result


def _compute_fib_levels(w1_start: float, w3_top: float, w4_bottom: float) -> list[dict]:
    """Compute Fibonacci projection levels for Wave 5."""
    levels = []
    diff = w3_top - w1_start
    ratios = [0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618]
    for r in ratios:
        levels.append({
            "ratio": r,
            "price": round(w4_bottom + diff * r, 3),
            "label": f"W5_{r}",
        })
    return levels


def _compute_fib_levels_down(w1_start: float, w3_bottom: float, w4_top: float) -> list[dict]:
    """Compute Fibonacci projection levels for Wave 5 down."""
    levels = []
    diff = w1_start - w3_bottom
    ratios = [0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618]
    for r in ratios:
        levels.append({
            "ratio": r,
            "price": round(w4_top - diff * r, 3),
            "label": f"W5_down_{r}",
        })
    return levels


def compute_fib_retracement(high: float, low: float) -> dict:
    """Compute Fibonacci retracement grid."""
    diff = high - low
    levels = {}
    for r in [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]:
        levels[f"{r:.3f}"] = round(high - diff * r, 3)
    return levels


def generate_elliott_signals(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """
    Generate Elliott Wave-based trading signals.

    Returns DataFrame with columns: date, signal, price, wave_info, fib_targets
    """
    close = df["close"].values
    swings = find_swings(close, window=window)
    wave_result = label_waves(swings)

    signals = []

    if wave_result.get("pattern") == "impulse_5wave_up":
        waves = wave_result.get("waves", [])
        if len(waves) >= 4:
            w4 = waves[3]
            fib_levels = wave_result.get("fib_levels", [])

            # Buy signal at Wave 4 completion
            for lvl in fib_levels:
                if lvl["ratio"] == 0.618:
                    signals.append({
                        "date": str(df.iloc[-1].get("date", "")) if "date" in df.columns else "",
                        "signal": "BUY",
                        "price": w4["end"],
                        "wave_info": f"Wave 4 complete at {w4['end']:.2f}",
                        "fib_targets": json.dumps(fib_levels, ensure_ascii=False),
                        "target_618": lvl["price"],
                    })
                    break

    elif wave_result.get("pattern") == "impulse_5wave_down":
        waves = wave_result.get("waves", [])
        if len(waves) >= 3:
            w3 = waves[2]
            signals.append({
                "date": str(df.iloc[-1].get("date", "")) if "date" in df.columns else "",
                "signal": "SELL",
                "price": w3["end"],
                "wave_info": f"Bearish impulse wave 3 at {w3['end']:.2f}",
                "fib_targets": json.dumps(wave_result.get("fib_levels", [])),
            })

    return pd.DataFrame(signals) if signals else pd.DataFrame()


def analyze_wave_structure(symbol: str, df: pd.DataFrame) -> dict:
    """
    Full Elliott Wave analysis for a symbol.

    Returns comprehensive wave structure analysis.
    """
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values

    swings = find_swings(close, window=5)
    wave_result = label_waves(swings)

    # Also compute Fibonacci retracement from major high to major low
    if len(close) > 100:
        major_high = float(np.max(high[-100:]))
        major_low = float(np.min(low[-100:]))
        fib_ret = compute_fib_retracement(major_high, major_low)

        # Use Rust core if available
        try:
            import sancai_core
            rust_fib = json.loads(sancai_core.fib_retrace(major_high, major_low))
        except Exception:
            rust_fib = []
    else:
        fib_ret = {}
        rust_fib = []

    return {
        "symbol": symbol,
        "latest_price": float(close[-1]) if len(close) > 0 else None,
        "wave_structure": wave_result,
        "swing_points": {
            "highs": [{"index": s[0], "price": s[1]} for s in swings if s[2] == "H"][-10:],
            "lows": [{"index": s[0], "price": s[1]} for s in swings if s[2] == "L"][-10:],
        },
        "fibonacci_retracement": fib_ret,
        "fibonacci_rust": rust_fib,
        "data_points": len(close),
    }
