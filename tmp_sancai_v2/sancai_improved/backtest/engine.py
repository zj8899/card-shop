"""
Main backtest engine: loads data, computes indicators via Rust core,
evaluates buy/sell rules, and records trades.
"""
import json
import logging
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .broker import SimulatedBroker, BrokerConfig
from .metrics import compute_metrics
from .strategies.registry import get_strategy
from .strategies.interface import BarContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"


def load_klines(symbol: str, period: str = "daily") -> Optional[pd.DataFrame]:
    """Load K-line data from local store. Uses DuckDB for fast multi-symbol scan."""
    try:
        from server.db import get_db
        table = "daily_klines" if period == "daily" else f"minute_{period}_klines"
        date_col = "date" if period == "daily" else "datetime"
        sql = f"SELECT * FROM {table} WHERE regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) = ? ORDER BY {date_col}"
        df = get_db().execute(sql, [symbol]).fetchdf()
        if df.empty:
            # Fallback to direct parquet
            raise FileNotFoundError(f"No DuckDB data for {symbol}")
        return df
    except Exception:
        logger.warning("DuckDB query failed, falling back to direct parquet read", exc_info=True)
        # Fallback: direct parquet read
        if period == "daily":
            fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
        else:
            fpath = DATA_DIR / "minute" / period / f"{symbol}.parquet"

        if not fpath.exists():
            logger.error(f"Data file not found: {fpath}")
            return None

        df = pd.read_parquet(fpath)
        if period == "daily" and "date" not in df.columns:
            # Parquet stored with DatetimeIndex — promote to column
            df = df.sort_index().reset_index()
            df = df.rename(columns={"index": "date"})
        elif period != "daily" and "datetime" not in df.columns:
            df = df.sort_index().reset_index()
            df = df.rename(columns={"index": "datetime"})
        else:
            df = df.sort_values("date" if period == "daily" else "datetime").reset_index(drop=True)
        return df


SINGLE_SCHOOL_MODES = {
    "chan_theory", "ict", "price_action", "wyckoff",
    "morphology", "gann", "wave_theory", "dow_theory",
}

ML_MODES = {"ml", "ml_regression", "ml_classification"}


