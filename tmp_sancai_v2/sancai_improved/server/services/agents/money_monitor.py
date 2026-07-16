"""Money Flow Monitor Agent (Agent 3) — real-time capital flow surveillance.

Runs every 5 minutes during trading hours.
Monitors: order book anomalies, fund flow spikes, main force behavior.

Output: real-time alerts + money flow anomaly reports.
"""
import logging
from datetime import datetime, time as dt_time

from .base import AIAgent, AgentContext

logger = logging.getLogger(__name__)

TRADING_MORNING = (dt_time(9, 30), dt_time(11, 30))
TRADING_AFTERNOON = (dt_time(13, 0), dt_time(15, 0))


class MoneyFlowMonitorAgent(AIAgent):
    name = "money_monitor"
    role = "资金监控员"
    description = "实时监控盘口异动、资金流向异常、主力行为"
    schedule_minutes = 5  # every 5 minutes during trading

    async def analyze(self, ctx: AgentContext) -> dict:
        """Scan for money flow anomalies."""
        result = {
            "timestamp": ctx.timestamp,
            "is_trading": self._is_trading_time(),
            "anomalies": [],
            "alerts": [],
        }

        if not result["is_trading"]:
            result["alerts"].append("非交易时段，监控暂停")
            return result

        # ── 1. Scan monitored stocks for money flow signals ──
        try:
            # 优先从管线日报中取标的（反映当日真实关注），其次用持仓，兜底 3 票
            symbols = self._memory.get("monitor_symbols")  # 用户手动设的优先级最高
            if not symbols:
                try:
                    import json
                    from server.utils import DATA_DIR as _dd
                    plan_path = _dd.parent / "daily_plan.json"
                    if plan_path.exists():
                        plan = json.loads(plan_path.read_text(encoding="utf-8"))
                        orders = plan.get("steps", {}).get("orders", {}).get("orders", [])
                        symbols = list({o["symbol"] for o in orders if o.get("symbol")})
                except Exception:
                    pass
            if not symbols:
                from server.utils.holdings_utils import _load_holdings
                symbols = [h["symbol"] for h in _load_holdings() if h.get("symbol")]
            if not symbols:
                symbols = ["000001", "600519", "300750"]
            monitor_symbols = symbols[:15]  # 最多 15 只
            from pathlib import Path
            from server.utils import DATA_DIR
            import pandas as pd
            from research.engines.money_flow import analyze_money_flow

            for sym in monitor_symbols[:10]:
                fpath = DATA_DIR / "daily" / f"{sym}.parquet"
                if not fpath.exists():
                    continue

                df = pd.read_parquet(fpath)
                mf = analyze_money_flow(sym, df)

                # Detect anomalies
                if mf.money_flow_score > 80:
                    result["anomalies"].append({
                        "symbol": sym,
                        "type": "strong_inflow",
                        "score": mf.money_flow_score,
                        "phase": mf.current_phase,
                        "summary": mf.summary,
                    })
                elif mf.money_flow_score < 30:
                    result["anomalies"].append({
                        "symbol": sym,
                        "type": "strong_outflow",
                        "score": mf.money_flow_score,
                        "phase": mf.current_phase,
                        "summary": mf.summary,
                    })

                # Latest signal check
                if mf.recent_signals:
                    latest = mf.recent_signals[-1]
                    if latest.behavior in ("distribution", "board_break"):
                        result["alerts"].append(
                            f"🚨 {sym}: 检测到{latest.behavior_label}信号 ({latest.strength:.2f})"
                        )
        except Exception as e:
            logger.debug("[money_monitor] Scan failed: %s", e)

        # ── 2. Northbound flow check ──
        try:
            from data_sources.northbound import get_northbound_flow
            nb_data = get_northbound_flow()
            if nb_data:
                result["northbound"] = nb_data
        except Exception:
            pass

        return result

    async def report(self, result: dict) -> str:
        if not result.get("is_trading"):
            return f"[{self.name}] 非交易时段"

        parts = [f"## 💰 资金监控 — {result.get('timestamp', '')[:16]}", ""]

        anomalies = result.get("anomalies", [])
        if anomalies:
            parts.append("### 资金异动")
            for a in anomalies:
                emoji = "🔥" if a["type"] == "strong_inflow" else "❄️"
                parts.append(f"- {emoji} **{a['symbol']}**: {a['phase']} (健康度 {a['score']:.0f})")
        else:
            parts.append("### 资金流向正常，无显著异动")

        parts.append("")

        alerts = result.get("alerts", [])
        if alerts:
            parts.append("### ⚠️ 即时预警")
            for a in alerts:
                parts.append(f"- {a}")

        return "\n".join(parts)

    @staticmethod
    def _is_trading_time() -> bool:
        now = datetime.now().time()
        return (TRADING_MORNING[0] <= now <= TRADING_MORNING[1] or
                TRADING_AFTERNOON[0] <= now <= TRADING_AFTERNOON[1])
