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

    # Trade statistics
    if trades:
        trade_pnls = []
        for i in range(0, len(trades), 2):
            if i + 1 < len(trades):
                buy = trades[i]
                sell = trades[i + 1]
                if buy.side == "buy" and sell.side == "sell":
                    pnl = (sell.fill_price - buy.fill_price) * buy.quantity
                    pnl -= buy.commission + sell.commission + sell.stamp_duty
                    trade_pnls.append(pnl)

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

    return {
        "total_return": round(total_return * 100, 2),
        "annualized_return": round(ann_return * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "calmar_ratio": round(calmar, 2),
        "win_rate": round(win_rate * 100, 2),
        "total_trades": total_trades,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "final_equity": round(final_equity, 2),
        "initial_capital": initial_capital,
        "total_data_points": len(equity_df),
    }
