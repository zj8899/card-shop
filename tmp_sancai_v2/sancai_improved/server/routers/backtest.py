"""Backtest API endpoints."""
import asyncio
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from server.state import app_state

router = APIRouter()


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=100)
    period: str = Field("daily", pattern="^(daily|30min|60min|15min|5min|1min)$")
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = Field(1_000_000, ge=10000)
    max_positions: int = Field(5, ge=1, le=20)
    risk_per_trade: float = Field(0.02, ge=0.005, le=0.1)
    mode: str = Field("simple", pattern="^(simple|schools|strict|strict_reverse|chan_theory|ict|price_action|wyckoff|morphology|gann|wave_theory|dow_theory|user_.+)$")
    school_config: Optional[dict] = Field(None, description="Fine-tuning flags and numeric params for individual school modes")

    @model_validator(mode="after")
    def validate_school_config(self):
        SINGLE_SCHOOL_MODES = {"chan_theory", "ict", "price_action", "wyckoff", "morphology", "gann", "wave_theory", "dow_theory"}
        if self.school_config and self.mode not in SINGLE_SCHOOL_MODES:
            raise ValueError("school_config is only valid for single-school modes (chan_theory, ict, etc.)")
        return self


class BacktestResult(BaseModel):
    task_id: str
    status: str
    metrics: Optional[dict] = None
    trades: Optional[list] = None
    equity_curve: Optional[list] = None
    signals: Optional[list] = None
    error: Optional[str] = None


@router.get("/school-params")
async def get_school_params():
    """Return parameter schema and signal toggles for all 8 trading schools.
    Used by the frontend to render the micro-tuning panel per school."""
    from backtest.schools import get_all_school_params
    return get_all_school_params()


@router.post("/run")
async def run_backtest(req: BacktestRequest):
    """Start a backtest (runs async, returns task_id)."""
    task_id = f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    app_state.set_backtest_task(task_id, {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "request": req.model_dump(),
    })

    # Run in background
    asyncio.create_task(_execute_backtest(task_id, req))

    return {"task_id": task_id, "status": "running"}


async def _execute_backtest(task_id: str, req: BacktestRequest):
    """Execute backtest in background thread."""
    try:
        from backtest.engine import SancaiBacktestEngine

        config = {
            "initial_capital": req.initial_capital,
            "max_positions": req.max_positions,
            "risk_per_trade": req.risk_per_trade,
        }

        engine = SancaiBacktestEngine(config)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    engine.run,
                    symbols=req.symbols,
                    period=req.period,
                    start_date=req.start_date,
                    end_date=req.end_date,
                    mode=req.mode,
                    school_config=req.school_config,
                ),
                timeout=90,
            )
        except asyncio.TimeoutError:
            app_state.update_backtest_task(task_id, {
                "status": "failed",
                "error": ("回测超时（>90秒）。策略可能在 populate_entry/exit_signals 里做了低效的全量重算"
                          "（如每根bar都遍历 close_arr 重算指标）。请用 AI 助手优化：把指标计算放进 "
                          "populate_indicators，信号方法里用 ctx.mas / ctx.factor_values 读取，"
                          "不要在信号方法里遍历历史重算。"),
            })
            return

        app_state.update_backtest_task(task_id, {
            "status": "completed",
            "completed_at": datetime.now().isoformat(),
            "result": result,
        })
    except Exception as e:
        app_state.update_backtest_task(task_id, {
            "status": "failed",
            "error": str(e),
        })


@router.get("/{task_id}/status")
async def get_backtest_status(task_id: str):
    """Poll backtest progress."""
    task = app_state.get_backtest_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    resp = {
        "task_id": task_id,
        "status": task["status"],
        "started_at": task.get("started_at"),
    }
    if task["status"] == "completed":
        resp["metrics"] = task.get("result", {}).get("metrics", {})
        resp["trade_count"] = len(task.get("result", {}).get("trades", []))
        resp["signal_count"] = len(task.get("result", {}).get("signals", []))
    elif task["status"] == "failed":
        resp["error"] = task.get("error")
    return resp


@router.get("/{task_id}/result")
async def get_backtest_result(task_id: str):
    """Get full backtest result."""
    task = app_state.get_backtest_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Task is {task['status']}")

    result = task["result"]
    return {
        "task_id": task_id,
        "status": "completed",
        "metrics": result.get("metrics", {}),
        "trades": result.get("trades", []),
        "equity_curve": result.get("equity_curve", []),
        "signals": result.get("signals", []),
    }
