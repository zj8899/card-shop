"""
Enhanced Distill Prompt — 基于 TradeAnalyzer 分析结果的增强蒸馏 Prompt 构建器

将原有的一行"胜率45%，请改进"替换为包含以下信息的结构化 Prompt：
  1. 策略 DNA（流派核心理念 + 当前逻辑摘要）
  2. 回测多维度概览（胜率/收益率/盈亏比/夏普/回撤）
  3. 失败模式深度分析（每模式 2-3 个典型案例）
  4. 成功模式（盈利交易的共同特征）
  5. 市场环境表现对比（牛/熊/震荡市各表现）
  6. 具体的改进清单

用法:
    builder = DistillPromptBuilder()
    prompt = builder.build(school, prev_gen, original_code, symbols)
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 流派介绍（给 AI 的策略背景）
SCHOOL_INTRO = {
    "chan_theory": (
        "缠论（缠中说禅）核心：通过 K 线包含处理 → 笔 → 线段 → 中枢 → 背驰的层级结构，"
        "识别趋势转折点。三类买卖点：一买（下跌背驰后）、二买（回踩中枢不破）、"
        "三买（突破中枢回抽）。出场为对应的一/二/三类卖点。"
    ),
    "ict": (
        "ICT（Inner Circle Trader）核心：基于市场结构和流动性概念的短线交易。"
        "关键信号：FVG（公允价值缺口）回补、OB（订单块）支撑/阻力、"
        "Liquidity Grab（流动性猎杀）。出场为反方向 FVG/OB 触及。"
    ),
    "price_action": (
        "价格行为核心：基于K线形态和支撑阻力位。关键信号：Pin Bar 反转、"
        "Inside Bar 突破、Engulfing 吞没、支撑/阻力位反弹。"
        "出场为到达对侧支撑/阻力位或形态失败。"
    ),
    "wyckoff": (
        "威科夫核心：市场操纵周期（累积→上涨→派发→下跌）。"
        "买入信号：Spring/Upthrust（弹簧/上冲）、SOS（强势信号）、"
        "LPS（最后支撑点）。卖出信号：SOW（弱势信号）、UTAD（顶部上冲后派发）。"
    ),
    "morphology": (
        "形态学核心：经典图表形态识别。买入信号：双重底/W底、头肩底、"
        "杯柄形态、三角形突破上沿。卖出信号：双重顶/M顶、头肩顶、"
        "下降三角形跌破、楔形反转。"
    ),
    "gann": (
        "江恩理论核心：基于几何角度线和八分位价格分割。买入信号：价格触及"
        "1/8或2/8支撑位反弹、时间周期共振。卖出信号：价格到达7/8或8/8阻力位、"
        "江恩扇形角度线压制。"
    ),
    "wave_theory": (
        "波浪理论核心：艾略特五浪推进+三浪调整。买入信号：调整浪（第2/4浪）结束、"
        "新的推动浪（第1/3/5浪）起点确认。卖出信号：第5浪顶部衰竭、"
        "ABC调整确认。"
    ),
    "dow_theory": (
        "道氏理论核心：趋势确认与反转。买入信号：Higher Low + Higher High 确认上升趋势、"
        "回调不破前低。卖出信号：Lower High + Lower Low 确认下降趋势、"
        "反弹不过前高。"
    ),
    "simple": (
        "KDJ超卖反弹策略：KDJ_K<35超卖区金叉+价格在MA34上方时买入。"
        "基础均值回归策略，出场为KDJ超买或跟踪止损。"
    ),
}


class DistillPromptBuilder:
    """构建增强蒸馏 Prompt."""

    def __init__(self):
        self._school_intro = SCHOOL_INTRO

    def build(self, school: str, prev_gen, original_code: str,
              symbols: list[str], ml_context: dict = None) -> tuple[str, str]:
        """构建增强蒸馏的 system_prompt 和 user_prompt.

        Args:
            school: 流派名
            prev_gen: GenerationResult — 上一代结果
            original_code: 原始策略源代码
            symbols: 股票列表
            ml_context: 可选 ML 注入 {"top_factors": [...], "advice": "..."}

        Returns:
            (system_prompt, user_prompt)
        """
        system_prompt = self._build_system(school, prev_gen, ml_context)
        user_prompt = self._build_user(school, prev_gen, original_code, symbols, ml_context)
        return system_prompt, user_prompt

    def _build_system(self, school: str, prev_gen, ml_context: dict = None) -> str:
        """构建 system prompt（含可选的 ML 因子注入）。"""
        school_name = SCHOOL_INTRO.get(school, "量化交易策略")
        if school in {"simple", "schools", "strict", "strict_reverse"}:
            school_name = ""

        parts = [
            "你是一位专业的量化策略「蒸馏」专家。你的任务是对一个回测表现不佳的策略进行逐代改进。",
            "",
            "核心原则:",
            "1. 每次只修改1-2个核心逻辑，不要推翻重写整个策略",
            "2. 修改必须有明确的数据依据（基于提供的失败模式分析）",
            "3. 优先修复失败概率最高的模式，其次强化成功率最高的模式",
            "4. 保持代码结构不变（继承 IStrategy，保留 populate_indicators/entry_signals/exit_signals）",
            "5. 在代码注释中标注每个修改对应的失败模式编号",
        ]

        # ML 因子注入（影响 system prompt 的策略指导）
        if ml_context and ml_context.get("top_factors"):
            top = ml_context["top_factors"]
            advice = ml_context.get("advice", "")
            parts.extend([
                "",
                "⚡ ML 因子市场适配指导（基于 LightGBM 滚动训练的特征重要性）:",
                f"当前市场环境下最有解释力的因子(按 IC 降序): {', '.join(top[:5])}",
                f"策略建议: {advice}" if advice else f"请在策略中优先参考上述因子构造买卖信号。",
                "指标计算请放进 populate_indicators（向量化一次），信号方法里用 ctx.mas/ctx.factor_values 读取值。",
            ])

        if school_name:
            parts.insert(1, f"策略流派背景: {school_name}")

        return "\n".join(parts)

    def _build_user(self, school: str, prev_gen, original_code: str,
                    symbols: list[str], ml_context: dict = None) -> str:
        """构建 user prompt — 包含完整的分析数据."""
        lines = []

        # ═══ SECTION 1: 策略 DNA ═══
        school_desc = SCHOOL_INTRO.get(school, "")
        lines.append("=" * 50)
        lines.append("SECTION 1: 策略 DNA")
        lines.append(f"流派: {school}")
        if school_desc:
            lines.append(f"核心理念: {school_desc}")
        lines.append(f"当前代码名: {prev_gen.strategy_name}")
        lines.append(f"覆盖股票: {', '.join(symbols[:8])}{'...' if len(symbols) > 8 else ''}")
        lines.append("")

        # ═══ SECTION 2: 回测多维度概览 ═══
        lines.append("=" * 50)
        lines.append("SECTION 2: 回测多维度概览")
        lines.append(f"  综合评分: {prev_gen.composite_score:.1f}/100")
        lines.append(f"  胜率: {prev_gen.avg_win_rate:.1f}%")
        lines.append(f"  收益率: {prev_gen.avg_return:+.1f}%")
        lines.append(f"  盈亏比: {prev_gen.avg_profit_factor:.2f}")
        lines.append(f"  夏普: {prev_gen.avg_sharpe:.2f}")
        lines.append(f"  最大回撤: {abs(prev_gen.max_drawdown):.1f}%")
        lines.append(f"  总交易次数: {prev_gen.total_trades}")
        lines.append(f"  测试股票数: {prev_gen.symbols_tested}")

        # 逐股票表现
        if prev_gen.per_symbol:
            lines.append("\n逐股票表现:")
            for s in prev_gen.per_symbol[:10]:
                if "error" in s:
                    lines.append(f"  {s['symbol']}: ERROR - {s['error'][:60]}")
                else:
                    lines.append(
                        f"  {s['symbol']}: "
                        f"胜率{s.get('win_rate',0):.1f}% "
                        f"收益{s.get('total_return',0):+.1f}% "
                        f"盈亏比{s.get('profit_factor',0):.2f} "
                        f"交易{s.get('trades',0)}笔"
                    )
        lines.append("")

        # ═══ SECTION 3: 失败模式深度分析 ═══
        lines.append("=" * 50)
        lines.append("SECTION 3: 失败模式深度分析")
        all_failures = []
        for ta in getattr(prev_gen, 'trade_analyses', []) or []:
            for fp in ta.get("failure_patterns", []):
                all_failures.append(fp)

        if all_failures:
            # 合并同类型失败模式
            merged = self._merge_failure_patterns(all_failures)
            for i, fp in enumerate(merged[:5], 1):
                lines.append(f"\n失败模式 #{i}: {fp['pattern']}")
                lines.append(f"  出现次数: {fp['count']}")
                lines.append(f"  平均亏损: {fp['avg_loss_pct']:.1f}%")
                lines.append(f"  总亏损: {fp.get('total_loss', 0):,.0f}")
                lines.append(f"  平均持仓: {fp.get('avg_bars_held', 0)}天")

                # 典型案例
                for j, ex in enumerate(fp.get("examples", [])[:2], 1):
                    pct = ex.get("pnl_pct", 0) * 100
                    lines.append(
                        f"  案例 {j}: {ex.get('entry_date','')}→{ex.get('exit_date','')} "
                        f"入场¥{ex.get('entry_price',0)}→出场¥{ex.get('exit_price',0)} "
                        f"({pct:+.1f}%/{ex.get('bars_held',0)}天)"
                    )
                    reason = ex.get("exit_reason", "")
                    if reason:
                        lines.append(f"    出场原因: {reason[:100]}")
        else:
            lines.append("\n(无 TradeAnalyzer 数据 — 交易次数不足或分析失败)")
        lines.append("")

        # ═══ SECTION 4: 成功模式 ═══
        lines.append("=" * 50)
        lines.append("SECTION 4: 成功模式")
        all_successes = []
        for ta in getattr(prev_gen, 'trade_analyses', []) or []:
            for sp in ta.get("success_patterns", []):
                all_successes.append(sp)

        if all_successes:
            merged = self._merge_success_patterns(all_successes)
            for i, sp in enumerate(merged[:3], 1):
                lines.append(f"\n成功模式 #{i}: {sp['pattern']}")
                lines.append(f"  出现次数: {sp['count']}")
                lines.append(f"  平均盈利: {sp['avg_win_pct']:+.1f}%")
                for j, ex in enumerate(sp.get("examples", [])[:1], 1):
                    pct = ex.get("pnl_pct", 0) * 100
                    lines.append(
                        f"  案例: {ex.get('entry_date','')}→{ex.get('exit_date','')} "
                        f"入场¥{ex.get('entry_price',0)}→出场¥{ex.get('exit_price',0)} "
                        f"({pct:+.1f}%/{ex.get('bars_held',0)}天)"
                    )
        else:
            lines.append("\n(无成功模式数据)")
        lines.append("")

        # ═══ SECTION 5: 市场环境 ═══
        lines.append("=" * 50)
        lines.append("SECTION 5: 市场环境表现")
        regime_found = False
        for ta in getattr(prev_gen, 'trade_analyses', []) or []:
            rp = ta.get("regime_perf", {})
            if rp and "note" not in rp:
                regime_found = True
                lines.append(f"股票 {ta.get('symbol','')}:")
                for reg, label in [("bull", "牛市"), ("bear", "熊市"), ("ranging", "震荡")]:
                    if reg in rp:
                        data = rp[reg]
                        lines.append(
                            f"  {label}: {data.get('trades',0)}笔 "
                            f"胜率{data.get('win_rate',0):.1f}% "
                            f"均收益{data.get('avg_return',0):+.2f}%"
                        )
        if not regime_found:
            lines.append("(无市场环境数据 — 需要更长的回测周期以计算MA34斜率)")
        lines.append("")

        # ═══ SECTION 6: 针对性的改进清单 ═══
        lines.append("=" * 50)
        lines.append("SECTION 6: 针对性改进清单")
        suggestions = self._generate_targeted_fixes(school, prev_gen)
        for i, s in enumerate(suggestions, 1):
            lines.append(f"  #{i}: {s}")
        lines.append("")

        # ═══ SECTION 6.5: ML 因子数据注入 (可选) ═══
        if ml_context and ml_context.get("top_factors"):
            lines.append("=" * 50)
            lines.append("SECTION 6.5: ML 因子适配数据(基于 LightGBM 滚动训练)")
            lines.append(f"当前市场 Top-5 有效因子(按 IC 降序):")
            for i, f in enumerate(ml_context["top_factors"][:5], 1):
                lines.append(f"  #{i}: {f}")
            if ml_context.get("advice"):
                lines.append(f"\n建议: {ml_context['advice']}")
            lines.append("请在策略改进时优先利用这些因子构造买卖信号。")
            lines.append("")

        # ═══ SECTION 7: 输出要求 ═══
        lines.append("=" * 50)
        lines.append("输出要求:")
        lines.append("1. 输出完整可运行的 Python 策略代码（一个继承 IStrategy 的类）")
        lines.append("2. 在代码注释中用 [FIX-#] 标注每个改动对应哪个改进清单项")
        lines.append("3. 代码最后用注释块附一段说明：「改动清单」+「预期效果」")
        lines.append("4. 代码可直接保存为 .py 文件导入使用，不要有 markdown 代码块标记")
        lines.append("5. class name 字段使用: " + (getattr(prev_gen, 'strategy_name', school) or school))

        lines.append(f"\n=== 原始/上一代代码 ===")
        lines.append(original_code[:6000])

        return "\n".join(lines)

    def _merge_failure_patterns(self, patterns: list[dict]) -> list[dict]:
        """合并相似的失败模式."""
        if len(patterns) <= 4:
            return patterns

        # 按 pattern 名称合并
        merged = {}
        for p in patterns:
            key = p.get("pattern", "unknown")
            if key not in merged:
                merged[key] = {
                    "pattern": key,
                    "count": 0,
                    "avg_loss_pct": 0,
                    "total_loss": 0,
                    "avg_bars_held": 0,
                    "examples": [],
                }
            m = merged[key]
            m["count"] += p.get("count", 0)
            if p.get("count", 0) > 0:
                m["avg_loss_pct"] = (
                    m["avg_loss_pct"] * (m["count"] - p["count"]) +
                    p.get("avg_loss_pct", 0) * p["count"]
                ) / m["count"] if m["count"] > 0 else 0
            m["total_loss"] += p.get("total_loss", 0)
            m["avg_bars_held"] = int(
                (m["avg_bars_held"] * (m["count"] - p["count"]) +
                 p.get("avg_bars_held", 0) * p["count"]) / m["count"]
            ) if m["count"] > 0 else 0
            m["examples"].extend(p.get("examples", [])[:2])

        result = sorted(merged.values(), key=lambda x: abs(x["total_loss"]), reverse=True)
        return result[:5]

    def _merge_success_patterns(self, patterns: list[dict]) -> list[dict]:
        """合并相似的成功模式."""
        if len(patterns) <= 3:
            return patterns

        merged = {}
        for p in patterns:
            key = p.get("pattern", "unknown")
            if key not in merged:
                merged[key] = {
                    "pattern": key,
                    "count": 0,
                    "avg_win_pct": 0,
                    "total_profit": 0,
                    "examples": [],
                }
            m = merged[key]
            m["count"] += p.get("count", 0)
            if p.get("count", 0) > 0:
                m["avg_win_pct"] = (
                    m["avg_win_pct"] * (m["count"] - p["count"]) +
                    p.get("avg_win_pct", 0) * p["count"]
                ) / m["count"] if m["count"] > 0 else 0
            m["total_profit"] += p.get("total_profit", 0)
            m["examples"].extend(p.get("examples", [])[:1])

        return sorted(merged.values(), key=lambda x: x["total_profit"], reverse=True)[:3]

    def _generate_targeted_fixes(self, school: str, prev_gen) -> list[str]:
        """基于分析数据生成针对性的改进建议."""
        fixes = []
        ta_data = getattr(prev_gen, 'trade_analyses', []) or []

        # 从 TradeAnalyzer 的 AI 摘要中提取建议
        for ta in ta_data:
            # 检查 exit_reasons
            exit_reasons = ta.get("exit_reasons", {})
            hold_time = ta.get("hold_time", {})

            # 硬止损过多
            hard_stop = exit_reasons.get("硬止损", 0)
            total = ta.get("total_trades", 0)
            if hard_stop > 0 and total > 0 and hard_stop / total > 0.3:
                fixes.append(
                    f"出场优化: {hard_stop}/{total}笔交易被硬止损（占{hard_stop/total*100:.0f}%），"
                    f"说明固定止损位太窄。建议改用 ATR(14)*2.0 的动态止损"
                )

            # 持仓时长不对称
            avg_loss_bars = hold_time.get("avg_loss_bars", 0)
            avg_win_bars = hold_time.get("avg_win_bars", 0)
            if avg_loss_bars > avg_win_bars * 1.5 and avg_loss_bars > 5:
                fixes.append(
                    f"出场加速: 亏损持仓{avg_loss_bars}天远长于盈利持仓{avg_win_bars}天，"
                    f"建议加 {hold_time.get('optimal_range', [3,8])[0]} 天的硬性时间止损"
                )

            # 小额连续亏损
            for fp in ta.get("failure_patterns", []):
                if "小额" in fp.get("pattern", ""):
                    fixes.append(
                        f"入场过滤: {fp['count']}笔小额连续亏损（<3%），说明噪音信号太多。"
                        f"建议加成交量确认（volume > MA(vol,20)*1.2）和趋势过滤（close > MA34）"
                    )

            # 大额亏损
            for fp in ta.get("failure_patterns", []):
                if "大额" in fp.get("pattern", ""):
                    fixes.append(
                        f"风险控制: {fp['count']}笔大额亏损（>5%）。"
                        f"建议设 max_loss_per_trade=3%，止损不随价格波动放宽"
                    )

        # 通用改进
        if prev_gen.avg_win_rate < 45:
            fixes.append(
                "入场收紧: 胜率<45%，说明入场信号太宽松。"
                "建议加双重确认（如: 技术信号 + KDJ/RSI 确认 + 成交量放大）"
            )

        if prev_gen.avg_win_rate < 35:
            fixes.append(
                "激进方案: 胜率<35%，考虑反转核心买卖逻辑，"
                "或在强趋势市场中暂停交易（MA34斜率<0时不出手）"
            )

        if prev_gen.avg_return < -5:
            fixes.append(
                "风险管理: 收益率为负，说明止损/止盈比不对。"
                "建议设最低盈亏比1.5:1（止盈至少是止损的1.5倍）"
            )

        if not fixes:
            fixes.append("提高信号质量: 加成交量过滤和趋势确认")
            fixes.append("优化止损: 根据ATR动态调整止损宽度")

        return fixes[:6]  # 最多 6 条
