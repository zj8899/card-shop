"""Pipeline + Dashboard API — 决策管线 / 仪表盘聚合 / 飞书开关."""
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.utils.response import ok

logger = logging.getLogger(__name__)
router = APIRouter()
PROJECT_ROOT = Path(__file__).parent.parent.parent

TOGGLES_PATH = PROJECT_ROOT / "data" / "feishu_toggles.json"
PLAN_PATH = PROJECT_ROOT / "data" / "daily_plan.json"


class FeishuToggleReq(BaseModel):
    enabled: bool = True
    daily_decision: bool = True
    auction_confirm: bool = True
    daily_close: bool = True
    evolution_report: bool = True
    realtime_signal: bool = False


# ══════════════════════════════════════════════════════════════
# Dashboard 聚合端点
# ══════════════════════════════════════════════════════════════

@router.get("/dashboard/summary")
async def dashboard_summary():
    """仪表盘聚合: 今日决策 + 策略排名 + 竞价验证 + 进化记录。前端 30s 轮询。"""
    result = {
        "ts": datetime.now().isoformat(),
        "today_decision": _get_today_decision(),
        "strategy_ranking": _get_strategy_ranking(),
        "latest_evolution": _get_latest_evolution(),
        "today_auction": _get_today_auction(),
    }
    return ok(result)


def _get_today_decision() -> dict:
    if not PLAN_PATH.exists():
        return {"has_plan": False}
    try:
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        steps = plan.get("steps", {})
        scan = steps.get("scan", {})
        news = steps.get("news", {})
        concept = steps.get("concept", {})
        orders = steps.get("orders", {})
        return {
            "has_plan": True,
            "date": plan.get("date"),
            "elapsed_s": plan.get("elapsed_s"),
            "candidates": scan.get("total_candidates", 0),
            "after_news": news.get("after_filter", 0),
            "after_concept": concept.get("after_filter", 0),
            "order_count": orders.get("order_count", 0),
            "total_amount": orders.get("total_amount", 0),
            "orders": orders.get("orders", [])[:10],
            "error": plan.get("error"),
        }
    except Exception:
        return {"has_plan": False}


def _get_strategy_ranking() -> list:
    try:
        from server.live_engine import get_strategy_ranking
        return get_strategy_ranking(30)
    except Exception:
        return []


def _get_latest_evolution() -> dict:
    try:
        backups_dir = PROJECT_ROOT / "backtest" / "strategies" / "_backups"
        files = sorted(backups_dir.glob("*.json"), reverse=True) if backups_dir.exists() else []
        latest = []
        for f in files[:3]:
            try:
                latest.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        # 最近生成的自定义策略
        user_dir = PROJECT_ROOT / "backtest" / "strategies" / "user_generated"
        user_files = sorted(user_dir.glob("*_gen*.py"), reverse=True) if user_dir.exists() else []
        return {
            "has_evolution": len(files) > 0 or len(user_files) > 0,
            "recent_backups": latest,
            "new_strategies": [f.name for f in user_files[:5]],
        }
    except Exception:
        return {"has_evolution": False}


def _get_today_auction() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        from server.live_engine import get_db as _gdb
        db = _gdb()
        rows = db.execute(
            "SELECT verdict, COUNT(*) as n FROM auction_confirm WHERE date=? GROUP BY verdict",
            (today,)).fetchall()
        d = {r["verdict"]: r["n"] for r in rows}
        details = [dict(r) for r in db.execute(
            "SELECT * FROM auction_confirm WHERE date=? ORDER BY gap_pct DESC LIMIT 10",
            (today,)).fetchall()]
        total = sum(d.values())
        return {
            "date": today, "confirmed": d.get("confirmed", 0),
            "denied": d.get("denied", 0), "neutral": d.get("neutral", 0),
            "total": total, "confirm_rate": round(d.get("confirmed", 0) / max(total, 1) * 100, 1),
            "details": details,
        }
    except Exception:
        return {"date": today, "confirmed": 0, "denied": 0, "neutral": 0, "total": 0}


# ══════════════════════════════════════════════════════════════
# 飞书推送开关
# ══════════════════════════════════════════════════════════════

def _load_toggles() -> dict:
    try:
        return json.loads(TOGGLES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": True, "daily_decision": True, "auction_confirm": True,
                "daily_close": True, "evolution_report": True, "realtime_signal": False}


@router.get("/admin/feishu-toggles")
async def get_toggles():
    return ok(_load_toggles())


@router.post("/admin/feishu-toggles")
async def set_toggles(req: FeishuToggleReq):
    TOGGLES_PATH.write_text(req.model_dump_json(indent=2), encoding="utf-8")
    return ok({"status": "saved"})


@router.get("/dashboard/live-positions")
async def live_positions():
    """各策略独立账户的持仓+交易记录聚合。"""
    try:
        from server.live_engine import get_db as _gdb, get_strategy_ranking
        db = _gdb()
        accounts = [dict(r) for r in db.execute(
            "SELECT * FROM accounts WHERE type='strategy' ORDER BY total_equity DESC"
        ).fetchall()]
        result = []
        MODE_LABELS = {
            "strict": "三才BP1", "strict_reverse": "追涨突破", "simple": "KDJ超卖",
            "schools": "多学派共识", "chan_theory": "缠论", "ict": "ICT",
            "price_action": "价格行为", "wyckoff": "威科夫", "morphology": "形态学",
            "gann": "江恩", "wave_theory": "波浪", "dow_theory": "道氏",
        }
        for acc in accounts:
            aid = acc["id"]
            positions = [dict(r) for r in db.execute(
                "SELECT * FROM account_positions WHERE account_id=? AND shares>0", (aid,)
            ).fetchall()]
            trades = [dict(r) for r in db.execute(
                "SELECT * FROM account_trades WHERE account_id=? ORDER BY date DESC, id DESC LIMIT 20",
                (aid,)
            ).fetchall()]
            equity = [dict(r) for r in db.execute(
                "SELECT * FROM equity_log WHERE account_id=? ORDER BY date DESC LIMIT 30",
                (aid,)
            ).fetchall()]
            result.append({
                "account": dict(acc),
                "label": MODE_LABELS.get(aid, aid),
                "positions": positions,
                "trades": trades,
                "equity": equity,
            })
        ranking = get_strategy_ranking(30)
        return ok({"accounts": result, "ranking": ranking})
    except Exception as e:
        return ok({"accounts": [], "ranking": [], "error": str(e)})


@router.get("/pipeline/status")
async def pipeline_status():
    """查询决策管线当前状态."""
    from server.pipeline import get_pipeline_status
    return ok(get_pipeline_status())


@router.post("/pipeline/run")
async def pipeline_run():
    """手动触发决策管线（调试用）。"""
    from server.pipeline import run_daily_pipeline
    result = await run_daily_pipeline()
    return ok(result)
