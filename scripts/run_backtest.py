#!/usr/bin/env python
"""
CLI backtest runner script.
Usage: python scripts/run_backtest.py --symbols 000001 000002 --start 20240101
"""
import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.engine import run_backtest_cli


def main():
    parser = argparse.ArgumentParser(description="三才回测系统 CLI")
    parser.add_argument("--symbols", nargs="+", required=True, help="Stock symbols")
    parser.add_argument("--period", default="daily", help="K-line period (daily, 30min, 60min)")
    parser.add_argument("--start", help="Start date YYYYMMDD")
    parser.add_argument("--end", help="End date YYYYMMDD")
    parser.add_argument("--capital", type=float, default=1_000_000, help="Initial capital")
    parser.add_argument("--mode", default="simple", choices=["simple", "strict"],
                        help="Strategy mode: simple=MA crossover, strict=full Sancai rules")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    config = {
        "initial_capital": args.capital,
    }

    result = run_backtest_cli(
        symbols=args.symbols,
        period=args.period,
        start_date=args.start,
        end_date=args.end,
        config=config,
        mode=args.mode,
    )

    if args.json and "error" not in result:
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
