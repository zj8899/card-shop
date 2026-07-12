"""Strategy interface — Freqtrade pattern adapted for A-share backtesting.

Three-phase pipeline:
  1. populate_indicators(df) → augmented DataFrame
  2. populate_entry_signals(bar) → BUY signal or None
  3. populate_exit_signals(bar) → SELL signal or None

Each strategy is stateless; per-symbol state is carried in BarContext.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pandas as pd


class SignalType(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Signal:
    type: SignalType
    reason: str = ""
    price: float = 0.0
    confidence: float = 1.0


@dataclass
class BarContext:
    """Per-bar context passed to populate_entry/exit_signals.

    Encapsulates all data a strategy needs for one evaluation point,
    making strategies independent of the engine internals.
    """
    symbol: str
    index: int                                    # bar index
    price: float                                  # current close
    date_val: object                              # raw date/datetime
    date_str: str                                 # formatted date string
    in_position: bool                             # currently holding?
    entry_price: float = 0.0                      # avg cost if in position
    bars_held: int = 0                            # bars since entry
    kdj_k: float = 50.0
    kdj_d: float = 50.0
    kdj_j: float = 50.0
    mas: dict[int, float] = field(default_factory=dict)  # {period: ma_value}
    trend: str = "neutral"                        # "up" / "down" / "neutral"
    bp1_idx: int = -1                             # Buy Point 1 index (strict mode)
    volume_ratio: float = 1.0                     # recent_vol / avg_vol
    prev_high: float = 0.0                        # previous bar high
    prev_low: float = 0.0                         # previous bar low
    close_arr: list = field(default_factory=list) # full close array
    low_arr: list = field(default_factory=list)   # full low array
    school_signal: str = ""                       # school's raw signal label
    school_confidence: float = 0.0                # school's confidence (0-1)
    factor_values: dict[str, float] = field(default_factory=dict)  # factor name → value from populate_indicators


class IStrategy(ABC):
    """Abstract strategy interface (Freqtrade pattern).

    Concrete strategies implement:
      - populate_indicators(df) → DataFrame with indicator columns
      - populate_entry_signals(ctx) → Signal or None
      - populate_exit_signals(ctx) → Signal or None
    """

    name: str = "base"

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute indicators and add as columns. Override as needed."""
        return df

    @abstractmethod
    def populate_entry_signals(self, ctx: BarContext) -> Optional[Signal]:
        """Evaluate entry conditions for a single bar. Return Signal or None."""

    @abstractmethod
    def populate_exit_signals(self, ctx: BarContext) -> Optional[Signal]:
        """Evaluate exit conditions for a single bar. Return Signal or None."""
