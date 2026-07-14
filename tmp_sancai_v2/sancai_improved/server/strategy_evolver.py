"""策略蒸馏进化引擎 — 每个流派独立反复蒸馏，直到成为高胜率大概率事件。

通过 Claude/DeepSeek 分析跨股票回测聚合结果，逐代改写策略逻辑。
每代独立保存，可随时对比各代表现。

Usage:
    evolver = StrategyEvolver()
    gen2 = await evolver.distill_school("chan_theory", symbols=["000001","600519"],
                                         period="daily", generations=3)
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent

# ── 流派原始代码路径映射 ──
SCHOOL_CODE_MAP = {
    "chan_theory": "backtest/schools/chan_theory.py",
    "ict": "backtest/schools/ict.py",
    "price_action": "backtest/schools/price_action.py",
    "wyckoff": "backtest/schools/wyckoff.py",
    "morphology": "backtest/schools/morphology.py",
    "gann": "backtest/schools/gann.py",
    "wave_theory": "backtest/schools/wave_theory.py",
    "dow_theory": "backtest/schools/dow_theory.py",
    "simple": "backtest/strategies/simple_kdj.py",
}

SCHOOL_NAMES = {
    "chan_theory": "缠论", "ict": "ICT", "price_action": "价格行为",
    "wyckoff": "威科夫", "morphology": "形态学", "gann": "江恩",
    "wave_theory": "波浪", "dow_theory": "道氏", "simple": "KDJ超卖",
}


@dataclass
class GenerationResult:
    """一代策略的多股票回测聚合结果."""
    school: str = ""
    generation: int = 1
    strategy_name: str = ""
    symbols_tested: int = 0
    avg_win_rate: float = 0.0
    avg_return: float = 0.0
    avg_sharpe: float = 0.0
    avg_profit_factor: float = 0.0   # 新增：盈亏比
    max_drawdown: float = 0.0         # 新增：最大回撤（取各股票中最严重的）
    total_trades: int = 0             # 新增：总交易数
    per_symbol: list = field(default_factory=list)
    passed: bool = False  # 综合分 >= 60（改为多维度判断）
    ai_changes: str = ""
    composite_score: float = 0.0      # 新增：综合评分
    ts: str = ""
    trade_analyses: list = field(default_factory=list)  # 新增：TradeAnalysis结果

    def to_dict(self) -> dict:
        return {
            "school": self.school, "generation": self.generation,
            "strategy_name": self.strategy_name, "symbols_tested": self.symbols_tested,
            "avg_win_rate": round(self.avg_win_rate, 2),
            "avg_return": round(self.avg_return, 2),
            "avg_sharpe": round(self.avg_sharpe, 2),
            "avg_profit_factor": round(self.avg_profit_factor, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "total_trades": self.total_trades,
            "composite_score": round(self.composite_score, 2),
            "per_symbol": self.per_symbol, "passed": self.passed,
            "ai_changes": self.ai_changes, "ts": self.ts,
        }


class StrategyEvolver:
    """策略蒸馏进化器 — 每个流派独立多代进化."""

    def __init__(self):
        self.generation_log: dict[str, list[GenerationResult]] = {}  # school -> [gen1, gen2, ...]
        self._progress: dict = {"running": False, "current_school": "",
                                "current_gen": 0, "log": []}

    async def distill_all(
        self, symbols: list[str], period: str = "daily",
        start_date: str = None, end_date: str = None,
        generations: int = 3, deadline: str = None
    ) -> dict:
        """对8大流派逐一代蒸馏进化。

        Args:
            deadline: ISO datetime string，超过此时间自动结束蒸馏

        Returns:
            {school_name: [GenerationResult, ...], "summary": {...}}
        """
        self._progress = {"running": True, "current_school": "", "current_gen": 0, "log": []}
        self._deadline = deadline
        all_results = {}
        terminated_early = False

        for school in SCHOOL_CODE_MAP:
            # 截止时间检查
            if self._deadline and self._is_past_deadline():
                self._progress["log"].append(f"⏰ 到达截止时间，跳过剩余流派")
                terminated_early = True
                break

            self._progress["current_school"] = school
            gen_results = await self.distill_school(
                school, symbols, period, start_date, end_date, generations
            )
            all_results[school] = [g.to_dict() for g in gen_results]
            self._progress["log"].append(f"✓ {SCHOOL_NAMES.get(school, school)}: "
                                         f"{len(gen_results)}代完成")

        self._progress["running"] = False

        if terminated_early:
            self._progress["log"].append("⏰ 蒸馏提前结束（已达截止时间）")

        # 汇总
        total_passed = sum(1 for school_gens in all_results.values()
                          for g in school_gens if g.get("passed") if isinstance(g, dict))
        return {
            "schools": all_results,
            "summary": {
                "total_schools": len(all_results),
                "total_generations": sum(len(g) for g in all_results.values()),
                "passed_count": self._count_passed(all_results),
                "terminated_early": terminated_early,
                "ts": datetime.now().isoformat(),
            },
            "progress_log": self._progress["log"],
        }

    def _is_past_deadline(self) -> bool:
        """检查是否已过截止时间."""
        if not self._deadline:
            return False
        try:
            deadline_dt = datetime.fromisoformat(self._deadline)
            return datetime.now() >= deadline_dt
        except (ValueError, TypeError):
            return False

    def _count_passed(self, results: dict) -> int:
        count = 0
        for school_gens in results.values():
            for g in school_gens:
                if isinstance(g, dict) and g.get("passed"):
                    count += 1
        return count

    async def distill_school(
        self, school: str, symbols: list[str], period: str = "daily",
        start_date: str = None, end_date: str = None, generations: int = 3,
        ml_context: dict = None
    ) -> list[GenerationResult]:
        """单流派蒸馏：逐代回测→AI改写→再回测，多代迭代。

        ml_context: ML注入上下文 {"top_factors": [...], "advice": "..."} 可选

        Returns list of GenerationResult, one per generation.
        """
        gen_results = []
        current_strategy = school  # 第一代用原始代码
        current_gen = 1

        # ── 第1代：原始策略回测 ──
        self._progress["current_gen"] = 1
        gen1 = await self._test_generation(current_strategy, school, 1, symbols,
                                           period, start_date, end_date)
        gen_results.append(gen1)

        # 备份第1代
        self._backup_strategy(current_strategy, school, 1, gen1)

        if gen1.passed:
            self.generation_log[school] = gen_results
            return gen_results

        # ── 第2~N代：AI蒸馏 ──
        for gen in range(2, generations + 1):
            # 截止时间检查（每代都检查）
            if self._deadline and self._is_past_deadline():
                self._progress["log"].append(f"⏰ 到达截止时间，{school} 停止于 Gen{gen-1}")
                break

            self._progress["current_gen"] = gen
            prev_gen = gen_results[-1]

            # ── 备份上一代代码 ──
            self._backup_strategy(current_strategy, school, gen - 1, prev_gen)

            # AI生成改进版
            new_name = f"{school}_gen{gen}"
            success = await self._ai_distill(
                current_strategy, school, new_name, prev_gen, symbols, ml_context
            )

            if not success:
                break

            # 回测验证新一代
            current_strategy = f"user_{new_name}"
            gen_result = await self._test_generation(
                current_strategy, school, gen, symbols,
                period, start_date, end_date
            )
            gen_result.strategy_name = new_name
            gen_result.ai_changes = f"AI蒸馏第{gen}代"

            # ── 门控：只保留胜率提升的 ──
            kept = self._check_and_promote(new_name, prev_gen, gen_result)
            if not kept:
                # 删除 _pending，不记录这代
                break

            gen_results.append(gen_result)

            if gen_result.passed:
                break

        self.generation_log[school] = gen_results
        return gen_results

    async def _test_generation(
        self, mode: str, school: str, gen: int, symbols: list[str],
        period: str, start_date: str, end_date: str
    ) -> GenerationResult:
        """跑一代策略的所有股票回测，聚合结果（含 TradeAnalyzer 深度分析）."""
        from backtest.batch_runner import BatchRunner
        from backtest.trade_analyzer import TradeAnalyzer

        runner = BatchRunner()
        result = GenerationResult(
            school=school, generation=gen, strategy_name=mode,
            symbols_tested=len(symbols), ts=datetime.now().isoformat(),
        )

        total_wr = 0.0
        total_ret = 0.0
        total_sharpe = 0.0
        total_pf = 0.0
        total_trades = 0
        worst_dd = 0.0  # 最差回撤
        valid = 0

        for sym in symbols:
            try:
                sr = runner.run_single(sym, mode, period, start_date, end_date)
                if sr.error:
                    result.per_symbol.append({"symbol": sym, "error": sr.error})
                    continue

                result.per_symbol.append({
                    "symbol": sym, "win_rate": sr.win_rate,
                    "total_return": sr.total_return, "sharpe": sr.sharpe,
                    "profit_factor": sr.profit_factor, "max_drawdown": sr.max_drawdown,
                    "trades": sr.total_trades, "weak": sr.weak,
                })
                total_wr += sr.win_rate
                total_ret += sr.total_return
                total_sharpe += sr.sharpe
                total_pf += sr.profit_factor
                total_trades += sr.total_trades
                if abs(sr.max_drawdown) > abs(worst_dd):
                    worst_dd = sr.max_drawdown
                valid += 1

                # ── 逐笔交易深度分析（仅对最少 3 笔交易的股票）──
                if sr.total_trades >= 3 and sr.trades:
                    try:
                        analyzer = TradeAnalyzer()
                        ta = analyzer.analyze(sr.trades, symbol=sym, school=school)
                        if ta.total_trades > 0:
                            result.trade_analyses.append(ta.to_dict())
                    except Exception as e:
                        logger.warning(f"TradeAnalyzer failed for {sym}: {e}")

            except Exception as e:
                result.per_symbol.append({"symbol": sym, "error": str(e)})

        if valid > 0:
            result.avg_win_rate = round(total_wr / valid, 2)
            result.avg_return = round(total_ret / valid, 2)
            result.avg_sharpe = round(total_sharpe / valid, 2)
            result.avg_profit_factor = round(total_pf / valid, 2)
            result.max_drawdown = round(worst_dd, 2)
            result.total_trades = total_trades

        # ── 多维度评分 ──
        from server.evolution_scorer import EvolutionScorer
        scorer = EvolutionScorer()
        result.composite_score = scorer.score(
            result.avg_win_rate, result.avg_return,
            result.avg_profit_factor, result.avg_sharpe,
            result.max_drawdown, result.total_trades,
        )

        # 改为综合分 >= 60（而非仅胜率 >= 60）
        result.passed = result.composite_score >= 60

        return result

    def _backup_strategy(self, mode: str, school: str, gen: int,
                         prev_result: GenerationResult):
        """备份一代策略的原始代码和触发条件，防止迭代丢失."""
        backup_dir = PROJECT_ROOT / "backtest" / "strategies" / "_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{school}_gen{gen}_{ts}"

        # 复制代码
        code = self._read_strategy_code(mode)
        if code:
            (backup_dir / f"{backup_name}.py").write_text(code, encoding="utf-8")

        # 记录触发条件和回测结果
        info = {
            "school": school, "generation": gen,
            "strategy_name": mode, "backup_ts": ts,
            "avg_win_rate": prev_result.avg_win_rate,
            "avg_return": prev_result.avg_return,
            "per_symbol": prev_result.per_symbol,
            "passed": prev_result.passed,
        }
        import json
        (backup_dir / f"{backup_name}.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Cap disk usage: keep only the last 20 backups per school (.py + .json)
        for suffix in ("py", "json"):
            backups = sorted(backup_dir.glob(f"{school}_gen*.{suffix}"))
            for old in backups[:-20]:
                old.unlink(missing_ok=True)
        logger.info(f"Backed up {mode} (gen {gen}): {backup_name}")

    def _read_strategy_code(self, mode: str) -> Optional[str]:
        """读取策略源代码."""
        # 内置流派路径
        if mode in SCHOOL_CODE_MAP:
            path = PROJECT_ROOT / SCHOOL_CODE_MAP[mode]
            if path.exists():
                return path.read_text(encoding="utf-8")

        # 策略标准路径
        for fmt in ["backtest/strategies/{}.py",
                     "backtest/schools/{}.py",
                     "backtest/strategies/user_generated/{}.py"]:
            path = PROJECT_ROOT / fmt.format(mode)
            if path.exists():
                return path.read_text(encoding="utf-8")

        # 用户策略（带user_前缀的去掉重试）
        if mode.startswith("user_"):
            bare = mode[5:]
            path = PROJECT_ROOT / f"backtest/strategies/user_generated/{bare}.py"
            if path.exists():
                return path.read_text(encoding="utf-8")

        return None

    async def _ai_distill(
        self, current_mode: str, school: str, new_name: str,
        prev_gen: GenerationResult, symbols: list[str],
        ml_context: dict = None
    ) -> bool:
        """AI蒸馏一步：读取上一代结果+代码→增强分析(可选ML注入)→生成改进版→保存."""
        from server.routers.ai_chat import _get_api_key, _call_llm, _validate_strategy_code
        from server.enhanced_prompt import DistillPromptBuilder

        api_key = _get_api_key()
        if not api_key:
            logger.warning("No API key for distillation")
            return False

        # 读原始代码
        original_code = ""
        original_path = PROJECT_ROOT / SCHOOL_CODE_MAP.get(school, "")
        if original_path.exists():
            original_code = original_path.read_text(encoding="utf-8")
        else:
            # 读上一代
            last_path = PROJECT_ROOT / "backtest" / "strategies" / "user_generated" / f"{school}_gen{prev_gen.generation}.py"
            if last_path.exists():
                original_code = last_path.read_text(encoding="utf-8")

        if not original_code:
            logger.warning(f"No source code found for {school}")
            return False

        # ── 使用增强 Prompt 构建器 ──
        builder = DistillPromptBuilder()
        system_prompt, user_prompt = builder.build(
            school, prev_gen, original_code, symbols, ml_context
        )

        try:
            code, _ = await _call_llm(user_prompt, system_prompt, max_tokens=4000)
            code = code.strip()
            if code.startswith("```"):
                lines = code.split("\n")
                if lines[0].startswith("```"): lines = lines[1:]
                if lines and lines[-1].startswith("```"): lines = lines[:-1]
                code = "\n".join(lines)

            validation = _validate_strategy_code(code, new_name)
            if not validation["valid"]:
                logger.warning(f"Distill validation failed: {validation['errors']}")
                # Auto-fix: ensure import is correct
                code = code.replace("from .interface import", "from ..interface import")
                validation = _validate_strategy_code(code, new_name)
                if not validation["valid"]:
                    return False

            # 直接保存到 user_generated/（立即生效，不再需要 _pending 审核流程）
            from backtest.strategies.registry import register_user_strategy
            register_user_strategy(new_name, code)
            logger.info(f"Evolution output saved and registered: {new_name}")

            return True
        except Exception as e:
            logger.error(f"AI distill error: {e}")
            return False

    def _check_and_promote(self, strategy_name: str, prev_gen: GenerationResult,
                           new_gen: GenerationResult) -> bool:
        """多维度智能门控：用 SmartGate 替代单一胜率判断.

        Returns True if kept (new better than old), False if discarded.
        """
        from server.smart_gate import SmartGate

        gate = SmartGate()
        decision = gate.evaluate(prev_gen, new_gen)

        user_dir = PROJECT_ROOT / "backtest" / "strategies" / "user_generated"
        py_src = user_dir / f"{strategy_name}.py"

        if decision["keep"]:
            # Strategy already in user_generated, just log
            logger.info(
                f"Strategy {strategy_name} kept by SmartGate: "
                f"score {decision['score']['old_score']:.1f} -> {decision['score']['new_score']:.1f} "
                f"(+{decision['score']['improvement']:.1f})"
            )
            return True
        else:
            # Delete the underperforming evolution
            if py_src.exists():
                py_src.unlink()
                # Also remove from registry
                from backtest.strategies.registry import _registry
                key = f"user_{strategy_name}"
                if key in _registry:
                    del _registry[key]
            logger.info(
                f"Strategy {strategy_name} discarded by SmartGate: {decision['reason']}"
            )
            return False

    @staticmethod
    def approve_strategy(strategy_name: str) -> dict:
        """用户确认升级：策略已在 user_generated，只需热加载."""
        from backtest.strategies.registry import _discover_user_strategies
        _discover_user_strategies()
        logger.info(f"Strategy approved (re-registered): {strategy_name}")
        return {"status": "ok", "message": f"策略 {strategy_name} 已激活", "name": strategy_name}

    @staticmethod
    def reject_strategy(strategy_name: str) -> dict:
        """用户拒绝升级：从 user_generated 删除."""
        user_dir = PROJECT_ROOT / "backtest" / "strategies" / "user_generated"
        py_src = user_dir / f"{strategy_name}.py"

        if py_src.exists():
            py_src.unlink()
            from backtest.strategies.registry import _registry
            key = f"user_{strategy_name}"
            if key in _registry:
                del _registry[key]

        logger.info(f"Strategy rejected and deleted: {strategy_name}")
        return {"status": "ok", "message": f"策略 {strategy_name} 已删除", "name": strategy_name}

    @staticmethod
    def list_pending() -> list[dict]:
        """列出所有进化产出策略（从 user_generated 扫描 gen 后缀文件）."""
        user_dir = PROJECT_ROOT / "backtest" / "strategies" / "user_generated"
        if not user_dir.exists():
            return []
        results = []
        for f in sorted(user_dir.glob("*_gen*.py")):
            try:
                results.append({
                    "strategy_name": f.stem,
                    "status": "active",
                    "file": str(f.name),
                })
            except:
                pass
        return results

    def get_status(self) -> dict:
        """获取蒸馏进度."""
        return self._progress

    def get_generations(self) -> dict:
        """获取所有流派的各代结果."""
        result = {}
        for school, gens in self.generation_log.items():
            result[school] = {
                "name": SCHOOL_NAMES.get(school, school),
                "generations": [g.to_dict() for g in gens],
            }
        return result