class SancaiBacktestEngine:
    """三才回测引擎 - Sancai backtesting engine."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.broker = SimulatedBroker(
            initial_capital=self.config.get("initial_capital", 1_000_000),
            config=BrokerConfig(
                commission_rate=self.config.get("commission_rate", 0.00025),
                stamp_duty_rate=self.config.get("stamp_duty_rate", 0.001),
                slippage=self.config.get("slippage", 0.001),
            )
        )
        self.signals_log: list[dict] = []
        self.debug_info: dict = {}
        self._user_strategy_instances: dict = {}
        self._user_entry_bar: dict[str, int] = {}
        self._user_indicators_cache: dict[str, pd.DataFrame] = {}

    def load_data(self, symbols: list[str], period: str = "daily",
                  start_date: str = None, end_date: str = None) -> dict[str, pd.DataFrame]:
        """Load data for multiple symbols. Uses DuckDB bulk load for speed."""
        data = {}
        try:
            # Bulk load via DuckDB — one query for all symbols
            from server.db import get_db
            table = "daily_klines" if period == "daily" else f"minute_{period}_klines"
            date_col = "date" if period == "daily" else "datetime"
            placeholders = ", ".join(["?" for _ in symbols])
            sql = f"""
                SELECT *, regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) AS _sym
                FROM {table}
                WHERE regexp_extract(filename, '([^/\\\\]+)[.]parquet', 1) IN ({placeholders})
                ORDER BY _sym, {date_col}
            """
            df_all = get_db().execute(sql, symbols).fetchdf()
            if not df_all.empty:
                for sym, grp in df_all.groupby("_sym"):
                    df = grp.drop(columns=["_sym"])
                    if start_date:
                        df = df[df[date_col] >= pd.Timestamp(start_date).to_numpy()]
                    if end_date:
                        df = df[df[date_col] <= pd.Timestamp(end_date).to_numpy()]
                    if not df.empty:
                        data[sym] = df
                        logger.info(f"Loaded {len(df)} bars for {sym}")
                return data
        except Exception as e:
            logger.warning(f"DuckDB bulk load failed ({e}), falling back to per-symbol")

        # Fallback: per-symbol parquet read
        for symbol in symbols:
            df = load_klines(symbol, period)
            if df is not None:
                if start_date:
                    date_col = "date" if period == "daily" else "datetime"
                    df = df[df[date_col] >= pd.Timestamp(start_date)]
                if end_date:
                    df = df[df[date_col] <= pd.Timestamp(end_date)]
                if not df.empty:
                    data[symbol] = df
                    logger.info(f"Loaded {len(df)} bars for {symbol}")
        return data

    def run(self, symbols: list[str], period: str = "daily",
            start_date: str = None, end_date: str = None,
            mode: str = "simple", school_config: dict = None) -> dict:
        """
        Run backtest for given symbols and period.

        Modes:
        - "simple": MA crossover strategy
        - "strict": Full 三才 BP1/BP2 conditions with KDJ divergence
        - "strict_reverse": 追涨突破 — BP1完全反向 (新高+MA加速+KDJ超买)
        - "schools": Ensemble of 8 trading school modules (2+ schools must agree)
        - Individual school names (chan_theory, ict, ...): Single school with optional config

        Uses Rust core (sancai_core) for MA/KDJ/fibonacci computation.
        """
        self.broker.reset()
        self.signals_log = []

        # Merge school_config with defaults.yaml
        if school_config and mode in SINGLE_SCHOOL_MODES:
            defaults_path = PROJECT_ROOT / "config" / "defaults.yaml"
            if defaults_path.exists():
                with open(defaults_path, "r", encoding="utf-8") as f:
                    defaults = yaml.safe_load(f)
                school_defaults = defaults.get("school_params", {}).get(mode, {})
                if school_defaults:
                    merged = dict(school_defaults)
                    merged.update(school_config)
                    school_config = merged

        data = self.load_data(symbols, period, start_date, end_date)

        if not data:
            return {"error": "No data loaded for any symbol"}

        try:
            import sancai_core
            rust_available = True
        except ImportError:
            logger.warning("sancai_core not available, using Python fallback")
            rust_available = False

        all_trades = []
        equity_started = False

        # Process each symbol independently
        for symbol, df in data.items():
            date_col = "date" if period == "daily" else "datetime"
            close_arr = df["close"].values
            high_arr = df["high"].values
            low_arr = df["low"].values

            # Prepare OHLCV JSON for Rust
            ohlcv_data = {
                "open": df["open"].tolist(),
                "high": high_arr.tolist(),
                "low": low_arr.tolist(),
                "close": close_arr.tolist(),
                "volume": df["volume"].tolist(),
            }

            mas = {}
            kdj = {}
            skdj = {}
            trends = []

            if rust_available:
                try:
                    result_str = sancai_core.analyze_trends(
                        json.dumps(ohlcv_data), period
                    )
                    result = json.loads(result_str)

                    # Parse MAs
                    for period_str, values in result.get("mas", {}).items():
                        p = int(period_str)
                        mas[p] = np.array([v if v is not None else np.nan for v in values])

                    # Parse KDJ
                    for key in ["k", "d", "j"]:
                        vals = result.get("kdj", {}).get(key, [])
                        kdj[key] = np.array([v if v is not None else np.nan for v in vals])

                    # Parse trends
                    trends = result.get("trends", [])

                    logger.info(f"Rust core computed MAs: {list(mas.keys())} for {symbol}")

                except Exception as e:
                    logger.error(f"Rust core error: {e}, falling back to Python")
                    rust_available = False

            # Python fallback for MAs
            if not rust_available or not mas:
                minute_periods = [34, 144, 233]
                daily_periods = [5, 13, 21, 34, 55, 144, 233, 623]
                periods_use = daily_periods if period == "daily" else minute_periods
                for p in periods_use:
                    mas[p] = pd.Series(close_arr).rolling(window=p).mean().values

            # Build trend lookup
            trend_map = {}
            for t in trends:
                trend_map[(t["index"], t["period"])] = t["state"]

            # Determine MA periods
            minute_periods = [34, 144, 233]
            daily_periods = [5, 13, 21, 34, 55, 144, 233, 623]
            periods_use = daily_periods if period == "daily" else minute_periods

            # Ensure all required MAs exist
            for p in periods_use:
                if p not in mas:
                    mas[p] = pd.Series(close_arr).rolling(window=p).mean().values

            # Main backtest loop
            prev_low_min = float("inf")
            bp1_idx = None
            in_position = False
            entry_price = 0.0

            # Use the largest period that fits in the data, but at least 34
            usable_periods = [p for p in periods_use if p < len(df)]
            if not usable_periods:
                logger.warning(f"Not enough data for {symbol}: shortest period {min(periods_use)}, have {len(df)}")
                continue
            min_idx = max(34, min(usable_periods))
            min_idx = min(min_idx, len(df) - 1)

            # Record initial equity point
            first_date = str(df.iloc[min_idx][date_col])[:10]
            self.broker.record_equity(min_idx, first_date)

            for i in range(min_idx, len(df)):
                price = close_arr[i]
                date_val = df.iloc[i][date_col]
                date_str = str(date_val)[:10]

                # Update equity recording every 20 bars and at the end
                if i % 20 == 0:
                    self.broker.record_equity(i, date_str)

                # Update position tracking
                if in_position:
                    self.broker.update_positions(symbol, price)

                # Track previous minimum low
                if i > 0:
                    prev_low_min = min(prev_low_min, low_arr[i - 1])

                # --- Strategy Logic ---
                if mode == "simple":
                    self._simple_crossover_strategy(
                        symbol, i, price, date_val, date_str, mas, close_arr, df, in_position
                    )
                    in_position = symbol in self.broker.positions
                elif mode == "schools":
                    self._schools_strategy(
                        symbol, i, price, date_val, date_str, df, in_position
                    )
                    in_position = symbol in self.broker.positions
                elif mode in SINGLE_SCHOOL_MODES:
                    self._single_school_strategy(
                        symbol, i, price, date_val, date_str, df, in_position,
                        mode, school_config
                    )
                    in_position = symbol in self.broker.positions
                elif mode.startswith("user_"):
                    self._user_strategy(
                        symbol, i, price, date_val, date_str,
                        df, mas, kdj, close_arr, low_arr, mode
                    )
                    in_position = symbol in self.broker.positions
                elif mode in ML_MODES:
                    self._ml_strategy(
                        symbol, i, price, date_val, date_str,
                        df, in_position, mode
                    )
                    in_position = symbol in self.broker.positions
                elif mode == "strict_reverse":
                    self._strict_reverse_strategy(
                        symbol, i, price, date_val, date_str,
                        mas, kdj, trend_map, high_arr,
                        bp1_idx, in_position, entry_price,
                        close_arr, df
                    )
                    in_position = symbol in self.broker.positions
                else:
                    # Strict 三才 rules
                    self._strict_sancai_strategy(
                        symbol, i, price, date_val, date_str,
                        mas, kdj, trend_map, low_arr,
                        bp1_idx, in_position, entry_price,
                        close_arr, df
                    )

            # Record final equity
            self.broker.record_equity(len(df) - 1, str(df.iloc[-1][date_col])[:10])

            # Close any remaining positions at last price
            for sym, pos in list(self.broker.positions.items()):
                if pos.quantity > 0:
                    last_price = close_arr[-1]
                    self.broker.submit_order(
                        sym, "sell", pos.quantity, last_price,
                        df.iloc[-1][date_col],
                        reason="回测结束平仓"
                    )

        # Compute metrics
        trades = self.broker.trade_log
        metrics = compute_metrics(self.broker.equity_curve, trades)

        # ── 数据新鲜度 ──
        latest_data_date = ""
        staleness_days = 0
        try:
            for sym, df in data.items():
                if len(df) > 0 and "date" in df.columns:
                    d = str(df.iloc[-1]["date"])[:10]
                    if not latest_data_date or d > latest_data_date:
                        latest_data_date = d
            if latest_data_date:
                import pandas as pd
                staleness_days = (pd.Timestamp.now() - pd.Timestamp(latest_data_date)).days
        except Exception:
            pass

        # ── Event engine: batch-emit all signals ──
        try:
            from server.events import event_engine, EventType
            for sig in self.signals_log:
                event_engine.emit(EventType.SIGNAL, sig, source="backtest_engine")
        except ImportError:
            pass

        return {
            "metrics": metrics,
            "equity_curve": self.broker.equity_curve,
            "trades": [
                {
                    "date": str(t.fill_time)[:19] if t.fill_time else "",
                    "symbol": t.symbol,
                    "side": t.side,
                    "price": t.fill_price,
                    "quantity": t.quantity,
                    "commission": t.commission,
                    "stamp_duty": t.stamp_duty,
                    "pnl": round(float(t.pnl), 2) if np.isfinite(t.pnl) else 0.0,
                    "reason": t.reason,
                    "status": t.status.value,
                }
                for t in trades
            ],
            "signals": self.signals_log,
            "final_equity": self.broker.total_equity,
            "cash": self.broker.cash,
            "latest_data_date": latest_data_date,
            "staleness_days": staleness_days,
        }


    def _simple_crossover_strategy(self, symbol, i, price, date_val, date_str,
                                     mas, close_arr, df, in_position):
        """KDJ mean-reversion with trend filter targeting >50% win rate.

        Entry: KDJ oversold bounce in uptrend
          1. KDJ K < 35 (oversold zone, relaxed from 30)
          2. K crosses above D today (bullish reversal)
          3. Price > MA34 (medium-term trend intact)
          4. Was deeply oversold within last 5 bars (K < 25)

        Exit:
          1. KDJ K > 70 and K crosses below D (overbought reversal, min 3 bars held)
          2. Hard stop -5%
          3. Take profit +10%
          4. Time stop: held 30+ bars, K > 50, and gain < 2%
        """
        ma21 = mas.get(21)
        ma34 = mas.get(34)
        ma144 = mas.get(144)
        if ma21 is None or ma34 is None:
            return
        if i < 60:
            return

        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        v = df["volume"].values

        # Compute KDJ locally for precision
        if not hasattr(self, '_kdj_cache'):
            self._kdj_cache = {}
        if symbol not in self._kdj_cache:
            self._kdj_cache[symbol] = self._compute_kdj_local(h, l, c)
        k_vals, d_vals, j_vals = self._kdj_cache[symbol]

        avg_vol = v[i-20:i].mean() if i >= 20 else 1
        high_i = h[i]

        pos = self.broker.positions.get(symbol)
        entry_price = pos.avg_cost if (pos and pos.quantity > 0) else 0.0

        if not hasattr(self, '_entry_high'):
            self._entry_high = {}
        if in_position:
            self._entry_high[symbol] = max(self._entry_high.get(symbol, high_i), high_i)
        elif symbol in self._entry_high:
            del self._entry_high[symbol]

        if not in_position:
            # ---- KDJ OVERSOLD BOUNCE ENTRY ----
            k_now = k_vals[i] if not np.isnan(k_vals[i]) else 50
            d_now = d_vals[i] if not np.isnan(d_vals[i]) else 50
            k_prev = k_vals[i-1] if not np.isnan(k_vals[i-1]) else 50
            d_prev = d_vals[i-1] if not np.isnan(d_vals[i-1]) else 50

            # Oversold: K < 35
            oversold = k_now < 35
            # Was deeply oversold within last 5 bars
            deep_oversold = False
            for lookback in range(1, min(6, i)):
                if not np.isnan(k_vals[i-lookback]) and k_vals[i-lookback] < 25:
                    deep_oversold = True
                    break
            # Bullish crossover: K crosses above D
            k_cross_up = k_prev <= d_prev and k_now > d_now
            # Medium-term trend: price > MA34
            trend_ok = price > ma34[i]

            entry_ok = oversold and k_cross_up and trend_ok and deep_oversold

            if entry_ok:
                atr = self._calc_atr(df, i)
                stop_dist = atr * 2.5 if not np.isnan(atr) else price * 0.05
                risk_amount = self.broker.total_equity * 0.02
                qty = max(
                    self.broker.config.min_lot,
                    int(risk_amount / stop_dist / self.broker.config.min_lot)
                    * self.broker.config.min_lot
                )
                max_qty = int(self.broker.cash * 0.2 / price / self.broker.config.min_lot) * self.broker.config.min_lot
                qty = min(qty, max_qty)
                if qty < self.broker.config.min_lot:
                    return

                order = self.broker.submit_order(
                    symbol, "buy", qty, price, date_val,
                    reason=f"KDJ超卖反弹: K={k_now:.1f} D={d_now:.1f} K上穿D 价格>MA34({ma34[i]:.2f})"
                )
                if order.status.value == "filled":
                    self.signals_log.append({
                        "date": date_str, "symbol": symbol,
                        "signal": "BUY", "price": price,
                        "reason": order.reason,
                    })
                    self._entry_high[symbol] = high_i
                    if not hasattr(self, '_entry_bar'):
                        self._entry_bar = {}
                    self._entry_bar[symbol] = i
                    logger.info(f"BUY @ {date_str} {symbol} {price:.2f} x{qty} | KDJ超卖反弹 K={k_now:.1f}")

        else:
            # ---- EXIT ----
            exit_sig = False
            exit_reason = ""

            k_now = k_vals[i] if not np.isnan(k_vals[i]) else 50
            d_now = d_vals[i] if not np.isnan(d_vals[i]) else 50
            k_prev = k_vals[i-1] if not np.isnan(k_vals[i-1]) else 50
            d_prev = d_vals[i-1] if not np.isnan(d_vals[i-1]) else 50
            gain_pct = (price - entry_price) / entry_price if entry_price > 0 else 0
            entry_high = self._entry_high.get(symbol, high_i)
            bars_held = i - getattr(self, '_entry_bar', {}).get(symbol, i)

            # 1. KDJ overbought dead cross (min 3 bars held)
            if bars_held > 3 and k_now > 70 and k_prev >= d_prev and k_now < d_now:
                exit_sig = True
                exit_reason = f"KDJ超买死叉: K={k_now:.1f}<D={d_now:.1f}"

            # 2. Hard stop -5%
            if not exit_sig and price < entry_price * 0.95:
                exit_sig = True
                exit_reason = f"硬止损-5%: {entry_price:.2f}->{price:.2f}"

            # 3. Take profit +10%
            if not exit_sig and price > entry_price * 1.10:
                exit_sig = True
                exit_reason = f"止盈+10%: {entry_price:.2f}->{price:.2f}"

            # 4. Time stop: 30+ bars, not oversold anymore, minimal gain
            if not exit_sig and bars_held > 30 and gain_pct < 0.02 and k_now > 50:
                exit_sig = True
                exit_reason = f"时间止盈: 持有{bars_held}根 K={k_now:.1f} 盈利{gain_pct*100:.1f}%"

            if exit_sig and self.broker.can_sell(symbol, date_val):
                pos = self.broker.positions.get(symbol)
                if pos and pos.quantity > 0:
                    order = self.broker.submit_order(
                        symbol, "sell", pos.quantity, price, date_val,
                        reason=exit_reason
                    )
                    if order.status.value == "filled":
                        self.signals_log.append({
                            "date": date_str, "symbol": symbol,
                            "signal": "EXIT", "price": price,
                            "reason": exit_reason,
                        })
                        if symbol in self._entry_high:
                            del self._entry_high[symbol]
                        if hasattr(self, '_entry_bar') and symbol in self._entry_bar:
                            del self._entry_bar[symbol]
                        logger.info(f"SELL @ {date_str} {symbol} {price:.2f} | {exit_reason}")

    def _compute_kdj_local(self, high, low, close, n=9):
        """Compute KDJ values locally."""
        k_vals = np.full(len(close), np.nan)
        d_vals = np.full(len(close), np.nan)
        j_vals = np.full(len(close), np.nan)
        k, d = 50.0, 50.0
        for i in range(n, len(close)):
            hh = high[i-n+1:i+1].max()
            ll = low[i-n+1:i+1].min()
            if hh - ll < 1e-6:
                rsv = 50.0
            else:
                rsv = (close[i] - ll) / (hh - ll) * 100
            k = 2/3 * k + 1/3 * rsv
            d = 2/3 * d + 1/3 * k
            k_vals[i] = k
            d_vals[i] = d
            j_vals[i] = 3*k - 2*d
        return k_vals, d_vals, j_vals

    def _single_school_strategy(self, symbol, i, price, date_val, date_str, df, in_position,
                                 school_name, school_config=None):
        """Single-school strategy: run one school's signals with optional config gating.

        BUY: school generates BUY signal at this bar
        SELL: school generates SELL signal, or risk management triggers
        """
        if not hasattr(self, '_single_school_cache'):
            self._single_school_cache = {}
        if not hasattr(self, '_entry_high_single'):
            self._entry_high_single = {}

        cache_key = (symbol, school_name)
        if cache_key not in self._single_school_cache:
            from .schools import SCHOOLS
            cls = SCHOOLS.get(school_name)
            if cls is None:
                return
            sigs_by_date = {}
            try:
                school = cls()
                sigs_df = school.generate_signals(df, school_config)
                if len(sigs_df) > 0:
                    for _, row in sigs_df.iterrows():
                        d = str(row.get("date", ""))[:10]
                        if d not in sigs_by_date:
                            sigs_by_date[d] = {"BUY": False, "SELL": False, "reasons": [], "conditions_met": []}
                        sig = str(row.get("signal", ""))
                        if sig in ("BUY", "SELL"):
                            sigs_by_date[d][sig] = True
                            reason = str(row.get("reason", ""))
                            if reason:
                                sigs_by_date[d]["reasons"].append(reason)
                            conditions = row.get("conditions_met", None)
                            if conditions is not None and not (isinstance(conditions, float) and np.isnan(conditions)):
                                if isinstance(conditions, list):
                                    sigs_by_date[d]["conditions_met"].extend(conditions)
                                elif isinstance(conditions, str):
                                    sigs_by_date[d]["conditions_met"].append(conditions)
            except Exception:
                logger.warning("Failed to process signal row from single-school cache", exc_info=True)
                pass
            self._single_school_cache[cache_key] = sigs_by_date

        sigs_by_date = self._single_school_cache.get(cache_key, {})
        bar_signals = sigs_by_date.get(date_str, {"BUY": False, "SELL": False, "reasons": [], "conditions_met": []})
        has_buy = bar_signals.get("BUY", False)
        has_sell = bar_signals.get("SELL", False)
        reasons = bar_signals.get("reasons", [])

        pos = self.broker.positions.get(symbol)
        entry_price = pos.avg_cost if (pos and pos.quantity > 0) else 0.0

        if not in_position:
            if has_buy:
                reason_text = "; ".join(reasons[:3]) if reasons else "买入信号"
                atr = self._calc_atr(df, i)
                stop_dist = atr * 2.0 if not np.isnan(atr) else price * 0.04
                risk_amount = self.broker.total_equity * 0.02
                qty = max(
                    self.broker.config.min_lot,
                    int(risk_amount / stop_dist / self.broker.config.min_lot)
                    * self.broker.config.min_lot
                )
                max_qty = int(self.broker.cash * 0.2 / price / self.broker.config.min_lot) * self.broker.config.min_lot
                qty = min(qty, max_qty)
                if qty < self.broker.config.min_lot:
                    return

                order = self.broker.submit_order(
                    symbol, "buy", qty, price, date_val,
                    reason=f"{school_name}: {reason_text}"
                )
                if order.status.value == "filled":
                    self.signals_log.append({
                        "date": date_str, "symbol": symbol,
                        "signal": "BUY", "price": price,
                        "reason": order.reason,
                        "conditions_met": bar_signals.get("conditions_met", []),
                    })
                    self._entry_high_single[cache_key] = df["high"].values[i]
                    logger.info(f"BUY @ {date_str} {symbol} {price:.2f} x{qty} | {school_name}")
        else:
            exit_sig = False
            exit_reason = ""
            gain_pct = (price - entry_price) / entry_price if entry_price > 0 else 0
            high_i = df["high"].values[i]
            prev_high = self._entry_high_single.get(cache_key, high_i)
            self._entry_high_single[cache_key] = max(prev_high, high_i)

            if has_sell:
                exit_sig = True
                exit_reason = f"{school_name}卖出: {'; '.join(reasons[:2])}" if reasons else f"{school_name}卖出信号"

            atr = self._calc_atr(df, i)
            if not exit_sig and gain_pct > 0.03 and not np.isnan(atr):
                trail_stop = self._entry_high_single[cache_key] - atr * 2.5
                if price < trail_stop:
                    exit_sig = True
                    exit_reason = f"移动止损: 高{self._entry_high_single[cache_key]:.2f}-2.5ATR={trail_stop:.2f}"

            if not exit_sig and price < entry_price * 0.95:
                exit_sig = True
                exit_reason = f"硬止损-5%: {entry_price:.2f}->{price:.2f}"

            if not exit_sig and price > entry_price * 1.15:
                exit_sig = True
                exit_reason = f"止盈+15%: {entry_price:.2f}->{price:.2f}"

            if exit_sig and self.broker.can_sell(symbol, date_val):
                pos = self.broker.positions.get(symbol)
                if pos and pos.quantity > 0:
                    order = self.broker.submit_order(
                        symbol, "sell", pos.quantity, price, date_val,
                        reason=exit_reason
                    )
                    if order.status.value == "filled":
                        self.signals_log.append({
                            "date": date_str, "symbol": symbol,
                            "signal": "EXIT", "price": price,
                            "reason": exit_reason,
                        })
                        if cache_key in self._entry_high_single:
                            del self._entry_high_single[cache_key]
                        logger.info(f"SELL @ {date_str} {symbol} {price:.2f} | {exit_reason}")

    def _schools_strategy(self, symbol, i, price, date_val, date_str, df, in_position):
        """Ensemble strategy using all 8 trading school modules.

        BUY: 2+ schools generate BUY signal at this bar
        SELL: 1+ school generates SELL signal, or risk management triggers
        """
        if not hasattr(self, '_school_signals_cache'):
            self._school_signals_cache = {}

        if symbol not in self._school_signals_cache:
            from .schools import SCHOOLS
            sigs_by_date = {}
            for sid, cls in SCHOOLS.items():
                try:
                    school = cls()
                    sigs_df = school.generate_signals(df)
                    if len(sigs_df) > 0:
                        for _, row in sigs_df.iterrows():
                            d = str(row.get("date", ""))[:10]
                            if d not in sigs_by_date:
                                sigs_by_date[d] = {"BUY": [], "SELL": []}
                            sig = str(row.get("signal", ""))
                            if sig in ("BUY", "SELL"):
                                sigs_by_date[d][sig].append(sid)
                except Exception:
                    logger.warning("School %s signal generation failed", sid, exc_info=True)
                    pass
            self._school_signals_cache[symbol] = sigs_by_date

        sigs_by_date = self._school_signals_cache.get(symbol, {})
        bar_signals = sigs_by_date.get(date_str, {"BUY": [], "SELL": []})
        buy_votes = len(bar_signals.get("BUY", []))
        sell_votes = len(bar_signals.get("SELL", []))

        pos = self.broker.positions.get(symbol)
        entry_price = pos.avg_cost if (pos and pos.quantity > 0) else 0.0

        if not hasattr(self, '_entry_high2'):
            self._entry_high2 = {}

        if not in_position:
            if buy_votes >= 2:
                schools_list = ", ".join(bar_signals["BUY"][:4])
                atr = self._calc_atr(df, i)
                stop_dist = atr * 2.0 if not np.isnan(atr) else price * 0.04
                risk_amount = self.broker.total_equity * 0.02
                qty = max(
                    self.broker.config.min_lot,
                    int(risk_amount / stop_dist / self.broker.config.min_lot)
                    * self.broker.config.min_lot
                )
                max_qty = int(self.broker.cash * 0.2 / price / self.broker.config.min_lot) * self.broker.config.min_lot
                qty = min(qty, max_qty)
                if qty < self.broker.config.min_lot:
                    return

                order = self.broker.submit_order(
                    symbol, "buy", qty, price, date_val,
                    reason=f"多流派共识买入({buy_votes}/8): {schools_list}"
                )
                if order.status.value == "filled":
                    self.signals_log.append({
                        "date": date_str, "symbol": symbol,
                        "signal": "BUY", "price": price,
                        "reason": order.reason,
                    })
                    self._entry_high2[symbol] = df["high"].values[i]
                    logger.info(f"BUY @ {date_str} {symbol} {price:.2f} x{qty} | {buy_votes}流派共识")
        else:
            exit_sig = False
            exit_reason = ""
            gain_pct = (price - entry_price) / entry_price if entry_price > 0 else 0
            high_i = df["high"].values[i]
            self._entry_high2[symbol] = max(self._entry_high2.get(symbol, high_i), high_i)
            entry_high = self._entry_high2[symbol]

            # School sell signal
            if sell_votes >= 1:
                exit_sig = True
                exit_reason = f"流派卖出信号({sell_votes}/8): {', '.join(bar_signals['SELL'][:3])}"

            # Risk management
            atr = self._calc_atr(df, i)
            if not exit_sig and gain_pct > 0.03 and not np.isnan(atr):
                trail_stop = entry_high - atr * 2.5
                if price < trail_stop:
                    exit_sig = True
                    exit_reason = f"移动止损: 高{entry_high:.2f}-2.5ATR={trail_stop:.2f}"

            if not exit_sig and price < entry_price * 0.95:
                exit_sig = True
                exit_reason = f"硬止损-5%: {entry_price:.2f}->{price:.2f}"

            if not exit_sig and price > entry_price * 1.15:
                exit_sig = True
                exit_reason = f"止盈+15%: {entry_price:.2f}->{price:.2f}"

            if exit_sig and self.broker.can_sell(symbol, date_val):
                pos = self.broker.positions.get(symbol)
                if pos and pos.quantity > 0:
                    order = self.broker.submit_order(
                        symbol, "sell", pos.quantity, price, date_val,
                        reason=exit_reason
                    )
                    if order.status.value == "filled":
                        self.signals_log.append({
                            "date": date_str, "symbol": symbol,
                            "signal": "EXIT", "price": price,
                            "reason": exit_reason,
                        })
                        if symbol in self._entry_high2:
                            del self._entry_high2[symbol]
                        logger.info(f"SELL @ {date_str} {symbol} {price:.2f} | {exit_reason}")

    # ── User Strategy Bridge (IStrategy interface) ──

    def _user_strategy(self, symbol, i, price, date_val, date_str,
                       df, mas, kdj, close_arr, low_arr, mode):
        """Bridge to user-generated IStrategy classes.

        Loads the strategy class from the registry, pre-computes indicators
        on first bar, builds a BarContext per bar, and routes entry/exit
        signals through the broker.
        """
        from .strategies.registry import get_strategy
        from .strategies.interface import BarContext

        # Lazy-load and cache strategy instance per mode
        if mode not in self._user_strategy_instances:
            cls = get_strategy(mode)
            if cls is None:
                logger.error(f"User strategy '{mode}' not found in registry")
                return
            strat = cls()
            self._user_strategy_instances[mode] = strat
            # Pre-compute indicators on first bar
            if symbol not in self._user_indicators_cache:
                try:
                    df_with_indicators = strat.populate_indicators(df.copy())
                    self._user_indicators_cache[symbol] = df_with_indicators
                    logger.info(f"User strategy {mode}: indicators computed for {symbol}")
                except Exception as e:
                    logger.error(f"User strategy {mode}: indicator error: {e}")
                    return

        strat = self._user_strategy_instances[mode]
        df_ind = self._user_indicators_cache.get(symbol, df)

        # Build BarContext
        pos = self.broker.positions.get(symbol)
        in_position = pos is not None and pos.quantity > 0

        ctx = BarContext(
            symbol=symbol,
            index=i,
            price=price,
            date_val=date_val,
            date_str=date_str,
            in_position=in_position,
        )

        # Position tracking
        if in_position:
            ctx.entry_price = pos.avg_cost
            ctx.bars_held = i - self._user_entry_bar.get(symbol, i)
        else:
            ctx.entry_price = 0.0
            ctx.bars_held = 0

        # KDJ from engine cache
        k_vals, d_vals, j_vals = None, None, None
        if symbol in getattr(self, '_kdj_cache', {}):
            k_vals, d_vals, j_vals = self._kdj_cache[symbol]
        elif kdj:
            k_vals = kdj.get("k", [])
            d_vals = kdj.get("d", [])
            j_vals = kdj.get("j", [])
        if k_vals is not None and i < len(k_vals):
            ctx.kdj_k = float(k_vals[i]) if not np.isnan(k_vals[i]) else 50.0
            ctx.kdj_d = float(d_vals[i]) if not np.isnan(d_vals[i]) else 50.0
            ctx.kdj_j = float(j_vals[i]) if not np.isnan(j_vals[i]) else 50.0

        # MAs
        ctx.mas = {}
        for p, ma_arr in mas.items():
            if i < len(ma_arr) and not np.isnan(ma_arr[i]):
                ctx.mas[p] = float(ma_arr[i])

        # Close array, low array (up to current bar)
        ctx.close_arr = close_arr[:i + 1].tolist()
        ctx.low_arr = low_arr[:i + 1].tolist()

        # Previous bar info
        ctx.prev_high = float(df["high"].values[i - 1]) if i > 0 else price
        ctx.prev_low = float(df["low"].values[i - 1]) if i > 0 else price

        # Volume ratio
        if i >= 20:
            avg_vol = df["volume"].values[i - 20:i].mean()
            ctx.volume_ratio = float(df["volume"].values[i] / avg_vol) if avg_vol > 0 else 1.0

        # Trend detection (simple: price vs MA34)
        ma34_val = ctx.mas.get(34, price)
        if price > ma34_val * 1.005:
            ctx.trend = "up"
        elif price < ma34_val * 0.995:
            ctx.trend = "down"
        else:
            ctx.trend = "neutral"

        # Factor values — pass indicators from populate_indicators to signal methods
        try:
            row = df_ind.iloc[i]
            ctx.factor_values = {
                col: float(row[col]) for col in df_ind.columns
                if col not in ("open", "high", "low", "close", "volume", "amount",
                               "date", "symbol", "forward_return")
                and not col.startswith("ma_")
                and isinstance(row[col], (int, float, np.floating, np.integer))
                and not np.isnan(row[col])
            }
        except Exception:
            ctx.factor_values = {}

        if not in_position:
            try:
                signal = strat.populate_entry_signals(ctx)
            except Exception as e:
                logger.warning(f"User strategy {mode} entry signal error at bar {i}: {e}")
                return
            if signal and signal.type.value == "buy":
                self._execute_user_entry(symbol, i, price, date_val, date_str,
                                         df, signal)
        else:
            try:
                signal = strat.populate_exit_signals(ctx)
            except Exception as e:
                logger.warning(f"User strategy {mode} exit signal error at bar {i}: {e}")
                return
            if signal and signal.type.value == "sell":
                self._execute_user_exit(symbol, price, date_val, date_str, signal)

    def _execute_user_entry(self, symbol, i, price, date_val, date_str,
                            df, signal):
        """Execute a BUY signal from a user strategy."""
        atr = self._calc_atr(df, i)
        stop_dist = atr * 2.0 if not np.isnan(atr) else price * 0.04
        risk_amount = self.broker.total_equity * 0.02
        qty = max(
            self.broker.config.min_lot,
            int(risk_amount / stop_dist / self.broker.config.min_lot)
            * self.broker.config.min_lot
        )
        max_qty = int(self.broker.cash * 0.2 / price /
                      self.broker.config.min_lot) * self.broker.config.min_lot
        qty = min(qty, max_qty)
        if qty < self.broker.config.min_lot:
            return

        order = self.broker.submit_order(
            symbol, "buy", qty, price, date_val,
            reason=signal.reason or "用户策略买入"
        )
        if order.status.value == "filled":
            self.signals_log.append({
                "date": date_str,
                "symbol": symbol,
                "signal": "BUY",
                "price": price,
                "reason": signal.reason,
                "confidence": signal.confidence,
            })
            self._user_entry_bar[symbol] = i
            logger.info(f"USER BUY @ {date_str} {symbol} {price:.2f} | {signal.reason}")

    def _execute_user_exit(self, symbol, price, date_val, date_str, signal):
        """Execute a SELL signal from a user strategy."""
        if not self.broker.can_sell(symbol, date_val):
            return
        pos = self.broker.positions.get(symbol)
        if not pos or pos.quantity <= 0:
            return
        order = self.broker.submit_order(
            symbol, "sell", pos.quantity, price, date_val,
            reason=signal.reason or "用户策略卖出"
        )
        if order.status.value == "filled":
            self.signals_log.append({
                "date": date_str,
                "symbol": symbol,
                "signal": "EXIT",
                "price": price,
                "reason": signal.reason,
                "confidence": signal.confidence,
            })
            if symbol in self._user_entry_bar:
                del self._user_entry_bar[symbol]
            logger.info(f"USER SELL @ {date_str} {symbol} {price:.2f} | {signal.reason}")

    def _calc_atr(self, df, i, period=14):
        if i < period:
            return np.nan
        h = df["high"].values
        l = df["low"].values
        c = df["close"].values
        tr = np.maximum(h[i-period:i] - l[i-period:i],
               np.maximum(np.abs(h[i-period:i] - np.roll(c[i-period:i], 1)),
                          np.abs(l[i-period:i] - np.roll(c[i-period:i], 1))))
        tr[0] = h[i-period] - l[i-period]
        return pd.Series(tr).mean()

    def _ml_strategy(self, symbol, i, price, date_val, date_str,
                       df, in_position, mode):
        """ML prediction strategy — factor → LightGBM → signal.

        Pre-computes all 12 factors, trains LightGBM on rolling window,
        and generates BUY/SELL based on predicted forward returns.

        Lazy-init: first call for each symbol trains the model on
        the warmup window; subsequent calls use cached predictions.
        """
        cache_key = f"ml_{symbol}"
        if not hasattr(self, '_ml_cache'):
            self._ml_cache = {}

        ml_state = self._ml_cache.get(cache_key)

        # ── First call: compute factors + train on warmup window ──
        if ml_state is None:
            try:
                from backtest.strategies.ml_strategy import MLStrategy
            except ImportError:
                logger.warning("ML strategy not available (missing research modules?)")
                return

            strategy = MLStrategy(
                task="regression" if mode == "ml" else
                     "regression" if mode == "ml_regression" else "classification",
            )

            # Compute all factor columns
            df_aug = strategy.populate_indicators(df.copy())

            # Forward return target
            df_aug["forward_return"] = df_aug["close"].pct_change(1).shift(-1)

            # Train on warmup window (first 80% of bars up to current i)
            warmup_end = min(i + 1, len(df_aug))
            warmup_start = max(0, warmup_end - strategy._train_window)
            train_df = df_aug.iloc[warmup_start:warmup_end]

            try:
                strategy._fit_model(train_df)
            except Exception as e:
                logger.warning("ML model training failed: %s", e)
                self._ml_cache[cache_key] = {"trained": False}
                return

            # Precompute predictions for bars warmup_end..end
            df_clean = df_aug.dropna(subset=strategy.feature_cols)
            X_all = df_clean[strategy.feature_cols].values.astype(np.float64)
            try:
                preds = strategy._model.predict(X_all)
            except Exception:
                self._ml_cache[cache_key] = {"trained": False}
                return

            pred_map = {}
            for idx, pred in zip(df_clean.index, preds):
                pred_map[idx] = float(pred)

            ml_state = {
                "trained": True,
                "strategy": strategy,
                "pred_map": pred_map,
                "model_ic": strategy._model_ic.get("_global", 0.0),
            }
            self._ml_cache[cache_key] = ml_state
            logger.info("ML strategy trained for %s, IC=%.4f, warmup=%d bars",
                        symbol, ml_state["model_ic"], warmup_end - warmup_start)

        if not ml_state.get("trained"):
            return

        strategy = ml_state["strategy"]
        pred_map = ml_state["pred_map"]
        pred = pred_map.get(i, 0.0)

        pos = self.broker.positions.get(symbol)
        entry_price = pos.avg_cost if (pos and pos.quantity > 0) else 0.0
        current_in_pos = pos is not None and pos.quantity > 0

        # ── Decision ──
        if not current_in_pos:
            if pred > strategy._buy_threshold:
                pos_size = self._compute_ml_position_size(symbol, price)
                if pos_size > 0:
                    order = self.broker.submit_order(
                        symbol, "buy", pos_size, price, date_val,
                        reason=f"ML预测涨 {pred:.4f} (IC={ml_state['model_ic']:.3f})"
                    )
                    if order:
                        self.signals_log.append({
                            "date": str(date_val)[:10], "symbol": symbol,
                            "signal": "BUY", "price": float(price),
                            "reason": f"ML pred={pred:.4f} IC={ml_state['model_ic']:.3f}",
                        })
        else:
            exit_signal = False
            exit_reason = ""

            if pred < strategy._sell_threshold:
                exit_signal = True
                exit_reason = f"ML预测跌 {pred:.4f}"
            elif entry_price > 0 and price < entry_price * 0.95:
                exit_signal = True
                exit_reason = "ML硬止损 -5%"
            elif entry_price > 0 and price > entry_price * 1.15:
                exit_signal = True
                exit_reason = "ML止盈 +15%"

            if exit_signal:
                self.broker.submit_order(
                    symbol, "sell", pos.quantity, price, date_val,
                    reason=exit_reason
                )
                self.signals_log.append({
                    "date": str(date_val)[:10], "symbol": symbol,
                    "signal": "SELL", "price": float(price),
                    "reason": exit_reason,
                })

    def _compute_ml_position_size(self, symbol, price):
        """ATR-based position sizing for ML strategy."""
        capital = self.broker.total_equity
        risk_pct = 0.02
        risk_amount = capital * risk_pct

        # Try ATR-based sizing
        try:
            atr = self._compute_atr(symbol, period=14)
        except Exception:
            atr = price * 0.03

        if atr <= 0:
            atr = price * 0.03

        shares = int(risk_amount / (2.5 * atr))
        shares = max(shares, 0)
        # Keep within capital
        max_shares = int((capital * 0.25) / price)  # max 25% per position
        shares = min(shares, max_shares or 1)
        shares = (shares // 100) * 100
        return shares

    def _strict_sancai_strategy(self, symbol, i, price, date_val, date_str,
                                  mas, kdj, trend_map, low_arr,
                                  bp1_idx, in_position, entry_price,
                                  close_arr, df):
        """Full 三才 BP1/BP2 strategy with strict conditions."""
        if not in_position:
            ma34 = mas.get(34)
            ma144 = mas.get(144)
            ma233 = mas.get(233)
            if ma34 is None or ma144 is None or ma233 is None:
                return

            if i < 233:
                return

            below_ma34 = price < ma34[i]
            new_low = low_arr[i] < min(low_arr[max(0, i - 233):i]) if i > 233 else False
            ma144_state = trend_map.get((i, 144), "")
            ma144_decel = ma144_state == "decelerating"
            below_ma233 = price < ma233[i]
            j_val = kdj.get("j", [50])[i] if kdj else 50
            kdj_oversold = j_val < 20

            if below_ma34 and new_low and ma144_decel and below_ma233 and kdj_oversold:
                risk_amount = self.broker.total_equity * 0.02
                stop_dist = price * 0.03
                qty = max(
                    self.broker.config.min_lot,
                    int(risk_amount / stop_dist / self.broker.config.min_lot)
                    * self.broker.config.min_lot
                )
                order = self.broker.submit_order(
                    symbol, "buy", qty, price, date_val,
                    reason=f"BP1: 破34MA+新低+144减速+破233MA+KDJ超卖"
                )
                if order.status.value == "filled":
                    self.signals_log.append({
                        "date": date_str, "symbol": symbol,
                        "signal": "BUY_POINT_1", "price": price,
                        "reason": order.reason,
                    })
                    logger.info(f"BUY(BP1) @ {date_str} {symbol} {price:.2f} x{qty}")
        else:
            ma144 = mas.get(144)
            ma233 = mas.get(233)
            exit_sig = False
            exit_reason = ""
            if ma144 is not None and ma233 is not None:
                if price < ma144[i] and price < ma233[i]:
                    exit_sig = True
                    exit_reason = "破144+233MA清仓"
                elif price < ma144[i]:
                    exit_sig = True
                    exit_reason = "破144MA减仓"

            pos = self.broker.positions.get(symbol)
            if pos and pos.avg_cost > 0:
                if price < pos.avg_cost * 0.95:
                    exit_sig = True
                    exit_reason = f"止损-5%"
                if price > pos.avg_cost * 1.15:
                    exit_sig = True
                    exit_reason = f"止盈+15%"

            if exit_sig and self.broker.can_sell(symbol, date_val):
                pos = self.broker.positions.get(symbol)
                if pos and pos.quantity > 0:
                    qty = pos.quantity // 2 if "减仓" in exit_reason else pos.quantity
                    order = self.broker.submit_order(
                        symbol, "sell", qty, price, date_val,
                        reason=exit_reason
                    )
                    if order.status.value == "filled":
                        self.signals_log.append({
                            "date": date_str, "symbol": symbol,
                            "signal": "EXIT", "price": price,
                            "reason": exit_reason,
                        })

    def _strict_reverse_strategy(self, symbol, i, price, date_val, date_str,
                                  mas, kdj, trend_map, high_arr,
                                  bp1_idx, in_position, entry_price,
                                  close_arr, df):
        """追涨突破策略 — BP1完全反向.

        Entry: price>MA34 + new 233d high + MA144 accelerating + price>MA233 + KDJ J>80
        Exit: same risk management as BP1
        """
        if not in_position:
            ma34 = mas.get(34)
            ma144 = mas.get(144)
            ma233 = mas.get(233)
            if ma34 is None or ma144 is None or ma233 is None:
                return

            if i < 233:
                return

            above_ma34 = price > ma34[i]
            new_high = high_arr[i] > max(high_arr[max(0, i - 233):i]) if i > 233 else False
            ma144_state = trend_map.get((i, 144), "")
            ma144_accel = ma144_state == "accelerating"
            above_ma233 = price > ma233[i]
            j_val = kdj.get("j", [50])[i] if kdj else 50
            kdj_overbought = j_val > 80

            if above_ma34 and new_high and ma144_accel and above_ma233 and kdj_overbought:
                risk_amount = self.broker.total_equity * 0.02
                stop_dist = price * 0.03
                qty = max(
                    self.broker.config.min_lot,
                    int(risk_amount / stop_dist / self.broker.config.min_lot)
                    * self.broker.config.min_lot
                )
                order = self.broker.submit_order(
                    symbol, "buy", qty, price, date_val,
                    reason=f"追涨突破: 站上MA34+新高+MA144加速+站上MA233+KDJ超买(J={j_val:.1f})"
                )
                if order.status.value == "filled":
                    self.signals_log.append({
                        "date": date_str, "symbol": symbol,
                        "signal": "MOMENTUM_BUY", "price": price,
                        "reason": order.reason,
                    })
                    logger.info(f"BUY(Momentum) @ {date_str} {symbol} {price:.2f} x{qty}")
        else:
            ma144 = mas.get(144)
            ma233 = mas.get(233)
            exit_sig = False
            exit_reason = ""
            if ma144 is not None and ma233 is not None:
                if price < ma144[i] and price < ma233[i]:
                    exit_sig = True
                    exit_reason = "追涨结束: 跌破144+233MA清仓"
                elif price < ma144[i]:
                    exit_sig = True
                    exit_reason = "动能衰减: 跌破144MA减仓"

            pos = self.broker.positions.get(symbol)
            if pos and pos.avg_cost > 0:
                if price < pos.avg_cost * 0.95:
                    exit_sig = True
                    exit_reason = f"止损-5%"
                if price > pos.avg_cost * 1.20:
                    exit_sig = True
                    exit_reason = f"止盈+20%"

            if exit_sig and self.broker.can_sell(symbol, date_val):
                pos = self.broker.positions.get(symbol)
                if pos and pos.quantity > 0:
                    qty = pos.quantity // 2 if "减仓" in exit_reason else pos.quantity
                    order = self.broker.submit_order(
                        symbol, "sell", qty, price, date_val,
                        reason=exit_reason
                    )
                    if order.status.value == "filled":
                        self.signals_log.append({
                            "date": date_str, "symbol": symbol,
                            "signal": "EXIT", "price": price,
                            "reason": exit_reason,
                        })


def run_backtest_cli(symbols: list[str], period: str = "daily",
                     start_date: str = None, end_date: str = None,
                     config: dict = None, mode: str = "simple"):
    """CLI-friendly backtest runner."""
    engine = SancaiBacktestEngine(config)
    result = engine.run(symbols, period, start_date, end_date, mode=mode)

    if "error" in result:
        logger.error(f"Backtest failed: {result['error']}")
        return result

    metrics = result["metrics"]
    print("\n" + "=" * 60)
    print("  三才回测结果 Sancai Backtest Results")
    print("=" * 60)
    print(f"  总收益率:     {metrics.get('total_return', 0):>8.2f}%")
    print(f"  年化收益:     {metrics.get('annualized_return', 0):>8.2f}%")
    print(f"  夏普比率:     {metrics.get('sharpe_ratio', 0):>8.2f}")
    print(f"  最大回撤:     {metrics.get('max_drawdown', 0):>8.2f}%")
    print(f"  胜率:         {metrics.get('win_rate', 0):>8.2f}%")
    print(f"  总交易次数:   {metrics.get('total_trades', 0):>8}")
    print(f"  盈亏比:       {metrics.get('profit_factor', 0):>8.2f}")
    print(f"  最终资金:     {metrics.get('final_equity', 0):>12.2f}")
    print("-" * 60)
    print(f"  交易记录数:   {len(result['trades'])}")
    print(f"  信号数:       {len(result['signals'])}")
    print("=" * 60)

    # Print last 5 trades
    if result["trades"]:
        print("\n最近 5 笔交易:")
        for t in result["trades"][-5:]:
            side_label = "买入" if t["side"] == "buy" else "卖出"
            print(f"  {t['date']} {side_label} {t['symbol']} @ {t['price']:.2f} x {t['quantity']} | {t['reason']}")

    return result
