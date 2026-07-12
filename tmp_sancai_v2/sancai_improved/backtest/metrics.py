"""
Performance metrics for backtest evaluation.
"""
import numpy as np
import pandas as pd


def compute_metrics(equity_curve: list[dict], trades: list,
                    benchmark_returns: list[float] = None) -> dict:
    """
    Compute backtest performance metrics from equity curve and trades.
    """
    if not equity_curve:
        return {"error": "No equity data"}

    equity_df = pd.DataFrame(equity_curve)
    if "equity" not in equity_df.columns or len(equity_df) < 2:
        return {"error": "Insufficient equity data"}

    equity = equity_df["equity"].values
    initial_capital = equity[0]
    final_equity = equity[-1]

    # Returns
    total_return = (final_equity - initial_capital) / initial_capital

    # Daily returns (simple percentage changes)
    returns = np.diff(equity) / equity[:-1]

    # Annualized return (assuming ~252 trading days)
    if len(returns) > 1:
        ann_return = (1 + total_return) ** (252.0 / len(returns)) - 1
    else:
        ann_return = 0.0

    # Sharpe ratio
    if len(returns) > 1 and returns.std() > 0:
        sharpe = np.sqrt(252) * returns.mean() / returns.std()
    else:
        sharpe = 0.0

    # Max drawdown
    peak = np.maximum.accumulate(equity)
    drawdowns = (equity - peak) / peak
    max_drawdown = drawdowns.min()

    # Trade statistics — FIFO matching (handles partial exits & position adds)
    if trades:
        trade_pnls = []
        # Per-symbol queue of open buy lots: each lot tracks remaining shares,
        # entry price, and per-share buy commission.
        open_lots: dict = {}
        for order in trades:
            side = getattr(order, "side", None)
            qty = getattr(order, "quantity", 0) or 0
            if qty <= 0 or side not in ("buy", "sell"):
                continue
            symbol = getattr(order, "symbol", "")
            fill_price = getattr(order, "fill_price", 0.0)

            if side == "buy":
                buy_comm_per_share = (getattr(order, "commission", 0.0) or 0.0) / qty
                open_lots.setdefault(symbol, []).append({
                    "shares": qty,
                    "price": fill_price,
                    "comm_per_share": buy_comm_per_share,
                })
            else:  # sell — match against earliest open buys (FIFO)
                lots = open_lots.get(symbol, [])
                sell_costs = (getattr(order, "commission", 0.0) or 0.0) + \
                    (getattr(order, "stamp_duty", 0.0) or 0.0)
                sell_comm_per_share = sell_costs / qty
                remaining = qty
                realized = 0.0
                matched_any = False
                while remaining > 0 and lots:
                    lot = lots[0]
                    matched = min(remaining, lot["shares"])
                    gross = (fill_price - lot["price"]) * matched
                    costs = (lot["comm_per_share"] + sell_comm_per_share) * matched
                    realized += gross - costs
                    lot["shares"] -= matched
                    remaining -= matched
                    matched_any = True
                    if lot["shares"] <= 0:
                        lots.pop(0)
                # Only count sells that closed real shares (skip orphan sells)
                if matched_any:
                    trade_pnls.append(realized)

        winning_trades = [t for t in trade_pnls if t > 0]
        losing_trades = [t for t in trade_pnls if t <= 0]

        win_rate = len(winning_trades) / len(trade_pnls) if trade_pnls else 0.0
        avg_win = np.mean(winning_trades) if winning_trades else 0.0
        avg_loss = np.mean(losing_trades) if losing_trades else 0.0
        profit_factor = abs(sum(winning_trades) / sum(losing_trades)) if sum(losing_trades) != 0 else float("inf")
        total_trades = len(trade_pnls)
    else:
        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        profit_factor = 0.0
        total_trades = 0

    # Calmar ratio
    calmar = ann_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

    def _sanitize(v):
        """Replace NaN/Inf with 0.0 for JSON serialization."""
        if v is None:
            return 0.0
        try:
            f = float(v)
            if not np.isfinite(f):
                return 0.0
            return f
        except (TypeError, ValueError):
            return 0.0

    return {
        "total_return": round(_sanitize(total_return * 100), 2),
        "annualized_return": round(_sanitize(ann_return * 100), 2),
        "sharpe_ratio": round(_sanitize(sharpe), 2),
        "max_drawdown": round(_sanitize(max_drawdown * 100), 2),
        "calmar_ratio": round(_sanitize(calmar), 2),
        "win_rate": round(_sanitize(win_rate * 100), 2),
        "total_trades": total_trades,
        "avg_win": round(_sanitize(avg_win), 2),
        "avg_loss": round(_sanitize(avg_loss), 2),
        "profit_factor": round(_sanitize(profit_factor), 2),
        "final_equity": round(_sanitize(final_equity), 2),
        "initial_capital": initial_capital,
        "total_data_points": len(equity_df),
    }
