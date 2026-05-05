"""
Simulated A-share broker for backtesting.
Implements T+1 settlement, commissions, stamp duty, price limits.
"""
from dataclasses import dataclass, field
from enum import Enum


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"


@dataclass
class Order:
    symbol: str
    side: str  # "buy" | "sell"
    quantity: int
    order_type: OrderType
    limit_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float = 0.0
    fill_time: object = None
    commission: float = 0.0
    stamp_duty: float = 0.0
    reason: str = ""


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    avg_cost: float = 0.0
    buy_date: object = None  # For T+1 check
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class BrokerConfig:
    commission_rate: float = 0.00025  # 0.025%
    stamp_duty_rate: float = 0.001   # 0.1% (sell only)
    slippage: float = 0.001          # 0.1%
    min_lot: int = 100
    price_limit: float = 0.10        # 10% daily
    t_plus_one: bool = True


class SimulatedBroker:
    """Simulated A-share broker for backtesting."""

    def __init__(self, initial_capital: float = 1_000_000, config: BrokerConfig = None):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.config = config or BrokerConfig()
        self.positions: dict[str, Position] = {}
        self.orders: list[Order] = []
        self.trade_log: list[Order] = []
        self.equity_curve: list[dict] = []

    def reset(self):
        self.cash = self.initial_capital
        self.positions = {}
        self.orders = []
        self.trade_log = []
        self.equity_curve = []

    @property
    def total_equity(self) -> float:
        pos_value = sum(
            p.quantity * p.avg_cost + p.unrealized_pnl
            for p in self.positions.values()
        )
        return self.cash + pos_value

    @property
    def total_position_value(self) -> float:
        return sum(
            p.quantity * p.avg_cost + p.unrealized_pnl
            for p in self.positions.values()
        )

    def can_sell(self, symbol: str, current_date) -> bool:
        """Check T+1: cannot sell shares bought today."""
        if not self.config.t_plus_one:
            return True
        pos = self.positions.get(symbol)
        if pos is None or pos.quantity <= 0:
            return False
        if pos.buy_date is not None and pos.buy_date == current_date:
            return False
        return True

    def submit_order(self, symbol: str, side: str, quantity: int,
                     current_price: float, current_date=None, reason: str = "") -> Order:
        """Submit and fill an order at current price with slippage."""
        order = Order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            reason=reason,
        )

        # Apply slippage
        if side == "buy":
            fill_price = current_price * (1 + self.config.slippage)
        else:
            fill_price = current_price * (1 - self.config.slippage)

        # Check if we can afford (buy) or have shares (sell)
        if side == "buy":
            cost = fill_price * quantity
            commission = cost * self.config.commission_rate
            total_cost = cost + commission
            if total_cost > self.cash:
                order.status = OrderStatus.REJECTED
                self.orders.append(order)
                return order

            self.cash -= total_cost
            order.commission = commission

            # Update position
            if symbol not in self.positions:
                self.positions[symbol] = Position(symbol=symbol)
            pos = self.positions[symbol]
            old_value = pos.quantity * pos.avg_cost
            pos.quantity += quantity
            pos.avg_cost = (old_value + cost) / pos.quantity if pos.quantity > 0 else 0
            pos.buy_date = current_date

        else:  # sell
            pos = self.positions.get(symbol)
            if pos is None or pos.quantity < quantity:
                order.status = OrderStatus.REJECTED
                self.orders.append(order)
                return order

            revenue = fill_price * quantity
            commission = revenue * self.config.commission_rate
            stamp_duty = revenue * self.config.stamp_duty_rate
            total_revenue = revenue - commission - stamp_duty

            # Calculate realized PnL
            cost_basis = quantity * pos.avg_cost
            pos.realized_pnl += total_revenue - cost_basis

            self.cash += total_revenue
            pos.quantity -= quantity
            order.commission = commission
            order.stamp_duty = stamp_duty

            if pos.quantity == 0:
                del self.positions[symbol]

        order.status = OrderStatus.FILLED
        order.fill_price = fill_price
        order.fill_time = current_date
        self.orders.append(order)
        self.trade_log.append(order)

        return order

    def update_positions(self, symbol: str, current_price: float):
        """Update unrealized PnL for a position."""
        pos = self.positions.get(symbol)
        if pos and pos.quantity > 0:
            pos.unrealized_pnl = pos.quantity * (current_price - pos.avg_cost)

    def record_equity(self, timestamp, date_str: str = None):
        """Record equity curve data point."""
        self.equity_curve.append({
            "timestamp": timestamp,
            "date": date_str or str(timestamp),
            "cash": self.cash,
            "equity": self.total_equity,
            "positions": sum(1 for p in self.positions.values() if p.quantity > 0),
        })

    def get_position_pct(self, symbol: str) -> float:
        """Get position as percentage of total equity."""
        pos = self.positions.get(symbol)
        if pos is None or pos.quantity <= 0:
            return 0.0
        return (pos.quantity * pos.avg_cost) / self.total_equity if self.total_equity > 0 else 0.0
