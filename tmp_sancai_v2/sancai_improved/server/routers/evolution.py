"""Evolution API — ML 框架 / 因子 IC / 策略蒸馏 / 自主循环控制 + 系统资源监控。

端点的实际逻辑都在现有模块里(StrategyEvolver/AutoLoopRunner/BatchRunner 等);
本路由只负责暴露 HTTP 端点 + 串联参数。
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from server.utils.response import ok

logger = logging.getLogger(__name__)
router = APIRouter()

PROJECT_ROOT = Path(__file__).parent.parent.parent
MODELS_DIR = PROJECT_ROOT / "data" / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════
# Request models
# ═══════════════════════════════════════════════════════════════════

class TrainRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=200, description="训练用的股票代码")
    factor_names: list[str] | None = None
    label_horizon: int = Field(5, ge=1, le=20, description="标签前瞻窗口(bars)")
    window_size: int = Field(252, ge=60, le=500, description="滚动训练窗口(bars)")
    task: str = Field("regression", pattern="^(regression|classification)$")
    device: str = Field("gpu", pattern="^(cpu|gpu)$", description="gpu 用 GPU 加速, cpu 用 CPU")
    save_model: bool = Field(True, description="训练完后是否保存模型到 data/models/")


class EvolveRequest(BaseModel):
    mode: str = Field(..., description="策略模式(simple/strict/chan_theory/... 或 user_xxx)")
    symbols: list[str] = Field(..., min_length=1, max_length=100)
    generations: int = Field(2, ge=1, le=5, description="蒸馏代数")
    inject_ml: bool = Field(True, description="是否注入 ML 特征重要性到 AI prompt")


class AutoLoopRequest(BaseModel):
    symbol_count: int = Field(10, ge=5, le=100, description="每周期随机抽选股票数")
    generations: int = Field(2, ge=1, le=5, description="每流派蒸馏代数")
    deadline_minutes: int = Field(60, ge=5, le=480, description="总运行时限(分钟)")
    modes: list[str] | None = Field(None, description="要进化的流派列表, None=全部8个")
    inject_ml: bool = Field(True, description="是否注入 ML 特征到 AI prompt")


# ═══════════════════════════════════════════════════════════════════
# ML 训练端点
# ═══════════════════════════════════════════════════════════════════

@router.post("/evolution/train")
async def train_model(req: TrainRequest):
    """全批次股票 → 因子矩阵 → LightGBM 滚动训练 → 模型+IC 报告。

    训练完后模型保存在 data/models/lightgbm_{ts}.txt (LightGBM 原生格式)，
    可通过 /evolution/model/predict 加载预测，或 /evolution/factors/ic 看因子排名。
    """
    try:
        from server.services.training_data import build_batch_dataset
        from research.models import LightGBMModel, RollingTrainer

        # 1. 构建训练数据集
        df = build_batch_dataset(req.symbols, factor_names=req.factor_names,
                                 label_horizon=req.label_horizon)
        if df.empty or len(df) < 100:
            raise HTTPException(status_code=400, detail="训练数据不足(<100行)。增加股票数或检查数据文件。")

        feat_cols = [c for c in df.columns if c not in ("symbol", "date", "label")]

        # 2. 滚动训练
        model = LightGBMModel(task=req.task, device=req.device)
        trainer = RollingTrainer(model, window_size=req.window_size,
                                 retrain_freq=20, forward_periods=req.label_horizon)
        result = trainer.run(df, feature_cols=feat_cols, target_col="label")

        # 3. 特征重要性（最佳模型一轮）
        X = df[feat_cols].values.astype("float64")
        y = df["label"].values.astype("float64")
        mask = ~np.any(np.isnan(X), axis=1) & ~np.isnan(y)
        X_clean, y_clean = X[mask], y[mask]
        importances = {}
        if len(X_clean) >= 60:
            import numpy as np
            try:
                model.fit(X_clean, y_clean)
                imp = getattr(model, "feature_importances_", None)
                if hasattr(imp, "__call__"):
                    imp = imp()
                if imp is not None and len(imp) == len(feat_cols):
                    importances = {feat_cols[i]: round(float(imp[i]), 6) for i in range(len(feat_cols))}
            except Exception:
                pass

        # 4. 保存模型
        model_path = ""
        if req.save_model and model._model is not None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            model_path = str(MODELS_DIR / f"lightgbm_{ts}.txt")
            import lightgbm as lgb
            import numpy as np
            try:
                model._model.booster_.save_model(model_path)
            except Exception:
                pass

        return ok({
            "mean_ic": round(result.mean_ic, 4),
            "ic_ir": round(result.ic_ir, 4),
            "overall_ic": round(result.overall_ic, 4),
            "n_windows": len(result.results),
            "n_features": len(feat_cols),
            "n_rows": int(len(df)),
            "feature_importances": importances,
            "model_path": model_path,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("train_model failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/evolution/factors/ic")
async def get_factor_ic(symbols: str = Query("000001,600519,000858", description="逗号分隔股票列表"),
                        factor_names: str = Query(None, description="逗号分隔因子名")):
    """获取因子滚动窗口 IC 排名。进化 prompt 注入用。"""
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    fnames = [f.strip() for f in factor_names.split(",") if f.strip()] if factor_names else None
    try:
        from server.services.training_data import compute_factor_ic
        ranked = compute_factor_ic(syms, factor_names=fnames)
        return ok({"factors": ranked, "n_stocks": len(syms)})
    except Exception as e:
        logger.error("factor IC failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/evolution/model/predict")
async def model_predict(symbols: list[str] = Query(...),
                        model_file: str = Query(None, description="模型文件路径, 默认用最新")):
    """加载训练好的 LightGBM 模型, 对给定标的的最后一根 bar 打分(预测未来收益)。"""
    import numpy as np
    import pandas as pd
    import lightgbm as lgb

    # 模型文件
    import glob
    import numpy
    if not model_file:
        candidates = sorted(MODELS_DIR.glob("lightgbm_*.txt"), reverse=True)
        if not candidates:
            raise HTTPException(status_code=404, detail="无已训练的模型文件。请先调用 /evolution/train")
        model_file = str(candidates[0])

    booster = lgb.Booster(model_file=model_file)
    feat_names = booster.feature_name()
    if not feat_names:
        raise HTTPException(status_code=500, detail="模型文件不含特征名列, 无法预测")

    from server.services.training_data import build_signals_dataset
    results = []
    for sym in symbols:
        df = build_signals_dataset(sym, factor_names=list(feat_names), label_horizon=1)
        if df is None:
            continue
        latest = df[feat_names].iloc[-1:].values.astype("float64")
        pred = float(booster.predict(latest)[0])
        results.append({"symbol": sym, "score": round(pred, 6),
                        "direction": "up" if pred > 0 else "down"})

    results.sort(key=lambda x: -x["score"])
    return ok({"model": Path(model_file).name, "predictions": results})


# ═══════════════════════════════════════════════════════════════════
# 策略蒸馏进化端点
# ═══════════════════════════════════════════════════════════════════

@router.post("/evolution/evolve/{mode}")
async def evolve_strategy(mode: str, req: EvolveRequest):
    """对单个流派/策略触发 AI 蒸馏进化(含可选 ML 特征注入)。

    调用 StrategyEvolver.distill_school() → BatchRunner 回测 → AI 生成改进策略 →
    SmartGate 评分淘汰 → 通过的新策略进 user_generated。
    """
    try:
        from server.strategy_evolver import StrategyEvolver

        evolver = StrategyEvolver()
        ml_context = None
        if req.inject_ml:
            # 获取 ML 因子重要性作为 prompt 注入
            try:
                from server.services.training_data import compute_factor_ic
                ranked = compute_factor_ic(req.symbols[:30])
                if ranked:
                    top5 = ranked[:5]
                    ml_context = {
                        "top_factors": [f"{r['factor_name']}(IC={r['mean_ic']})" for r in top5],
                        "advice": f"当前市场最有解释力的因子: {', '.join(f['factor_name'] for f in top5)}。"
                                  "请优先参考这些因子构造策略逻辑。",
                    }
            except Exception:
                pass

        # 异步跑蒸馏（to_thread 避免阻塞）
        result = await asyncio.to_thread(
            evolver.distill_school,
            school=mode,
            symbols=req.symbols,
            generations=req.generations,
            ml_context=ml_context,
        )
        return ok({
            "mode": mode,
            "generations": result.get("generations", []),
            "best_composite": result.get("best_composite"),
            "strategy_saved": result.get("strategy_saved", False),
        })

    except Exception as e:
        logger.error("evolve %s failed: %s", mode, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════
# AutoLoop 控制端点
# ═══════════════════════════════════════════════════════════════════

_auto_loop_task: Optional[asyncio.Task] = None


@router.post("/evolution/auto-loop/start")
async def start_auto_loop(req: AutoLoopRequest):
    """启动自主进化循环: 随机抽股 → 批量回测 → 蒸馏进化 → 重复直到时限。"""
    global _auto_loop_task
    from server.auto_loop import AutoLoopRunner

    if _auto_loop_task and not _auto_loop_task.done():
        raise HTTPException(status_code=409, detail="AutoLoop 已在运行中")

    runner = AutoLoopRunner()
    deadline = datetime.now().isoformat()

    async def _run():
        try:
            runner.start(
                symbol_count=req.symbol_count,
                generations=req.generations,
                deadline=deadline,
                modes=req.modes,
                inject_ml=req.inject_ml,
            )
        except Exception as e:
            logger.error("AutoLoop crashed: %s", e, exc_info=True)

    _auto_loop_task = asyncio.create_task(_run())
    return ok({"status": "started", "deadline": deadline})


@router.post("/evolution/auto-loop/stop")
async def stop_auto_loop():
    """请求停止自主进化循环(当前周期完成后停止)。"""
    global _auto_loop_task
    from server.auto_loop import get_loop_runner

    runner = get_loop_runner()
    if runner is not None:
        runner.request_stop()
    return ok({"status": "stopping"})


@router.get("/evolution/auto-loop/status")
async def auto_loop_status():
    """轮询自主进化循环进度。"""
    from server.auto_loop import get_loop_runner

    runner = get_loop_runner()
    if runner is None:
        return ok({"running": False, "phase": "idle"})

    status = runner.get_status()
    return ok(status)


@router.get("/evolution/auto-loop/report")
async def auto_loop_report():
    """下载最新自主进化循环 Markdown 报告。"""
    import glob as _glob
    candidates = sorted(Path(PROJECT_ROOT / "reports").glob("auto_loop_report_*.md"), reverse=True)
    if not candidates:
        raise HTTPException(status_code=404, detail="无报告。请先运行 auto-loop")
    return ok({"report": candidates[0].read_text(encoding="utf-8"),
               "path": str(candidates[0])})


# ═══════════════════════════════════════════════════════════════════
# 系统资源监控
# ═══════════════════════════════════════════════════════════════════

@router.get("/evolution/system-stats")
async def system_stats():
    """实时 CPU / 内存 / GPU 使用率 + 进程信息。前端 3 秒轮询。"""
    import psutil
    gpu = {}
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        gpu = {
            "name": str(pynvml.nvmlDeviceGetName(h)),
            "mem_total_mb": round(mem.total / (1024 * 1024), 1),
            "mem_used_mb": round(mem.used / (1024 * 1024), 1),
            "mem_pct": round(mem.used / max(mem.total, 1) * 100, 1),
            "gpu_util_pct": pynvml.nvmlDeviceGetUtilizationRates(h).gpu,
            "temp_c": pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU),
        }
        pynvml.nvmlShutdown()
    except Exception:
        pass  # 无 GPU 或无 pynvml → gpu 为空

    vm = psutil.virtual_memory()
    proc = psutil.Process(os.getpid())
    return ok({
        "cpu_pct": round(psutil.cpu_percent(interval=0.3), 1),
        "mem_total_gb": round(vm.total / (1024 ** 3), 1),
        "mem_used_gb": round(vm.used / (1024 ** 3), 1),
        "mem_pct": round(vm.percent, 1),
        "gpu": gpu,
        "process": {
            "pid": os.getpid(),
            "threads": proc.num_threads(),
            "cpu_pct": round(proc.cpu_percent(interval=None), 1),
            "mem_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
        },
        "ts": datetime.now().isoformat(),
    })
